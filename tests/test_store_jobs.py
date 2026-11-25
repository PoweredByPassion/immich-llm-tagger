import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import current_version, migrate
from app.store import jobs


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


# ---------- 失败模式 8：迁移 ----------

def test_fresh_db_migrates_to_latest(db: Database) -> None:
    assert current_version(db) == 14
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "cancel_requested" in cols


def test_old_v1_db_upgraded_to_v2_on_startup(tmp_path: Path) -> None:
    """停在 v1 的老库（没有 cancel_requested）启动 migrate() 后补上 v2。

    用 v1.sql + version=1 手工搭出一个真实的老库形态，
    再走正常 migrate() 路径升级。
    """
    database = Database(tmp_path / "v1only.db")
    with database.connect() as conn:
        v1_sql = (Path(__file__).resolve().parent.parent
                  / "app/db/migrations/v1.sql").read_text(encoding="utf-8")
        conn.executescript(v1_sql)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version    INTEGER PRIMARY KEY,"
            "    applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    assert current_version(database) == 1
    with database.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "cancel_requested" not in cols

    migrate(database)
    assert current_version(database) == 14
    with database.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "cancel_requested" in cols


# ---------- 失败模式 1 / 2：claim ----------

def test_create_and_claim(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {"paths": ["a"]})
    assert jid > 0
    row = jobs.claim_next(db)
    assert row is not None
    assert row.id == jid
    assert row.status == "running"
    assert row.params == {"paths": ["a"]}
    assert row.started_at is not None


def test_claim_empty_queue_returns_none(db: Database) -> None:
    assert jobs.claim_next(db) is None


def test_two_claims_only_one_wins(db: Database) -> None:
    jobs.create_job(db, "scan", {})
    first = jobs.claim_next(db)
    second = jobs.claim_next(db)
    assert first is not None
    assert second is None


def test_auto_sources_atomically_share_one_active_asset_job(db: Database) -> None:
    parent_id = jobs.create_job(db, "scan", {})

    def enqueue(i):
        type_ = "single" if i % 2 else "classify_item"
        return jobs.enqueue_auto_job(db, type_, {
            "asset_id": "same", "parent_job_id": parent_id,
            "source": "webhook" if i % 2 else "poll",
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        submitted = list(pool.map(enqueue, range(16)))
    assert len({job_id for job_id, _ in submitted}) == 1
    assert sum(created for _, created in submitted) == 1


def test_reset_excludes_new_job_claims_and_direct_mutations(db: Database) -> None:
    with jobs.asset_mutation(db), pytest.raises(jobs.JobsActive):
        jobs.enqueue_reset(db, {"dimensions": ["tag"]})

    reset_id = jobs.enqueue_reset(db, {"dimensions": ["tag"]})
    scan_id, _ = jobs.enqueue_scan(db, {"source": "manual"})
    with pytest.raises(jobs.ResetInProgress), jobs.asset_mutation(db):
        pytest.fail("direct mutation started during reset")
    assert jobs.claim_next(db).id == reset_id
    assert jobs.claim_next(db) is None
    jobs.finish_job(db, reset_id)
    assert jobs.claim_next(db).id == scan_id


def test_reset_waits_for_persisted_pending_asset_writes(db: Database) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO asset_state(asset_id,process_status) VALUES ('a','pending')")
        conn.execute("INSERT INTO pending_effects(asset_id,batches) VALUES ('a','[]')")
    with pytest.raises(jobs.JobsActive):
        jobs.enqueue_reset(db, {"dimensions": ["tag"]})

    with db.connect() as conn:
        conn.execute("UPDATE asset_state SET process_status='failed' WHERE asset_id='a'")
    assert jobs.enqueue_reset(db, {"dimensions": ["tag"]}) > 0


def test_recovery_preserves_eligible_children_and_honors_canceled_jobs(db: Database) -> None:
    parent = jobs.create_job(db, "scan", {})
    assert jobs.claim_next(db).id == parent
    child = jobs.create_job(db, "classify_item", {"asset_id": "a", "parent_job_id": parent})
    assert jobs.claim_next(db).id == child
    canceled = jobs.create_job(db, "single", {"asset_id": "b"})
    assert jobs.claim_next(db).id == canceled
    assert jobs.cancel_job(db, canceled)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET discovery_complete=1 WHERE id=?", (parent,))

    resumed, coordinators = jobs.recover_jobs(db)

    assert resumed == [child]
    assert coordinators == [parent]
    assert jobs.get_job(db, canceled).status == "canceled"
    assert jobs.get_job(db, parent).status == "running"
    assert jobs.claim_next(db).id == child


def test_recovery_finishes_backfill_after_committed_final_page(db: Database) -> None:
    backfill = jobs.create_job(db, "backfill", {})
    assert jobs.claim_next(db).id == backfill
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET discovery_complete=1, total=10, done=10 WHERE id=?", (backfill,))

    assert jobs.recover_jobs(db) == ([], [])
    assert jobs.get_job(db, backfill).status == "done"
    assert jobs.get_job(db, backfill).done == 10


def test_checkpoint_defers_children_until_discovery_can_no_longer_shift(db: Database) -> None:
    parent = jobs.create_job(db, "scan", {})
    assert jobs.claim_next(db).id == parent
    assert jobs.enqueue_scan_items(
        db, parent, ["a", "a"], "poll", next_page=2, next_cursor="resume-here",
        defer_until_discovered=True,
    ) == 1
    assert jobs.get_job(db, parent).scan_page == 2
    assert jobs.get_job(db, parent).scan_cursor == "resume-here"
    assert jobs.get_job(db, parent).total == 1
    assert jobs.claim_next(db) is None

    assert jobs.enqueue_scan_items(
        db, parent, ["a"], "poll", defer_until_discovered=True, complete_discovery=True,
    ) == 0
    assert jobs.get_job(db, parent).discovery_complete
    assert jobs.get_job(db, parent).scan_cursor is None
    assert jobs.claim_next(db).params["asset_id"] == "a"


def test_finishing_a_scan_child_updates_parent_progress_once(db: Database) -> None:
    parent = jobs.create_job(db, "scan", {})
    assert jobs.claim_next(db).id == parent
    assert jobs.enqueue_scan_items(db, parent, ["a"], "poll") == 1
    child = jobs.claim_next(db).id

    jobs.finish_scan_item(db, child, parent)
    jobs.finish_scan_item(db, child, parent, error="duplicate completion")

    assert jobs.get_job(db, child).status == "done"
    assert (jobs.get_job(db, parent).done, jobs.get_job(db, parent).failed) == (1, 0)


def test_paused_discovery_can_resume_after_other_ready_work(db: Database) -> None:
    parent = jobs.create_job(db, "scan", {})
    assert jobs.claim_next(db).id == parent
    jobs.defer_discovery(db, parent, delay_seconds=15)
    assert jobs.claim_next(db) is None
    other = jobs.create_job(db, "single", {"asset_id": "a"})
    assert jobs.claim_next(db).id == other
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET available_at=datetime('now','-1 second') WHERE id=?", (parent,))
    assert jobs.claim_next(db).id == parent


def test_concurrent_workers_cannot_claim_the_same_job(db: Database) -> None:
    job_id = jobs.create_job(db, "scan", {})
    with ThreadPoolExecutor(max_workers=5) as pool:
        claimed = list(pool.map(jobs.claim_next, [db] * 5))
    assert [row.id for row in claimed if row is not None] == [job_id]
    assert jobs.get_job(db, job_id).status == "running"


def test_claim_fifo_by_id(db: Database) -> None:
    a = jobs.create_job(db, "scan", {})
    b = jobs.create_job(db, "single", {})
    assert jobs.claim_next(db).id == a
    assert jobs.claim_next(db).id == b


# ---------- 失败模式 3：finish ----------

def test_finish_without_error_marks_done(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    jobs.claim_next(db)
    jobs.finish_job(db, jid)
    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert row.finished_at is not None
    assert row.error is None


def test_finish_with_error_marks_failed(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    jobs.claim_next(db)
    jobs.finish_job(db, jid, error="boom")
    row = jobs.get_job(db, jid)
    assert row.status == "failed"
    assert row.finished_at is not None
    assert row.error == "boom"


# ---------- 失败模式 4 / 5：cancel ----------

def test_cancel_queued_job_marks_canceled(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    assert jobs.cancel_job(db, jid) is True
    row = jobs.get_job(db, jid)
    assert row.status == "canceled"
    assert row.finished_at is not None


def test_cancel_running_job_only_sets_flag(db: Database) -> None:
    """running 的 job 取消不改 status，worker 最终自然 finish。"""
    jid = jobs.create_job(db, "scan", {})
    jobs.claim_next(db)
    assert jobs.cancel_job(db, jid) is True
    row = jobs.get_job(db, jid)
    assert row.status == "running"
    assert jobs.is_canceled(db, jid) is True
    # Worker completes its current action before finalizing cancellation.
    jobs.finish_job(db, jid)
    row = jobs.get_job(db, jid)
    assert row.status == "canceled"


def test_cancel_terminal_job_returns_false(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    jobs.cancel_job(db, jid)
    assert jobs.cancel_job(db, jid) is False


def test_cancel_nonexistent_returns_false(db: Database) -> None:
    assert jobs.cancel_job(db, 999) is False


def test_is_canceled_false_by_default(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    assert jobs.is_canceled(db, jid) is False


# ---------- 失败模式 6：进度 ----------

def test_incr_progress_counts(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    jobs.incr_progress(db, jid)
    jobs.incr_progress(db, jid)
    jobs.incr_progress(db, jid, failed=True)
    row = jobs.get_job(db, jid)
    assert row.done == 2
    assert row.failed == 1


# ---------- 失败模式 7：active 去重 ----------

def test_has_active_job_of_type_only_queued_running(db: Database) -> None:
    assert jobs.has_active_job_of_type(db, "scan") is False
    jid = jobs.create_job(db, "scan", {})
    assert jobs.has_active_job_of_type(db, "scan") is True
    jobs.finish_job(db, jid)
    assert jobs.has_active_job_of_type(db, "scan") is False


def test_has_active_job_ignores_canceled_and_other_types(db: Database) -> None:
    jid = jobs.create_job(db, "scan", {})
    jobs.cancel_job(db, jid)
    assert jobs.has_active_job_of_type(db, "scan") is False
    jobs.create_job(db, "single", {})
    assert jobs.has_active_job_of_type(db, "scan") is False
    assert jobs.has_active_job_of_type(db, "single") is True


# ---------- 其他 ----------

def test_get_job_nonexistent_returns_none(db: Database) -> None:
    assert jobs.get_job(db, 999) is None


def test_create_job_stores_prompt_version_id(db: Database) -> None:
    jid = jobs.create_job(db, "reclassify", {}, prompt_version_id=7)
    assert jobs.get_job(db, jid).prompt_version_id == 7


def test_jobs_type_check_constraint(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (type, status) VALUES ('bogus', 'queued')"
            )


def test_jobs_status_check_constraint(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (type, status) VALUES ('scan', 'bogus')"
            )


# ---------- M4 Task 2: list_jobs ----------

def test_list_jobs_returns_id_desc(db: Database) -> None:
    ids = [jobs.create_job(db, "scan", {}) for _ in range(3)]
    rows = jobs.list_jobs(db)
    assert [r.id for r in rows] == ids[::-1]


def test_list_jobs_filter_status_and_type(db: Database) -> None:
    a = jobs.create_job(db, "scan", {})
    b = jobs.create_job(db, "single", {})
    jobs.finish_job(db, b)
    assert [r.id for r in jobs.list_jobs(db, status="queued")] == [a]
    assert [r.id for r in jobs.list_jobs(db, status="done")] == [b]
    assert [r.id for r in jobs.list_jobs(db, type_="single")] == [b]
    assert jobs.list_jobs(db, status="running", type_="scan") == []


def test_list_jobs_limit(db: Database) -> None:
    for _ in range(3):
        jobs.create_job(db, "scan", {})
    assert len(jobs.list_jobs(db, limit=2)) == 2
    assert len(jobs.list_jobs(db, limit=0)) == 0
    assert len(jobs.list_jobs(db, limit=-1)) == 0


def test_list_jobs_params_dict(db: Database) -> None:
    jobs.create_job(db, "scan", {"paths": ["x"]})
    row = jobs.list_jobs(db)[0]
    assert row.params == {"paths": ["x"]}


def test_list_jobs_offset(db: Database) -> None:
    for _ in range(4):
        jobs.create_job(db, "scan", {})
    assert len(jobs.list_jobs(db, limit=2, offset=2)) == 2
    assert jobs.list_jobs(db, limit=2, offset=3)[0].id == (
        jobs.list_jobs(db)[0].id - 3
    )


def test_count_jobs_matches_filters(db: Database) -> None:
    a = jobs.create_job(db, "scan", {})
    b = jobs.create_job(db, "single", {})
    jobs.finish_job(db, b)
    assert jobs.count_jobs(db) == 2
    assert jobs.count_jobs(db, status="queued") == 1
    assert jobs.count_jobs(db, status="done") == 1
    assert jobs.count_jobs(db, type_="single") == 1
    assert jobs.count_jobs(db, status="running") == 0
    assert a != b


def test_requeue_orphan_running_jobs_puts_back_to_queued(db: Database) -> None:
    """进程重启后 running job 没有 worker 承载，必须恢复，否则
    has_active_job_of_type 恒真 → 轮询调度永不触发。

    恢复方式是**重新入队**而非标记失败：scan 的 total 是候选数，
    已完成的由 executor 的 _already_done 跳过，重跑只补剩余的。
    685/722 被进程打断的 job 应该续跑到 722，而不是显示成失败。"""
    stuck = jobs.create_job(db, "scan", {})
    queued = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running', started_at="
                     "datetime('now'), done=685, total=722, "
                     "cancel_requested=0 WHERE id=?", (stuck,))
    assert jobs.recover_jobs(db) == ([stuck], [])
    row = jobs.get_job(db, stuck)
    assert row.status == "queued"
    # 进度保留（续跑的基线），时间戳与取消标志清掉
    assert row.done == 685 and row.total == 722
    assert row.started_at is None and row.finished_at is None
    assert row.error is None
    with db.connect() as conn:
        flag = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?",
                            (stuck,)).fetchone()["cancel_requested"]
    assert flag == 0
    # 其他状态不受影响
    assert jobs.get_job(db, queued).status == "queued"


def test_recovery_with_no_running_jobs_returns_empty(
    db: Database,
) -> None:
    jobs.create_job(db, "scan", {})
    assert jobs.recover_jobs(db) == ([], [])
    assert jobs.get_job(db, 1).status == "queued"


def test_requeued_job_is_claimable_again(db: Database) -> None:
    """续跑的关键闭环：恢复出的 queued job 必须能被 claim_next 抢到。"""
    stuck = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (stuck,))
    jobs.recover_jobs(db)
    claimed = jobs.claim_next(db)
    assert claimed is not None and claimed.id == stuck
    assert claimed.status == "running"
    # 再 claim 无 job（队列空）
    assert jobs.claim_next(db) is None


def test_recovery_returns_ids_ordered(db: Database) -> None:
    ids = [jobs.create_job(db, "scan", {}) for _ in range(3)]
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running'")
    assert jobs.recover_jobs(db) == (ids, [])


# ---------- 优化批次 Part A：子任务（classify_item）机制 ----------

def test_claim_next_prioritizes_non_child_jobs(db: Database) -> None:
    """classify_item 让位：FIFO 之内用户/webhook 任务先认领。"""
    jobs.create_job(db, "classify_item", {"asset_id": "a", "parent_job_id": 1})
    jobs.create_job(db, "single", {"asset_id": "b"})
    job = jobs.claim_next(db)
    assert job.type == "single"
    # 认领完用户任务后才轮到子任务
    job2 = jobs.claim_next(db)
    assert job2.type == "classify_item"


def test_list_jobs_excludes_classify_item(db: Database) -> None:
    """面向用户的列表默认排除 classify_item。"""
    jobs.create_job(db, "single", {"asset_id": "b"})
    jobs.create_job(db, "classify_item", {"asset_id": "a", "parent_job_id": 1})
    assert all(j.type != "classify_item" for j in jobs.list_jobs(db))
    assert jobs.count_jobs(db) == 1


def test_list_jobs_include_internal_positive(db: Database) -> None:
    """include_internal=True 纳入 classify_item；显式 type_ 亦绕过默认过滤。"""
    jobs.create_job(db, "single", {"asset_id": "b"})
    jobs.create_job(db, "classify_item", {"asset_id": "a", "parent_job_id": 1})
    assert jobs.count_jobs(db, include_internal=True) == 2
    assert {j.type for j in jobs.list_jobs(db, include_internal=True)} == {
        "single", "classify_item"
    }
    # 显式指定 type_ 时不受默认 internal 过滤影响（type_ 分支优先）
    assert jobs.count_jobs(db, type_="classify_item") == 1


def test_pending_children_and_cancel(db: Database) -> None:
    """父完成度：按 parent_job_id 统计未终态子任务；取消传播到 queued 子任务。"""
    pid = jobs.create_job(db, "scan", {})
    c1 = jobs.create_job(db, "classify_item",
                         {"asset_id": "a", "parent_job_id": pid})
    c2 = jobs.create_job(db, "classify_item",
                         {"asset_id": "b", "parent_job_id": pid})
    assert jobs.pending_children(db, pid) == 2
    jobs.finish_job(db, c1)
    assert jobs.pending_children(db, pid) == 1
    assert jobs.cancel_children_of(db, pid) == 1   # 取消 queued 的 c2
    assert jobs.get_job(db, c2).status == "canceled"
    assert jobs.pending_children(db, pid) == 0


def test_recovery_cancels_queued_and_running_children_of_canceled_parent(db: Database) -> None:
    pid = jobs.create_job(db, "scan", {})
    queued = jobs.create_job(db, "classify_item", {"asset_id": "a", "parent_job_id": pid})
    with db.connect() as conn:                                    # running（手工造）
        running = conn.execute(
            "INSERT INTO jobs (type, status, params) VALUES "
            "('classify_item', 'running', ?)",
            (json.dumps({"asset_id": "b", "parent_job_id": pid}),),
        ).lastrowid
    assert jobs.cancel_job(db, pid)
    assert jobs.recover_jobs(db) == ([], [])
    assert jobs.get_job(db, queued).status == "canceled"
    assert jobs.get_job(db, running).status == "canceled"
    assert jobs.pending_children(db, pid) == 0
