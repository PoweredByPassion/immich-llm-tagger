"""概览页 + SSE 端点契约：路由可达、数字区块渲染、SSE 帧格式与 JSON 字段。

SSE 端点是无限 async generator；starlette TestClient 的 in-memory transport
会阻塞到响应体结束（handle_request 里 portal.call(self.app, ...)），无法中途
断开，因此 SSE 用例绕过 TestClient，直接以 ASGI 协议驱动 app：起 lifespan →
发 GET → 收够 N 帧后 cancel，等价于客户端断开（CancelledError 清理路径也被
覆盖）。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.jobs import create_job
from app.store.queries import overview
from app.store.settings import SettingsStore
from app.web.app import create_app


@pytest.fixture()
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        # 阻止后台调度线程入队 scan job：tick_step 是 5s 粒度的 stop.wait，
        # 立刻 set poll_enabled=False 挡不住已越过 enabled 检查的第一次 tick，
        # 所以直接停掉调度线程（stop() 幂等，lifespan 收尾再调一次无害）。
        c.app.state.scheduler.stop()
        SettingsStore(c.app.state.db).set("poll_enabled", False)
        # 兜底：把竞态窗口内已入队的 job 清掉
        import sqlite3
        with c.app.state.db.connect() as conn:
            conn.execute("DELETE FROM jobs")
        yield c


# ------------------------------------------------------- sse test infrastructure

def _collect_sse_frames(
    app: Any, path: str, n_frames: int, max_seconds: float = 5.0
) -> list[dict]:
    """直接 ASGI 驱动：返回消息列表，收够 n_frames 或超时后 cancel（模拟断开）。"""
    msgs: list[dict] = []

    async def run() -> list[dict]:
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "path": path,
            "raw_path": path.encode(), "root_path": "", "scheme": "http",
            "query_string": b"", "headers": [(b"host", b"test")],
            "client": ("testclient", 0), "server": ("testserver", 80), "state": {},
        }

        async def receive() -> dict:
            await asyncio.Event().wait()  # 永不收到 http.disconnect，靠 cancel 断开

        async def send(message: dict) -> None:
            msgs.append(message)

        got_startup = asyncio.Event()

        async def lifespan_send(message: dict) -> None:
            if message["type"] == "lifespan.startup.complete":
                got_startup.set()

        async def lifespan_receive() -> dict:
            return {"type": "lifespan.startup"}

        lifespan_task = asyncio.ensure_future(
            app({"type": "lifespan", "state": {}}, lifespan_receive, lifespan_send)
        )
        await asyncio.wait_for(got_startup.wait(), 5)
        task = asyncio.ensure_future(app(scope, receive, send))
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < max_seconds:
                await asyncio.sleep(0.02)
                bodies = [m for m in msgs if m["type"] == "http.response.body"]
                if len(bodies) >= n_frames:
                    break
        finally:
            task.cancel()
            lifespan_task.cancel()
            for t in (task, lifespan_task):
                try:
                    await t
                except BaseException:
                    pass
        return msgs

    return asyncio.run(run())


def _sse_bodies(msgs: list[dict]) -> list[bytes]:
    return [m["body"] for m in msgs if m["type"] == "http.response.body"]


def _sse_frame_count_with_timing(
    app: Any, path: str, window_seconds: float
) -> int:
    """在 window_seconds 内能收到多少帧——用于钉住推送间隔。"""
    count = 0

    async def run() -> int:
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "path": path,
            "raw_path": path.encode(), "root_path": "", "scheme": "http",
            "query_string": b"", "headers": [(b"host", b"test")],
            "client": ("testclient", 0), "server": ("testserver", 80), "state": {},
        }

        async def receive() -> dict:
            await asyncio.Event().wait()

        async def send(message: dict) -> None:
            nonlocal count
            if message["type"] == "http.response.body":
                count += 1

        got_startup = asyncio.Event()

        async def lifespan_send(message: dict) -> None:
            if message["type"] == "lifespan.startup.complete":
                got_startup.set()

        async def lifespan_receive() -> dict:
            return {"type": "lifespan.startup"}

        lifespan_task = asyncio.ensure_future(
            app({"type": "lifespan", "state": {}}, lifespan_receive, lifespan_send)
        )
        await asyncio.wait_for(got_startup.wait(), 5)
        task = asyncio.ensure_future(app(scope, receive, send))
        try:
            await asyncio.sleep(window_seconds)
        finally:
            task.cancel()
            lifespan_task.cancel()
            for t in (task, lifespan_task):
                try:
                    await t
                except BaseException:
                    pass
        return count

    return asyncio.run(run())


# ------------------------------------------------------------- overview page

def test_index_renders_overview(client):
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    poll_enabled = SettingsStore(client.app.state.db).get("poll_enabled")
    for fragment in (
        "队列深度", "今日处理", "归档", "运行中任务", "最近任务",
        "轮询模式" if poll_enabled else "仅 Webhook 触发",
        "tabular-nums", "hx-ext=\"sse\"", 'sse-connect="/api/sse/overview"',
    ):
        assert fragment in text, fragment


def test_index_empty_state(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "暂无任务" in resp.text


def test_index_redirect_gone(client):
    resp = client.get("/")
    # 不再是 307 重定向到 /config/immich
    assert resp.status_code == 200


# ------------------------------------------------------------- sse endpoint

def test_sse_content_type(client):
    msgs = _collect_sse_frames(client.app, "/api/sse/overview", n_frames=1)
    start = [m for m in msgs if m["type"] == "http.response.start"]
    assert len(start) == 1
    assert start[0]["status"] == 200
    headers = dict(start[0]["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")


def test_sse_frames_json_fields(client):
    create_job(client.app.state.db, "scan", params={"source": "test"})
    msgs = _collect_sse_frames(client.app, "/api/sse/overview", n_frames=2)
    bodies = _sse_bodies(msgs)
    assert len(bodies) >= 2
    for raw in bodies[:2]:
        assert raw.startswith(b"data: ") and raw.endswith(b"\n\n")
        payload = json.loads(raw[len(b"data: "):])
        for key in (
            "queued_jobs", "running_jobs", "processed_today",
            "failed_today", "archived_count", "running", "connections",
            "recent_jobs",
        ):
            assert key in payload, key
        assert isinstance(payload["running"], list)
        assert isinstance(payload["recent_jobs"], list)


def test_sse_no_data_no_500(client):
    """关闭轮询、无任何 job 时端点仍 200 且正常出帧。"""
    msgs = _collect_sse_frames(client.app, "/api/sse/overview", n_frames=2)
    start = [m for m in msgs if m["type"] == "http.response.start"]
    assert start and start[0]["status"] == 200
    assert len(_sse_bodies(msgs)) >= 2


def test_sse_interval_one_second(client):
    """推送间隔钉死为 ~1s：3.5s 窗口内恰好 4 帧（t=0,1,2,3）。

    间隔实现为 0.5s 时会有 7-8 帧，2s 时只有 2 帧——均被击杀。
    """
    count = _sse_frame_count_with_timing(
        client.app, "/api/sse/overview", window_seconds=3.5
    )
    assert count == 4


def test_sse_connection_status_flags(client):
    msgs = _collect_sse_frames(client.app, "/api/sse/overview", n_frames=1)
    raw = _sse_bodies(msgs)[0]
    payload = json.loads(raw[len(b"data: "):])
    assert payload["connections"] == {"immich": False, "llm": False}

    from app.store.connections import ConnectionStore, ImmichConn, LLMConn
    db = client.app.state.db
    store = ConnectionStore(db)
    store.set_immich(ImmichConn(url="https://immich.example.com", api_key="k"))
    store.set_llm(LLMConn(base_url="https://llm.example.com", api_key="k2"))
    msgs = _collect_sse_frames(client.app, "/api/sse/overview", n_frames=1)
    payload = json.loads(_sse_bodies(msgs)[0][len(b"data: "):])
    assert payload["connections"] == {"immich": True, "llm": True}


def test_sse_connection_whitespace_not_configured(client):
    """空白串 url/key 视为未配置（strip 判断）——变异专项。"""
    from app.store.connections import ConnectionStore, ImmichConn
    db = client.app.state.db
    ConnectionStore(db).set_immich(ImmichConn(url="   ", api_key="  "))
    msgs = _collect_sse_frames(client.app, "/api/sse/overview", n_frames=1)
    payload = json.loads(_sse_bodies(msgs)[0][len(b"data: "):])
    assert payload["connections"]["immich"] is False


# --------------------------------------------------- immich overview card

# 与页面上其他数字刻意错开，避免"数字恰好也出现在别处"的假通过。
_FAKE_STATS = {"images": 987654, "videos": 3211, "libraries": 4242, "tags": 5150}


class _FakeImmichClient:
    """ImmichClient 替身：三个只读方法返回固定数据。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def statistics(self) -> Any:
        return SimpleNamespace(
            images=_FAKE_STATS["images"],
            videos=_FAKE_STATS["videos"],
            total=_FAKE_STATS["images"] + _FAKE_STATS["videos"],
        )

    def list_libraries(self) -> list[Any]:
        return [SimpleNamespace(id=str(i)) for i in range(_FAKE_STATS["libraries"])]

    def list_tags(self) -> list[Any]:
        return [SimpleNamespace(name=str(i)) for i in range(_FAKE_STATS["tags"])]


def _force_immich_refresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    """清空模块级缓存，迫使下一次 payload 计算真的走一次刷新路径。"""
    from app.web.routes import overview as overview_module
    monkeypatch.setattr(
        overview_module, "_immich_overview_cache", (float("-inf"), None)
    )
    return overview_module


def test_overview_immich_card_renders_stats(client, monkeypatch):
    """mock ImmichClient → 首页出现 immich 数字卡片。"""
    from app.store.connections import ConnectionStore, ImmichConn

    overview_module = _force_immich_refresh(monkeypatch)
    monkeypatch.setattr(overview_module, "ImmichClient", _FakeImmichClient)
    ConnectionStore(client.app.state.db).set_immich(
        ImmichConn(url="https://immich.example.com", api_key="k")
    )

    resp = client.get("/")
    assert resp.status_code == 200
    for label in ("图片总数", "库数量", "标签数", "视频数"):
        assert label in resp.text, label
    for value in ("987654", "3211", "4242", "5150"):
        assert value in resp.text, value


def test_overview_immich_card_degrades_on_immich_error(client, monkeypatch):
    """ImmichError → 页面仍 200，显示未连接，绝不冒泡 500。"""
    from app.clients.immich import ImmichError
    from app.store.connections import ConnectionStore, ImmichConn

    class _BoomImmich(_FakeImmichClient):
        def statistics(self) -> Any:
            raise ImmichError("boom")

    overview_module = _force_immich_refresh(monkeypatch)
    monkeypatch.setattr(overview_module, "ImmichClient", _BoomImmich)
    ConnectionStore(client.app.state.db).set_immich(
        ImmichConn(url="https://immich.example.com", api_key="k")
    )

    resp = client.get("/")
    assert resp.status_code == 200
    assert "未连接" in resp.text


def test_overview_immich_card_unconfigured_skips_network(client, monkeypatch):
    """未配置 url/api_key 时不应构造 ImmichClient（跳过网络调用）。"""
    overview_module = _force_immich_refresh(monkeypatch)

    def _must_not_construct(**kwargs: Any) -> Any:
        raise AssertionError("未配置时不应构造 ImmichClient")

    monkeypatch.setattr(overview_module, "ImmichClient", _must_not_construct)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "未连接" in resp.text


def test_overview_immich_failure_is_cached_for_ttl(client, monkeypatch):
    """失败也必须缓存满 TTL：SSE 每秒重算 payload，绝不能每秒重试网络。"""
    from app.clients.immich import ImmichError
    from app.store.connections import ConnectionStore, ImmichConn

    calls = {"n": 0}

    class _CountingImmich(_FakeImmichClient):
        def statistics(self) -> Any:
            calls["n"] += 1
            raise ImmichError("boom")

    overview_module = _force_immich_refresh(monkeypatch)
    monkeypatch.setattr(overview_module, "ImmichClient", _CountingImmich)
    db = client.app.state.db
    ConnectionStore(db).set_immich(
        ImmichConn(url="https://immich.example.com", api_key="k")
    )

    for _ in range(4):
        overview_module._overview_payload(db)
    assert calls["n"] == 1


def test_overview_immich_success_is_cached_for_ttl(client, monkeypatch):
    """成功结果同样走缓存：TTL 内多次计算只打一次网络。"""
    from app.store.connections import ConnectionStore, ImmichConn

    calls = {"n": 0}

    class _CountingImmich(_FakeImmichClient):
        def statistics(self) -> Any:
            calls["n"] += 1
            return super().statistics()

    overview_module = _force_immich_refresh(monkeypatch)
    monkeypatch.setattr(overview_module, "ImmichClient", _CountingImmich)
    db = client.app.state.db
    ConnectionStore(db).set_immich(
        ImmichConn(url="https://immich.example.com", api_key="k")
    )

    for _ in range(3):
        overview_module._overview_payload(db)
    assert calls["n"] == 1


# --------------------------------------------------- PERF-1: sse non-blocking


def test_sse_slow_fetch_does_not_starve_loop(client, monkeypatch):
    """SSE fetch 内同步 sleep 0.3s×3 时，事件循环仍能调度其它协程。

    实现：ASGI 直接驱动 SSE 收首帧，同时并发一个 tick 协程；
    若 poll_stream 在循环线程同步调 fetch，tick 协程在 fetch 期间
    完全无法推进（progress < 3）；to_thread 化后可推进满。
    """
    import app.web.routes.overview as ov

    class _SlowImmich(_FakeImmichClient):
        def statistics(self):
            time.sleep(0.3)
            return SimpleNamespace(images=1, videos=0)

        def list_libraries(self):
            time.sleep(0.3)
            return []

        def list_tags(self):
            time.sleep(0.3)
            return []

    monkeypatch.setattr(ov, "_immich_overview_cache", (float("-inf"), None))
    monkeypatch.setattr(ov, "ImmichClient", _SlowImmich)
    from app.store.connections import ConnectionStore, ImmichConn
    ConnectionStore(client.app.state.db).set_immich(
        ImmichConn(url="https://immich.example.com", api_key="k")
    )

    async def run():
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/api/sse/overview", "raw_path": b"/api/sse/overview",
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [(b"host", b"test")],
            "client": ("testclient", 0), "server": ("testserver", 80),
            "state": {},
        }
        first_body = asyncio.Event()
        bodies = []

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])
                first_body.set()

        progress = []

        async def tick():
            while not first_body.is_set():
                await asyncio.sleep(0.02)
                progress.append(1)

        got_startup = asyncio.Event()

        async def lifespan_send(message):
            if message["type"] == "lifespan.startup.complete":
                got_startup.set()

        async def lifespan_receive():
            return {"type": "lifespan.startup"}

        app = client.app
        lifespan_task = asyncio.ensure_future(
            app({"type": "lifespan", "state": {}},
                lifespan_receive, lifespan_send)
        )
        await asyncio.wait_for(got_startup.wait(), 5)
        task = asyncio.ensure_future(app(scope, receive, send))
        tick_task = asyncio.ensure_future(tick())
        try:
            await asyncio.wait_for(first_body.wait(), 5)
        finally:
            task.cancel(); tick_task.cancel(); lifespan_task.cancel()
            for t in (task, tick_task, lifespan_task):
                try:
                    await t
                except BaseException:
                    pass
        # fetch 同步耗时约 0.9s；若不阻塞循环，tick 每 0.02s 推进一次
        assert len(progress) >= 10, (
            "事件循环在 SSE fetch 期间被饿死（未 to_thread 化）")

    asyncio.run(run())


# --------------------------------------------------- Part A: internal jobs filtered


def test_overview_queue_depth_excludes_classify_item(client):
    """scan 拆出的 classify_item 子任务不污染面向用户的队列深度/运行列表。"""
    from app.store import jobs

    db = client.app.state.db
    scan = create_job(db, "scan", {})
    jobs.finish_job(db, scan)          # 父 scan 已完成
    # 一个 running 的子任务，专门验证运行列表过滤
    running_child = create_job(db, "classify_item",
                               {"asset_id": "r", "parent_job_id": scan})
    assert jobs.claim_next(db).id == running_child   # 置 running
    # 5 个 queued 子任务，验证队列深度过滤
    for i in range(5):
        create_job(db, "classify_item",
                   {"asset_id": f"a{i}", "parent_job_id": scan})

    data = overview(db)
    assert data["queued_jobs"] == 0    # 5 个 queued 子任务不计入
    assert data["running_jobs"] == 0   # 1 个 running 子任务不计入
    assert data["running"] == []

