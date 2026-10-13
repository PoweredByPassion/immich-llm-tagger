"""M3 Task 7: lifespan 装配契约。

- lifespan 启动后 pool/scheduler 在 app.state 且线程活着，关闭后 join
- worker 数量固定为 5（spec：不再读 llm_conn.concurrency）
- webhook 路由在真实 lifespan 下可达（401 / 200 各一）
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.db.connection import Database
from app.store.settings import SettingsStore
from app.web.app import create_app


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _bootstrap(data_dir: Path) -> BootstrapConfig:
    return BootstrapConfig(data_dir=str(data_dir))


# ------------------------------------------------------------- lifespan


def test_lifespan_starts_pool_and_scheduler_and_stops_them(data_dir: Path):
    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))) as client:
        pool = client.app.state.pool
        scheduler = client.app.state.scheduler
        assert pool._threads, "worker 线程必须已启动"
        assert all(t.is_alive() for t in pool._threads)
        assert scheduler._thread is not None and scheduler._thread.is_alive()

    # shutdown 后线程必须已 join，不留泄漏
    assert not any(t.is_alive() for t in pool._threads)
    assert scheduler._thread is None or not scheduler._thread.is_alive()


def test_worker_count_is_fixed_five(data_dir: Path):
    """worker 数固定 5（spec：分类并发），不再读 llm_conn.concurrency。"""
    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))) as client:
        assert len(client.app.state.pool._threads) == 5


def test_scheduler_reads_poll_settings(data_dir: Path):
    """enabled_getter/interval_getter 必须来自 settings（poll_enabled/poll_interval_seconds）。"""
    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))) as client:
        scheduler = client.app.state.scheduler
        assert scheduler._enabled_getter() is True
        assert scheduler._interval_getter() == 300  # DEFAULTS["poll_interval_seconds"]

        db = client.app.state.db
        SettingsStore(db).set("poll_enabled", False)
        SettingsStore(db).set("poll_interval_seconds", 42)
        assert scheduler._enabled_getter() is False
        assert scheduler._interval_getter() == 42


# ------------------------------------------------- 孤儿 job 恢复（685/722 卡死回归）


def test_lifespan_requeues_orphan_running_jobs(data_dir: Path):
    """线上 job #24 卡在 685/722 的回归：进程重启后 lifespan 必须把
    无人承载的 running job 重新入队，否则 has_active_job_of_type 恒真
    → 轮询永不触发 → 进度永远停在 685。"""
    from app.db.migrate import migrate
    from app.store import jobs

    db = Database(_bootstrap(data_dir).db_path)
    migrate(db)
    stuck = jobs.create_job(db, "scan", {"source": "manual"})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running', done=685, total=722 "
                     "WHERE id=?", (stuck,))

    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))):
        live = Database(_bootstrap(data_dir).db_path)
        row = jobs.get_job(live, stuck)
        # 要么已被 worker 重新 claim（running，进度保留），要么还在队列里等
        assert row.status in ("queued", "running")
        assert row.done == 685 and row.total == 722
        # 记了恢复事件
        from app.store import events
        assert any(e["event_type"] == "jobs_resumed"
                   for e in events.tail_events(live, limit=200))


def test_lifespan_leaves_healthy_running_job_alone(data_dir: Path,
                                                   monkeypatch):
    """守卫回归：同一进程内二次驱动 lifespan 不能把正在跑的 job 抢走。

    app2 不起 worker（patch 掉 pool.start），排除"恢复后又被立刻 claim"
    的干扰，纯粹验证守卫生效。store 层的等价断言见
    test_requeue_orphan_running_jobs_is_idempotent_per_process。
    """
    from app.db.migrate import migrate
    from app.store import jobs

    monkeypatch.setattr("app.engine.pool.WorkerPool.start", lambda self: None)

    db = Database(_bootstrap(data_dir).db_path)
    migrate(db)
    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))) as client:
        live = client.app.state.db
        running = jobs.create_job(live, "scan", {})
        with live.connect() as conn:
            conn.execute("UPDATE jobs SET status='running', "
                         "started_at=datetime('now') WHERE id=?", (running,))
        before = jobs.get_job(live, running)
        # 同进程再开一个 app：单次守卫生效，不动这个 job
        with TestClient(create_app(data_dir=data_dir,
                                   bootstrap=_bootstrap(data_dir))) as client2:
            after = jobs.get_job(client2.app.state.db, running)
        assert after.status == "running", "job 被二次 lifespan 恢复了"
        assert after.started_at == before.started_at


# ------------------------------------------------------------- webhook


def test_webhook_reachable_401_without_token(data_dir: Path):
    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))) as client:
        resp = client.post("/api/hooks/immich", json={"data": {"asset": {"id": "a1"}}})
        assert resp.status_code == 401


def test_webhook_reachable_200_with_token(data_dir: Path):
    import app.web.routes.hooks as hooks_mod
    hooks_mod._last_fallback_scan_ts = 0.0
    with TestClient(create_app(data_dir=data_dir, bootstrap=_bootstrap(data_dir))) as client:
        db = client.app.state.db
        token = SettingsStore(db).webhook_token()
        resp = client.post(
            "/api/hooks/immich",
            json={"data": {"asset": {
                "id": "asset-1", "type": "IMAGE",
                "originalFileName": "IMG_1.png",
                "originalPath": "/lib/a.png", "libraryId": "lib-1",
                "exifInfo": {}, "tags": [],
            }}},
            headers={"x-webhook-token": token},
        )
        assert resp.status_code == 200
        with db.connect() as conn:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE type = 'single' "
                "AND json_extract(params, '$.asset_id') = 'asset-1'"
            ).fetchone()
        assert n == 1
