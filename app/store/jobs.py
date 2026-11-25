"""jobs 表仓储层：创建 / 原子抢占 / 取消 / 进度。

取消语义：
- queued  → 直接置 canceled（还没人动它）。
- running → 只置 cancel_requested 标志，不改 status。worker 在每张图之间
  轮询 is_canceled() 决定是否继续取新图，最终由 worker 正常 finish。
- 终态（done/failed/canceled）→ 无事发生，返回 False。

claim_next 在同一条 SQLite 更新中选择任务并返回结果。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass

from app.db.connection import Database

_CANCELABLE = ("queued", "running")

# scan 拆出的逐图子任务类型：内部实现细节，不进面向用户的列表/队列深度，
# 认领时让位于其它类型。
_INTERNAL_TYPE = "classify_item"
_ACTIVE = "status IN ('queued', 'running')"
_JOB_FIELDS = (
    "id, type, status, total, done, failed, params, prompt_version_id, "
    "started_at, finished_at, error, discovery_complete, scan_page, scan_cursor"
)
_RULE_SNAPSHOT_TYPES = frozenset({"single", "scan", "backfill", "reclassify", "dryrun"})


class JobsActive(RuntimeError):
    """Reset needs an idle queue and no active direct asset edits."""


class ResetInProgress(RuntimeError):
    """Asset edits must wait for the reserved reset to finish."""


@dataclass(frozen=True)
class JobRow:
    id: int
    type: str            # scan|backfill|single|reclassify|dryrun|reset|undo|classify_item
    status: str          # queued|running|done|failed|canceled
    total: int
    done: int
    failed: int
    params: dict
    prompt_version_id: int | None
    started_at: str | None
    finished_at: str | None
    error: str | None
    discovery_complete: bool = False
    scan_page: int = 1
    scan_cursor: str | None = None


def _row_to_job(row) -> JobRow:
    return JobRow(
        id=row["id"],
        type=row["type"],
        status=row["status"],
        total=row["total"],
        done=row["done"],
        failed=row["failed"],
        params=json.loads(row["params"]),
        prompt_version_id=row["prompt_version_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        discovery_complete=bool(row["discovery_complete"]),
        scan_page=row["scan_page"],
        scan_cursor=row["scan_cursor"],
    )


def create_job(
    db: Database, type_: str, params: dict, prompt_version_id: int | None = None
) -> int:
    with db.connect() as conn:
        return _insert_job(conn, type_, params, prompt_version_id)


def _insert_job(conn, type_: str, params: dict, prompt_version_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (type, status, params, prompt_version_id) "
        "VALUES (?, 'queued', ?, ?)",
        (type_, json.dumps(params, ensure_ascii=False), prompt_version_id),
    )
    job_id = cur.lastrowid
    if type_ in _RULE_SNAPSHOT_TYPES and not params.get("recover_pending"):
        from app.store.rules import read_rule_snapshot

        conn.execute("UPDATE jobs SET rule_snapshot=? WHERE id=?", (
            json.dumps(read_rule_snapshot(conn), ensure_ascii=False), job_id,
        ))
    return job_id


def enqueue_scan(db: Database, params: dict, *, enabled_setting: str | None = None) -> tuple[int | None, bool]:
    """Coalesce manual, periodic and webhook scans into one active scan."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if enabled_setting is not None and not _enabled(conn, enabled_setting):
            return None, False
        existing = conn.execute(
            f"SELECT id FROM jobs WHERE type='scan' AND {_ACTIVE} ORDER BY id LIMIT 1",
        ).fetchone()
        if existing:
            return existing["id"], False
        return _insert_job(conn, "scan", params), True


def enqueue_auto_job(db: Database, type_: str, params: dict, *,
                     enabled_setting: str | None = None) -> tuple[int | None, bool]:
    """Atomically deduplicate automatic asset jobs across entry points.

    A normal webhook for a completed asset returns ``(None, False)``.
    """
    if type_ not in {"single", _INTERNAL_TYPE}:
        raise ValueError(f"not an automatic asset job: {type_}")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if enabled_setting is not None and not _enabled(conn, enabled_setting):
            return None, False
        if type_ == "single" and params.get("source") == "webhook" and not params.get("recover_pending"):
            from app.store.assets import processing_complete

            if processing_complete(conn, params["asset_id"]):
                return None, False
        return _enqueue_auto_job(conn, type_, params)


def _enqueue_auto_job(conn, type_: str, params: dict) -> tuple[int, bool]:
    asset_id = params["asset_id"]
    existing = conn.execute(
        f"SELECT id FROM jobs WHERE type IN ('single','classify_item') AND {_ACTIVE} "
        "AND json_extract(params,'$.asset_id') = ? ORDER BY id LIMIT 1",
        (asset_id,),
    ).fetchone()
    if existing:
        return existing["id"], False
    if type_ == _INTERNAL_TYPE:
        same_scan = conn.execute(
            "SELECT id FROM jobs WHERE type='classify_item' "
            "AND json_extract(params,'$.parent_job_id') = ? "
            "AND json_extract(params,'$.asset_id') = ? ORDER BY id LIMIT 1",
            (params["parent_job_id"], asset_id),
        ).fetchone()
        if same_scan:
            return same_scan["id"], False
    return _insert_job(conn, type_, params), True


def enqueue_scan_items(db: Database, parent_job_id: int, asset_ids, source: str, *,
                       next_page: int | None = None, next_cursor: str | None = None,
                       discovered: int | None = None,
                       defer_until_discovered: bool = False,
                       complete_discovery: bool = False) -> int:
    """Commit a page of unique scan work and its progress together."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        parent = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id=? AND status='running'",
            (parent_job_id,),
        ).fetchone()
        if parent is None or parent["cancel_requested"]:
            return 0
        created = sum(
            _enqueue_auto_job(conn, _INTERNAL_TYPE, {
                "asset_id": asset_id, "parent_job_id": parent_job_id, "source": source,
                "defer_until_discovered": defer_until_discovered,
            })[1]
            for asset_id in asset_ids
        )
        conn.execute(
            "UPDATE jobs SET total=total+?, scan_page=COALESCE(?,scan_page), scan_cursor=?, "
            "discovery_complete=MAX(discovery_complete,?) WHERE id=?",
            (created if discovered is None else discovered, next_page, next_cursor,
             complete_discovery, parent_job_id),
        )
        return created


def enqueue_reset(db: Database, params: dict) -> int:
    """Reserve reset exclusivity only when no work can already be writing."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _has_active_jobs(conn) or _has_asset_mutation(conn) or _has_pending_writes(conn):
            raise JobsActive
        return _insert_job(conn, "reset", params)


def _has_active_jobs(conn) -> bool:
    return conn.execute(f"SELECT 1 FROM jobs WHERE {_ACTIVE} LIMIT 1").fetchone() is not None


def _has_asset_mutation(conn) -> bool:
    return conn.execute("SELECT 1 FROM active_asset_mutations LIMIT 1").fetchone() is not None


def _has_pending_writes(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM pending_effects JOIN asset_state USING (asset_id) "
        "WHERE process_status='pending' LIMIT 1",
    ).fetchone() is not None


def _enabled(conn, key: str) -> bool:
    from app.store.settings import DEFAULTS

    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return bool(json.loads(row["value"]) if row else DEFAULTS[key])


@contextmanager
def asset_mutation(db: Database):
    """Reserve one direct asset mutation without keeping SQLite locked during I/O."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(f"SELECT 1 FROM jobs WHERE type='reset' AND {_ACTIVE} LIMIT 1").fetchone():
            raise ResetInProgress
        mutation_id = conn.execute("INSERT INTO active_asset_mutations DEFAULT VALUES").lastrowid
    try:
        yield
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM active_asset_mutations WHERE id=?", (mutation_id,))


def claim_next(db: Database) -> JobRow | None:
    """Claim exclusive resets and prefer user-facing jobs over scan items."""
    with db.connect() as conn:
        if conn.execute(
            "SELECT 1 FROM jobs WHERE status='queued' "
            "AND (available_at IS NULL OR available_at<=datetime('now')) LIMIT 1",
        ).fetchone() is None:
            return None
        row = conn.execute(
            "UPDATE jobs SET status='running', started_at=datetime('now'), available_at=NULL WHERE id = ("
            "  SELECT candidate.id FROM jobs AS candidate WHERE status='queued' "
            "  AND (available_at IS NULL OR available_at<=datetime('now')) AND ("
            "    (candidate.type='reset' AND NOT EXISTS (SELECT 1 FROM jobs WHERE status='running')"
            "     AND NOT EXISTS (SELECT 1 FROM active_asset_mutations))"
            "    OR (candidate.type!='reset' AND NOT EXISTS ("
            "      SELECT 1 FROM jobs WHERE type='reset' AND status='running'"
            "    ))"
            "  ) AND (candidate.type!='classify_item' OR NOT EXISTS ("
            "    SELECT 1 FROM jobs AS parent WHERE parent.id=json_extract(candidate.params,'$.parent_job_id')"
            "    AND parent.type='scan' AND (parent.status!='running' OR parent.cancel_requested=1"
            "      OR (parent.discovery_complete=0 AND json_extract(candidate.params,'$.defer_until_discovered')=1))"
            "  )) ORDER BY CASE candidate.type WHEN 'reset' THEN 0 WHEN 'classify_item' THEN 2 ELSE 1 END,"
            "  candidate.id LIMIT 1"
            f") RETURNING {_JOB_FIELDS}",
        ).fetchone()
        return _row_to_job(row) if row else None


def finish_job(db: Database, job_id: int, *, error: str | None = None) -> None:
    with db.connect() as conn:
        _finish_job(conn, job_id, error, active_only=False)


def finish_scan_item(
    db: Database, child_id: int, parent_id: int, *, error: str | None = None,
) -> None:
    """Commit the child and its contribution to parent progress exactly once."""
    with db.connect() as conn:
        child_status = _finish_job(conn, child_id, error, active_only=True)
        if child_status in {"done", "failed"}:
            _incr_progress(conn, parent_id, failed=child_status == "failed")


def _finish_job(conn, job_id: int, error: str | None, *, active_only: bool) -> str | None:
    states = " AND status='running'" if active_only else ""
    row = conn.execute(
        "UPDATE jobs SET status = CASE WHEN cancel_requested = 1 THEN 'canceled' ELSE ? END, "
        f"finished_at = datetime('now'), error = ? WHERE id = ?{states} RETURNING status",
        ("failed" if error else "done", error, job_id),
    ).fetchone()
    return row["status"] if row else None


def cancel_job(db: Database, job_id: int) -> bool:
    """queued → canceled；running → 置取消标志；终态 → False。"""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None or row["status"] not in _CANCELABLE:
            return False
        if row["status"] == "queued":
            conn.execute(
                "UPDATE jobs SET status = 'canceled', "
                "finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
            _cancel_children(conn, job_id)
        else:
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE id = ?",
                (job_id,),
            )
        return True


def is_canceled(db: Database, job_id: int) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return row is not None and bool(row["cancel_requested"])


def incr_progress(db: Database, job_id: int, *, failed: bool = False) -> None:
    with db.connect() as conn:
        _incr_progress(conn, job_id, failed=failed)


def _incr_progress(conn, job_id: int, *, failed: bool) -> None:
    field = "failed" if failed else "done"
    conn.execute(f"UPDATE jobs SET {field} = {field} + 1 WHERE id = ?", (job_id,))


def get_job(db: Database, job_id: int) -> JobRow | None:
    with db.connect() as conn:
        row = conn.execute(f"SELECT {_JOB_FIELDS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def has_active_job_of_type(db: Database, type_: str) -> bool:
    """只算 queued/running —— done/failed/canceled 不去重。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE type = ? "
            "AND status IN ('queued', 'running') LIMIT 1",
            (type_,),
        ).fetchone()
        return row is not None


def list_jobs(
    db: Database, *, status: str | None = None, type_: str | None = None,
    limit: int = 50, offset: int = 0, include_internal: bool = False,
) -> list[JobRow]:
    """任务列表，id DESC。status/type_ 为 None 时不过滤。
    默认排除内部子任务 classify_item（include_internal=True 时纳入）。"""
    if limit <= 0:
        return []
    where, args = _job_filters(
        status=status, type_=type_, include_internal=include_internal
    )
    args.append(limit)
    args.append(offset)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_JOB_FIELDS} FROM jobs WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            args,
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def count_jobs(
    db: Database, *, status: str | None = None, type_: str | None = None,
    include_internal: bool = False,
) -> int:
    """与 list_jobs 同筛选的 COUNT(*)，供分页。默认排除 classify_item。"""
    where, args = _job_filters(
        status=status, type_=type_, include_internal=include_internal
    )
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE {' AND '.join(where)}", args
        ).fetchone()
    return row["n"]


def _job_filters(*, status: str | None, type_: str | None,
                 include_internal: bool = False) -> tuple[list[str], list]:
    where, args = ["1=1"], []
    if status is not None:
        where.append("status = ?")
        args.append(status)
    if type_ is not None:
        where.append("type = ?")
        args.append(type_)
    elif not include_internal:
        where.append("type != ?")
        args.append(_INTERNAL_TYPE)
    return where, args


def has_active_job(db: Database) -> bool:
    """任一类型存在 queued/running 任务。"""
    with db.connect() as conn:
        return _has_active_jobs(conn) or _has_asset_mutation(conn) or _has_pending_writes(conn)


def pending_children(db: Database, parent_job_id: int) -> int:
    """父 job 的未终态子任务数（父完成不变式的数据源）。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE type = ? "
            "AND json_extract(params, '$.parent_job_id') = ? "
            "AND status IN ('queued', 'running')",
            (_INTERNAL_TYPE, parent_job_id),
        ).fetchone()
    return row["n"]


def cancel_children_of(db: Database, parent_job_id: int) -> int:
    """取消父 job 的 queued 子任务（置 canceled）。running 的由 worker
    轮询父取消标志自然收尾。返回取消条数。"""
    with db.connect() as conn:
        return _cancel_children(conn, parent_job_id)


def _cancel_children(conn, parent_job_id: int) -> int:
    cur = conn.execute(
        "UPDATE jobs SET status='canceled', finished_at=datetime('now') WHERE type='classify_item' "
        "AND json_extract(params,'$.parent_job_id')=? AND status='queued'",
        (parent_job_id,),
    )
    return cur.rowcount


def defer_discovery(db: Database, job_id: int, *, delay_seconds: int = 15) -> None:
    """Make a paged discovery available for retry without discarding its checkpoint."""
    with db.connect() as conn:
        row = conn.execute(
            "UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'canceled' ELSE 'queued' END, "
            "available_at=CASE WHEN NOT cancel_requested THEN datetime('now',?) END, "
            "finished_at=CASE WHEN cancel_requested THEN datetime('now') END, started_at=NULL "
            "WHERE id=? AND status='running' RETURNING status",
            (f"+{delay_seconds} seconds", job_id),
        ).fetchone()
        if row and row["status"] == "canceled":
            _cancel_children(conn, job_id)


def recover_jobs(db: Database) -> tuple[list[int], list[int]]:
    """Recover once the data-directory lock proves the previous service stopped."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM active_asset_mutations")
        conn.execute("UPDATE asset_state SET lease_owner=NULL, lease_expires_at=NULL WHERE lease_owner IS NOT NULL")
        conn.execute(
            f"UPDATE jobs SET status='canceled', finished_at=datetime('now') WHERE {_ACTIVE} "
            "AND (cancel_requested=1 OR (type='classify_item' AND NOT EXISTS ("
            "  SELECT 1 FROM jobs AS parent WHERE parent.id=json_extract(jobs.params,'$.parent_job_id')"
            "  AND parent.type='scan' AND parent.status IN ('queued','running') AND parent.cancel_requested=0"
            ")))",
        )
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=datetime('now') "
            "WHERE status='running' AND type='backfill' AND discovery_complete=1",
        )
        coordinators = [row["id"] for row in conn.execute(
            "SELECT id FROM jobs WHERE status='running' AND type='scan' AND discovery_complete=1 ORDER BY id",
        )]
        resumed = [row["id"] for row in conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL, finished_at=NULL, error=NULL "
            "WHERE status='running' AND (type!='scan' OR discovery_complete=0) RETURNING id",
        )]
    return sorted(resumed), coordinators
