"""asset_state 与 change_log 的本地读写。"""
from __future__ import annotations

import sqlite3

from app.db.connection import Database


def has_system_archive(db: Database, asset_id: str) -> bool:
    """change_log 里是否存在该 asset 的本系统归档记录
    （action='archive' AND direction='apply'；undo 记录不算数）。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM change_log "
            " WHERE asset_id = :aid AND action = 'archive' AND direction = 'apply' "
            " LIMIT 1",
            {"aid": asset_id},
        ).fetchone()
    return row is not None


def write_changes_and_state(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    job_id: int,
    changes: list[tuple[str, str, str]],
    current_tag: str,
    current_markers: list[str],
    visibility: str,
    rating=None, description=None,
) -> None:
    """在调用方的事务里写 change_log 并 upsert asset_state。

    Applier 把本模块的本地写（多条 change_log + 一条 asset_state）合并进
    单个事务：调用方先把连接开好，这里只往里写，由外层统一 commit/rollback。
    """
    import json

    for action, _aid, payload in changes:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (:job_id, :aid, :action, 'apply', :payload)",
            {"job_id": job_id, "aid": asset_id, "action": action,
             "payload": payload},
        )
    conn.execute(
        "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
        "                        visibility, rating, description, last_job_id) "
        "VALUES (:aid, :tag, :markers, :vis, :rating, :desc, :job_id) "
        "ON CONFLICT(asset_id) DO UPDATE SET "
        "  current_tag = excluded.current_tag, "
        "  current_markers = excluded.current_markers, "
        "  visibility = excluded.visibility, "
        "  rating = excluded.rating, "
        "  description = excluded.description, "
        "  last_job_id = excluded.last_job_id, "
        "  updated_at = datetime('now')",
        {"aid": asset_id, "tag": current_tag,
         "markers": json.dumps(current_markers), "vis": visibility,
         "rating": rating, "desc": description, "job_id": job_id},
    )


# ---------- 读查询 ----------

import json

_RECORD_FIELDS = (
    "r.id, r.asset_id, r.job_id, r.prompt_version_id, r.source, "
    "r.predicted_tag, r.confidence, r.markers, r.model, r.usage, "
    "r.duration_ms, r.status, r.raw_response, r.dry_run, r.created_at, "
    "r.applied_at, r.llm_request, r.filename, r.rating, r.description, "
    "s.current_tag AS current_tag, s.visibility AS visibility, "
    "s.apply_attempts AS apply_attempts"
)


def _record_filters(
    *, status: str | None, tag: str | None, current_tag: str | None,
    source: str | None, job_id: int | None, dry_run: int | None = None,
    include_unapplied: bool = False,
) -> tuple[str, dict]:
    """构造 WHERE 片段；tag 走 predicted_tag（历史），current_tag 走
    LEFT JOIN asset_state（本地快照，可能不存在）。dry_run: None=全部，
    1=沙盒，0=正式。

    include_unapplied 只在 status="error" 时放宽：apply 失败但仍在重试的
    记录 status 仍是 'ok'，失败由 applied_at IS NULL 表达（下轮 scan 重试），
    故一并纳入。status="ok" 保持原语义（"分析成功"），不被悄悄改掉。

    **dry_run = 0 这个附加条件不在任务书的 SQL 块里，是必要的修正**：沙盒
    (dry_run=1) 记录从不写 applied_at（_write_record 不写，_mark_applied 只
    覆盖 dry_run=0），因此「ok + applied_at IS NULL」会命中每一条成功的
    dryrun 记录，让 dryrun 任务详情页把它们全列成"失败清单"。只有真实 apply
    才会失败重试，故限定 dry_run=0。
    """
    where, params = ["1=1"], {}
    if status is not None and include_unapplied and status == "error":
        where.append("(r.status = 'error' OR (r.status = 'ok' "
                     "AND r.applied_at IS NULL AND r.dry_run = 0))")
    elif status is not None:
        where.append("r.status = :status")
        params["status"] = status
    if tag is not None:
        where.append("r.predicted_tag = :tag")
        params["tag"] = tag
    if current_tag is not None:
        where.append("s.current_tag = :current_tag")
        params["current_tag"] = current_tag
    if source is not None:
        where.append("r.source = :source")
        params["source"] = source
    if job_id is not None:
        where.append("r.job_id = :job_id")
        params["job_id"] = job_id
    if dry_run is not None:
        where.append("r.dry_run = :dry_run")
        params["dry_run"] = dry_run
    return " AND ".join(where), params


def _row_to_record(row) -> dict:
    d = dict(row)
    d["markers"] = json.loads(d["markers"]) if d["markers"] else []
    d["usage"] = json.loads(d["usage"]) if d["usage"] else None
    d["llm_request"] = json.loads(d["llm_request"]) if d.get("llm_request") else None
    return d


def query_records(
    db: Database, *, status: str | None = None, tag: str | None = None,
    current_tag: str | None = None, source: str | None = None,
    job_id: int | None = None, dry_run: int | None = None,
    include_unapplied: bool = False,
    limit: int = 50, offset: int = 0,
) -> list[dict]:
    """列表页主查询，id DESC。markers/usage 是 JSON TEXT，返回 dict
    （markers 解析为 list，usage 解析为 dict 或 None）；current_tag/
    visibility 来自 LEFT JOIN asset_state，无 state 行时为 None。

    include_unapplied=True 且 status="error" 时把「apply 失败待重试」的
    ok 记录（applied_at IS NULL）一并列出；见 _record_filters。"""
    if limit <= 0:
        return []
    where, params = _record_filters(
        status=status, tag=tag, current_tag=current_tag, source=source,
        job_id=job_id, dry_run=dry_run, include_unapplied=include_unapplied)
    params["limit"] = limit
    params["offset"] = offset
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_RECORD_FIELDS} FROM asset_records r "
            "LEFT JOIN asset_state s ON s.asset_id = r.asset_id "
            f"WHERE {where} ORDER BY r.id DESC LIMIT :limit OFFSET :offset",
            params,
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def count_records(
    db: Database, *, status: str | None = None, tag: str | None = None,
    current_tag: str | None = None, source: str | None = None,
    job_id: int | None = None, dry_run: int | None = None,
    include_unapplied: bool = False,
) -> int:
    """与 query_records 同筛选的 COUNT(*)，供分页。
    include_unapplied 必须与 query_records 同步透传，否则总数与列表对不上。"""
    where, params = _record_filters(
        status=status, tag=tag, current_tag=current_tag, source=source,
        job_id=job_id, dry_run=dry_run, include_unapplied=include_unapplied)
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM asset_records r "
            "LEFT JOIN asset_state s ON s.asset_id = r.asset_id "
            f"WHERE {where}", params,
        ).fetchone()
    return row["n"]


def current_tag_counts(db: Database) -> dict[str, int]:
    """按 current_tag（asset_state 本地快照）统计每标签的资产数，
    供图片记录页筛选下拉展示各标签数量。无 current_tag 的行计入 None。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT current_tag, COUNT(*) AS n FROM asset_state "
            "GROUP BY current_tag"
        ).fetchall()
    return {row["current_tag"]: row["n"] for row in rows}


def get_record(db: Database, record_id: int) -> dict | None:
    """单条记录，字段全（含 markers/usage 解析）。不存在返回 None。"""
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT {_RECORD_FIELDS} FROM asset_records r "
            "LEFT JOIN asset_state s ON s.asset_id = r.asset_id "
            "WHERE r.id = ?",
            (record_id,),
        ).fetchone()
    return _row_to_record(row) if row else None


def records_for_asset(db: Database, asset_id: str) -> list[dict]:
    """分类历史，created_at DESC（created_at 相同时 id 兜底）。"""
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_RECORD_FIELDS} FROM asset_records r "
            "LEFT JOIN asset_state s ON s.asset_id = r.asset_id "
            "WHERE r.asset_id = ? ORDER BY r.created_at DESC, r.id DESC",
            (asset_id,),
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def get_state(db: Database, asset_id: str) -> dict | None:
    """asset_state 单条；不存在返回 None。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM asset_state WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    return dict(row) if row else None


def changes_for_job(db: Database, job_id: int) -> list[dict]:
    """本 job 的 change_log 明细，id ASC（apply 逆序撤销由执行器负责，
    这里按写入顺序展示）。payload 解析为 dict。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, asset_id, action, direction, payload, applied_at "
            "FROM change_log WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        out.append(d)
    return out


def count_changes_for_job(db: Database, job_id: int) -> tuple[int, int]:
    """(apply 变更条数, distinct asset 数)，供回滚确认文案。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT asset_id) AS a "
            "FROM change_log WHERE job_id = ? AND direction = 'apply'",
            (job_id,),
        ).fetchone()
    return row["n"], row["a"]


def last_written_value(db: Database, asset_id: str, action: str) -> object | None:
    """本系统最近一次 apply 写入某字段的 new 值（判定"是否我们自己写的"）。
    找不到记录返回 None。payload 解析失败按 None 处理。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM change_log "
            " WHERE asset_id = ? AND action = ? AND direction = 'apply' "
            " ORDER BY id DESC LIMIT 1",
            (asset_id, action),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"]).get("new")
    except (TypeError, ValueError):
        return None


def bumped_apply_attempts(db: Database, asset_id: str) -> int:
    """apply_attempts += 1 并返回新值（无 state 行则建行）。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, apply_attempts) VALUES (:aid, 1) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "  apply_attempts = apply_attempts + 1, updated_at = datetime('now')",
            {"aid": asset_id},
        )
        row = conn.execute(
            "SELECT apply_attempts FROM asset_state WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    return int(row["apply_attempts"])
