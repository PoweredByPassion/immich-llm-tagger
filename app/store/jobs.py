"""jobs 表仓储层：创建 / 原子抢占 / 取消 / 进度。

取消语义：
- queued  → 直接置 canceled（还没人动它）。
- running → 只置 cancel_requested 标志，不改 status。worker 在每张图之间
  轮询 is_canceled() 决定是否继续取新图，最终由 worker 正常 finish。
- 终态（done/failed/canceled）→ 无事发生，返回 False。

claim_next 的原子性：UPDATE ... WHERE status='queued' 带 LIMIT 的 rowcount
只有一个调用者能成功。SQLite 单写者 + WAL 下，顺序调用即可模拟两个竞争者。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.db.connection import Database

_CANCELABLE = ("queued", "running")

# scan 拆出的逐图子任务类型：内部实现细节，不进面向用户的列表/队列深度，
# 认领时让位于其它类型。
_INTERNAL_TYPE = "classify_item"


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
    )


def create_job(
    db: Database, type_: str, params: dict, prompt_version_id: int | None = None
) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, status, params, prompt_version_id) "
            "VALUES (?, 'queued', ?, ?)",
            (type_, json.dumps(params, ensure_ascii=False), prompt_version_id),
        )
        return cur.lastrowid


def claim_next(db: Database) -> JobRow | None:
    """原子抢占：优先非 classify_item，其后 FIFO（id ASC）。
    两段 SELECT-then-UPDATE 保持原原子性：UPDATE 带 status='queued'，
    rowcount==0 即被抢，返回 None。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' "
            "AND type != ? ORDER BY id LIMIT 1",
            (_INTERNAL_TYPE,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' "
                "ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        job_id = row["id"]
        cur = conn.execute(
            "UPDATE jobs SET status = 'running', started_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (job_id,),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row)


def finish_job(db: Database, job_id: int, *, error: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = datetime('now'), error = ? "
            "WHERE id = ?",
            ("failed" if error else "done", error, job_id),
        )


def cancel_job(db: Database, job_id: int) -> bool:
    """queued → canceled；running → 置取消标志；终态 → False。"""
    with db.connect() as conn:
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
        if failed:
            conn.execute(
                "UPDATE jobs SET failed = failed + 1 WHERE id = ?", (job_id,)
            )
        else:
            conn.execute(
                "UPDATE jobs SET done = done + 1 WHERE id = ?", (job_id,)
            )


def get_job(db: Database, job_id: int) -> JobRow | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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
            f"SELECT * FROM jobs WHERE {' AND '.join(where)} "
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
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
    return row is not None


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
        cur = conn.execute(
            "UPDATE jobs SET status = 'canceled', finished_at = datetime('now') "
            "WHERE type = ? "
            "AND json_extract(params, '$.parent_job_id') = ? "
            "AND status = 'queued'",
            (_INTERNAL_TYPE, parent_job_id),
        )
        return cur.rowcount


def drop_orphan_children(db: Database) -> int:
    """启动恢复：cancel 所有非终态 classify_item（running + queued）。
    此时 worker 尚未启动，任何 classify_item 都是孤儿；父 scan 会被
    requeue_orphan_running_jobs 重入队并重拆——从根上消除重复子任务。
    必须在 requeue_orphan_running_jobs **之前**调用（app.py lifespan）。
    带 per-DB-path 单次守卫（同 requeue_orphan_running_jobs：测试与热重载
    会重复驱动 lifespan，二次执行会误杀活体子任务）。"""
    key = str(db.path)
    if key in _orphan_children_cleanup_done:
        return 0
    _orphan_children_cleanup_done.add(key)
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'canceled', finished_at = datetime('now') "
            "WHERE type = ? AND status IN ('queued', 'running')",
            (_INTERNAL_TYPE,),
        )
        return cur.rowcount


def requeue_orphan_running_jobs(db: Database) -> list[int]:
    """启动时恢复孤儿 job：进程重启时 running 状态的 job 已无 worker 承载，
    会永久卡死（has_active_job_of_type 恒真 → 轮询永不触发）。

    **重新入队而不是标记失败**：scan/backfill 的 total 是"扫描发现的候选数"，
    已完成的那些由 executor 的 `_already_done` 自动跳过，重跑只会补剩下的
    部分。标记失败会让用户看到"685/722 失败"，而实际只差 37 张 —— 那是
    进程被杀打断的，不是业务失败。

    返回被恢复的 job id 列表（调用方可决定记什么事件）。

    每个进程（按 DB 路径）只执行一次：测试与热重载会重复驱动同一 app
    的 lifespan，二次执行会把测试中途构造的 running job 误改。
    """
    key = str(db.path)
    if key in _orphan_recovery_done:
        return []
    _orphan_recovery_done.add(key)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'running' ORDER BY id"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            # cancel_requested 一并清掉：用户上次点的取消已随进程作废，
            # 重启后的续跑不应被陈旧的取消标志立刻中断。
            conn.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL, "
                "finished_at = NULL, error = NULL, cancel_requested = 0 "
                "WHERE status = 'running'"
            )
    return ids


# 已执行过孤儿恢复的 DB 路径集合（进程级单次守卫）
_orphan_recovery_done: set[str] = set()

# 已执行过孤儿子任务清理的 DB 路径集合（进程级单次守卫，同 _orphan_recovery_done）
_orphan_children_cleanup_done: set[str] = set()
