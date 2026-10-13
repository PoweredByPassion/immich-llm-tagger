"""M3 Task 5: WorkerPool 的测试。

纪律：只验证线程生命周期（start/stop/join）与分发逻辑；业务语义
（取消、去重、total）全部在 executor 层单线程测。用注入的 executor
替身避免真实管线，claim_next 原子性已在 tests/test_store_jobs.py 覆盖。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import app.engine.pool as pool_module
from app.db.connection import Database
from app.db.migrate import migrate
from app.engine.pool import WorkerPool
from app.store import jobs


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "pool.db"
    d = Database(p)
    migrate(d)
    return str(p)


class FakeExecutor:
    """记录分发调用的 executor 替身，可模拟未预期异常。"""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple[int, str]] = []
        self.lock = threading.Lock()
        self.exc = exc

    def __call__(self, db, client, llm, job, **kwargs) -> None:
        with self.lock:
            self.calls.append((job.id, job.type))
        if self.exc is not None:
            raise self.exc


def wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_dispatches_each_job_type_once(db_path: str, monkeypatch) -> None:
    """claim_next → 按 type 分发到 executor；每 job 恰好执行一次。"""
    ex = FakeExecutor()
    monkeypatch.setattr(pool_module, "run_job", ex)
    db = Database(db_path)
    jids = [jobs.create_job(db, t, {"asset_id": f"a{i}"})
            for i, t in enumerate(["single", "scan", "dryrun"])]

    pool = WorkerPool(db_path, size=2, executor=ex)
    pool.start()
    assert wait_until(lambda: len(ex.calls) == 3)
    pool.stop()

    assert sorted(c[0] for c in ex.calls) == jids
    types = {jid: t for jid, t in ex.calls}
    assert types[jids[0]] == "single"
    assert types[jids[1]] == "scan"
    assert types[jids[2]] == "dryrun"


def test_unknown_type_fails_job_with_event(db_path: str) -> None:
    """未知 type（backfill 等）→ job_error 事件 + finish_job(error)。"""
    db = Database(db_path)
    jid = jobs.create_job(db, "backfill", {})
    pool = WorkerPool(db_path, size=1)
    pool.start()
    assert wait_until(lambda: jobs.get_job(db, jid).status == "failed")
    pool.stop()

    row = jobs.get_job(db, jid)
    assert row.status == "failed" and row.error is not None
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT event_type FROM events WHERE event_type = 'job_error'"
        ).fetchall()
    assert len(rows) == 1


def test_worker_survives_executor_exception(db_path: str,
                                            monkeypatch) -> None:
    """executor 抛未预期异常 → worker 记 job_error 并继续取下一个 job。"""
    ex = FakeExecutor(exc=RuntimeError("boom"))
    monkeypatch.setattr(pool_module, "run_job", ex)
    db = Database(db_path)
    jid = jobs.create_job(db, "single", {"asset_id": "a1"})
    jid2 = jobs.create_job(db, "single", {"asset_id": "a2"})

    pool = WorkerPool(db_path, size=1, executor=ex)
    pool.start()
    assert wait_until(lambda: jobs.get_job(db, jid2).status == "failed")
    pool.stop()

    # 第一个 job 被 executor 炸掉后，worker fallback finish_job(error)，
    # 且线程没死——第二个 job 仍被领取
    assert jobs.get_job(db, jid).status == "failed"
    assert jobs.get_job(db, jid2).status == "failed"
    assert len(ex.calls) == 2


def test_stop_join_terminates_threads(db_path: str) -> None:
    """stop 后线程全部退出（join 成功），空闲轮询不残留。"""
    pool = WorkerPool(db_path, size=3, executor=FakeExecutor())
    pool.start()
    assert len(pool._threads) == 3
    assert all(t.is_alive() for t in pool._threads)
    pool.stop()
    assert not any(t.is_alive() for t in pool._threads)


def test_idle_workers_sleep_without_busy_loop(db_path: str) -> None:
    """无 job 时不空转：短窗口内不领取任何东西也不异常。"""
    ex = FakeExecutor()
    pool = WorkerPool(db_path, size=2, executor=ex, poll_seconds=0.05)
    pool.start()
    time.sleep(0.15)
    pool.stop()
    assert ex.calls == []


def test_no_job_duplication_between_workers(db_path: str) -> None:
    """多 worker 下每个 job 恰好执行一次（claim_next 原子性的集成面）。"""
    ex = FakeExecutor()
    monkeypatch_set = ex
    import app.engine.pool as pm
    # 直接注入 executor，走真实 run_job 分发前的分发点
    pm.run_job  # noqa: B018 —— 确认符号存在
    db = Database(db_path)
    jids = [jobs.create_job(db, "single", {"asset_id": f"a{i}"})
            for i in range(8)]

    pool = WorkerPool(db_path, size=4, executor=ex)
    pool.start()
    assert wait_until(lambda: len(ex.calls) == 8)
    pool.stop()

    got = sorted(c[0] for c in ex.calls)
    assert got == jids  # 无重复、无遗漏


def test_pool_passes_on_scan_split(db_path: str) -> None:
    """scan 执行时，pool 把 on_scan_split（协调器钩子）透传给 executor。"""
    called = threading.Event()

    def executor(db, client, llm, job, *, on_scan_split=None):
        if job.type == "scan" and on_scan_split is not None:
            on_scan_split(job.id)

    def hook(job_id: int) -> None:
        called.set()

    db = Database(db_path)
    jobs.create_job(db, "scan", {})
    pool = WorkerPool(db_path, size=1, executor=executor, on_scan_split=hook)
    pool.start()
    assert called.wait(2.0)
    pool.stop()


def test_pool_runs_children_concurrently(db_path: str) -> None:
    """spec 验证：N 个 classify_item 子任务真并发（不是串行）。用 Barrier(N)
    强制 N 个 worker 同时进入 executor——只有真并发才能全部到达，否则 barrier
    超时。"""
    n = 3
    barrier = threading.Barrier(n, timeout=5)
    in_flight = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def executor(db, client, llm, job, *, on_scan_split=None):
        if job.type != "classify_item":
            return
        with lock:
            in_flight["cur"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["cur"])
        barrier.wait()          # 全部到齐才放行 → 证明 N 张同时在跑
        with lock:
            in_flight["cur"] -= 1

    db = Database(db_path)
    for i in range(n):
        jobs.create_job(db, "classify_item",
                        {"asset_id": f"a{i}", "parent_job_id": 1})

    pool = WorkerPool(db_path, size=n, executor=executor, poll_seconds=0.02)
    pool.start()
    assert wait_until(lambda: in_flight["max"] == n, timeout=5)
    pool.stop()

    assert in_flight["max"] == n
