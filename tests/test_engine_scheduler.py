"""M3 Task 6: PollScheduler 单测。

并发正确性全部抽到纯函数 decide_tick，线程只是载体；
测试不起真线程等真实时间，mock 时间 + 注入 getter。
"""

from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.engine.scheduler import PollScheduler, decide_tick
from app.store import events, jobs


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


# ---------- 纯函数 decide_tick ----------

def test_decide_tick_fires_when_due() -> None:
    assert decide_tick(now=1000.0, last_run=None, interval=300, enabled=True, has_active=False)


def test_decide_tick_not_due_before_interval() -> None:
    assert not decide_tick(now=1000.0, last_run=800.0, interval=300, enabled=True, has_active=False)


def test_decide_tick_disabled_never_fires() -> None:
    assert not decide_tick(now=1000.0, last_run=None, interval=300, enabled=False, has_active=False)


def test_decide_tick_skips_when_active_scan_exists() -> None:
    assert not decide_tick(now=1000.0, last_run=None, interval=300, enabled=True, has_active=True)


def test_decide_tick_fires_at_exact_boundary() -> None:
    assert decide_tick(now=1300.0, last_run=1000.0, interval=300, enabled=True, has_active=False)


# ---------- 失败模式 3：运行中配置热读 ----------

def test_interval_hot_reload(db: Database, tmp_path: Path) -> None:
    intervals = [300.0]
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: None,
        interval_getter=lambda: intervals[0],
        enabled_getter=lambda: True,
    )
    assert scheduler.maybe_tick(now=0.0)  # 首次触发
    assert not scheduler.maybe_tick(now=100.0)  # 300s 内不到点
    intervals[0] = 60.0  # 运行中改小
    assert scheduler.maybe_tick(now=70.0)  # 按 60s 新间隔触发
    assert not scheduler.maybe_tick(now=100.0)
    assert not scheduler.maybe_tick(now=125.0)
    assert scheduler.maybe_tick(now=131.0)


# ---------- 失败模式 4：scan 去重 ----------

def test_dedup_with_active_scan(db: Database, tmp_path: Path) -> None:
    calls: list[int] = []
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: calls.append(1),
        interval_getter=lambda: 60,
        enabled_getter=lambda: True,
    )
    jobs.create_job(db, "scan", {})
    assert not scheduler.maybe_tick(now=0.0)
    assert calls == []
    types = [e["event_type"] for e in events.tail_events(db)]
    assert any("skip" in t for t in types)


def test_skip_active_event_is_throttled(db: Database, tmp_path: Path) -> None:
    """卡死状态下（孤儿 running job）这个事件每 tick 都满足条件，
    不节流几小时能灌出上千条同内容事件，把事件流淹掉。"""
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: None,
        interval_getter=lambda: 60,
        enabled_getter=lambda: True,
    )
    jobs.create_job(db, "scan", {})
    # 同一阻塞状态连续 tick：只记第一条
    for t in (0.0, 5.0, 30.0, 120.0, 299.0):
        assert not scheduler.maybe_tick(now=t)
    logged = [e for e in events.tail_events(db, limit=500)
              if e["event_type"] == "scan_skip_active"]
    assert len(logged) == 1
    # 超过节流间隔后才允许再记一条
    assert not scheduler.maybe_tick(now=299.0 + 300.0)
    logged = [e for e in events.tail_events(db, limit=500)
              if e["event_type"] == "scan_skip_active"]
    assert len(logged) == 2


def test_skip_active_not_logged_when_not_time_due(db: Database,
                                                  tmp_path: Path) -> None:
    """没到点的 tick 不记 skip 事件（否则正常轮询也刷噪音）。"""
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: None,
        interval_getter=lambda: 60,
        enabled_getter=lambda: True,
    )
    jobs.create_job(db, "scan", {})
    # 先跑一次到点的 tick（记下第一条 skip），再把时钟往前挪但未到点
    assert not scheduler.maybe_tick(now=0.0)
    before = len([e for e in events.tail_events(db, limit=500)
                  if e["event_type"] == "scan_skip_active"])
    assert before == 1
    assert not scheduler.maybe_tick(now=40.0)
    after = len([e for e in events.tail_events(db, limit=500)
                 if e["event_type"] == "scan_skip_active"])
    assert after == 1  # 未到点，没新增


def test_enqueue_when_no_active_scan(db: Database, tmp_path: Path) -> None:
    calls: list[int] = []
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: calls.append(1),
        interval_getter=lambda: 60,
        enabled_getter=lambda: True,
    )
    assert scheduler.maybe_tick(now=0.0)
    assert calls == [1]
    # last_run 已更新：同一 now 再 tick 不重复触发（变异：触发后不更新 last_run）
    assert not scheduler.maybe_tick(now=0.0)
    assert not scheduler.maybe_tick(now=59.0)
    assert scheduler.maybe_tick(now=60.0)
    assert calls == [1, 1]


# ---------- 失败模式 2：disabled ----------

def test_disabled_never_enqueue(db: Database, tmp_path: Path) -> None:
    calls: list[int] = []
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: calls.append(1),
        interval_getter=lambda: 60,
        enabled_getter=lambda: False,
    )
    for t in (0.0, 100.0, 1000.0):
        assert not scheduler.maybe_tick(now=t)
    assert calls == []


# ---------- 失败模式 5：stop 后不再触发 ----------

def test_stop_prevents_ticks(db: Database, tmp_path: Path) -> None:
    calls: list[int] = []
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: calls.append(1),
        interval_getter=lambda: 0,
        enabled_getter=lambda: True,
        tick_step=0.0,
    )
    scheduler.start()
    scheduler.stop()
    scheduler.stop()  # 幂等
    n = len(calls)
    scheduler.maybe_tick(now=0.0)
    assert len(calls) == n  # stop 后 maybe_tick 直接短路


# ---------- 线程作为载体：0 步长快速验证 ----------

def test_thread_loop_runs_and_stops(db: Database, tmp_path: Path) -> None:
    calls: list[int] = []
    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: calls.append(1),
        interval_getter=lambda: 0,
        enabled_getter=lambda: True,
        tick_step=0.0,
    )
    scheduler.start()
    import time

    deadline = time.monotonic() + 5.0
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    thread = scheduler._thread
    scheduler.stop()
    assert len(calls) >= 3
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_maybe_tick_rechecks_enabled_before_enqueue(db: Database, tmp_path: Path) -> None:
    """竞态收紧：首次 enabled 检查通过后、创建 job 前，若全局暂停被设置，
    则不再入队（M4 记录的 lifespan 后窗口）。"""
    enqueued = []
    calls = {"n": 0}

    def enabled_getter() -> bool:
        calls["n"] += 1
        return calls["n"] < 2  # 第一次查 enabled=True，第二次（enqueue 前）变 False

    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: enqueued.append(1),
        interval_getter=lambda: 300.0,
        enabled_getter=enabled_getter,
    )
    assert not scheduler.maybe_tick(now=0.0)
    assert enqueued == []
    assert calls["n"] == 2
