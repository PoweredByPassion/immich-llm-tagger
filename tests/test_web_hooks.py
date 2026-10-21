"""POST /api/hooks/immich —— webhook 接收端点契约（M3 Task 3, §5.1）。

契约矩阵（计划 §9）：标准 WorkflowEventPayload（data.asset.id）· 只转发 data
（asset.id）· assetId · id · 数组 · 非 JSON（200 + parse_fail，不回 400 ——
Immich 不在乎响应码，原始留存就是为了缓冲 Immich 版本变化）· VIDEO 跳过 ·
无 id 兜底 scan。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.db.connection import Database
from app.db.migrate import migrate
from app.store.events import tail_events
from app.store.jobs import get_job
from app.store.settings import SettingsStore
from app.web.app import create_app


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def ctx(data_dir: Path):
    """启动 app 后返回 (client, token, db_path)。token 走首次生成逻辑。

    M3 Task 7 起 lifespan 会启动 poll scheduler（默认 enabled，首 tick 即
    enqueue scan）；本文件只测 webhook 契约，预置关闭轮询避免计数被污染。
    """
    import app.web.routes.hooks as hooks_mod
    hooks_mod._last_fallback_scan_ts = 0.0  # 模块级冷却状态在用例间重置
    pre = Database(BootstrapConfig(data_dir=str(data_dir)).db_path)
    migrate(pre)
    SettingsStore(pre).set("poll_enabled", False)
    with TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    ) as client:
        db = client.app.state.db
        token = SettingsStore(db).webhook_token()
        yield client, token, db


def _post(client: TestClient, token: str, payload) -> TestClient.__class__.__mro__[0]:
    headers = {"x-webhook-token": token}
    if isinstance(payload, bytes):
        return client.post("/api/hooks/immich", content=payload, headers=headers)
    return client.post("/api/hooks/immich", json=payload, headers=headers)


def _events(db, event_type: str) -> list[dict]:
    return [e for e in tail_events(db, limit=200) if e["event_type"] == event_type]


def _job_params(db) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT params FROM jobs WHERE type = 'single'"
        ).fetchall()
    return [json.loads(r["params"]) for r in rows]


def _scan_count(db) -> int:
    with db.connect() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'scan'"
        ).fetchone()
    return n


_ASSET = {
    "id": "asset-1",
    "type": "IMAGE",
    "originalFileName": "IMG_2043.HEIC",
    "originalPath": "/icloud/a.jpg",
    "libraryId": "lib-1",
    "exifInfo": {"make": "Apple"},
    "tags": [{"id": "t1", "name": "未分类"}],
}

# ---------------------------------------------------------------- 鉴权


def test_401_without_header(ctx):
    client, _, db = ctx
    resp = client.post("/api/hooks/immich", json={"data": {"asset": _ASSET}})
    assert resp.status_code == 401
    assert _scan_count(db) == 0


def test_401_with_wrong_token(ctx):
    client, _, db = ctx
    resp = client.post(
        "/api/hooks/immich",
        json={"data": {"asset": _ASSET}},
        headers={"x-webhook-token": "nope"},
    )
    assert resp.status_code == 401


def test_first_request_generates_token_and_accepts(ctx):
    """空配置 token：settings 首次生成后即可用（读 webhook_token 复用）。"""
    client, token, _ = ctx
    assert token
    resp = _post(client, token, {"data": {"asset": _ASSET}})
    assert resp.status_code == 200
    assert len(_job_params(ctx[2])) == 1


# ---------------------------------------------------------------- 契约矩阵


def test_standard_payload_data_asset_id(ctx):
    client, token, db = ctx
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    params = _job_params(db)
    assert params == [
        {
            "asset_id": "asset-1",
            "source": "webhook",
            "asset_hint": {
                "originalFileName": "IMG_2043.HEIC",
                "exifInfo": {"make": "Apple"},
                "libraryId": "lib-1",
                "originalPath": "/icloud/a.jpg",
                "tags": [{"id": "t1", "name": "未分类"}],
                "type": "IMAGE",
            },
        }
    ]


def test_data_only_payload_asset_id(ctx):
    client, token, db = ctx
    assert _post(client, token, {"asset": _ASSET}).status_code == 200
    assert [p["asset_id"] for p in _job_params(db)] == ["asset-1"]


def test_asset_id_form(ctx):
    client, token, db = ctx
    assert _post(client, token, {"assetId": "asset-1"}).status_code == 200
    assert [p["asset_id"] for p in _job_params(db)] == ["asset-1"]
    assert _job_params(db)[0]["asset_hint"] == {}


def test_bare_id_form(ctx):
    client, token, db = ctx
    assert _post(client, token, {"id": "asset-1"}).status_code == 200
    assert [p["asset_id"] for p in _job_params(db)] == ["asset-1"]


def test_array_of_assets(ctx):
    client, token, db = ctx
    a2 = {**_ASSET, "id": "asset-2"}
    assert _post(client, token, {"data": {"asset": [_ASSET, a2]}}).status_code == 200
    assert [p["asset_id"] for p in _job_params(db)] == ["asset-1", "asset-2"]


def test_non_json_body_returns_200_with_parse_fail(ctx):
    client, token, db = ctx
    resp = _post(client, token, b"not json at all")
    assert resp.status_code == 200
    assert len(_events(db, "parse_fail")) == 1
    # 无 id → 兜底 scan
    assert _scan_count(db) == 1
    assert len(_events(db, "fallback_scan_triggered")) == 1


def test_video_skipped(ctx):
    client, token, db = ctx
    video = {**_ASSET, "type": "VIDEO"}
    assert _post(client, token, {"data": {"asset": video}}).status_code == 200
    assert len(_job_params(db)) == 0
    assert len(_events(db, "asset_skipped")) == 1


def test_no_id_falls_back_to_scan(ctx):
    client, token, db = ctx
    assert _post(client, token, {"trigger": "AssetCreate"}).status_code == 200
    assert _scan_count(db) == 1
    assert len(_events(db, "fallback_scan_triggered")) == 1


# ---------------------------------------------------------------- 守卫


def test_in_flight_dedup(ctx):
    client, token, db = ctx
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    # 第二次投递：已有 queued single → 不新建
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 1
    assert len(_events(db, "asset_deduped")) == 1


def test_in_flight_dedup_includes_running(ctx):
    """去重查询必须把 running 也算进去（不只是 queued）。"""
    from app.store.jobs import claim_next, create_job

    client, token, db = ctx
    create_job(db, "single", params={"asset_id": "asset-1", "source": "webhook", "asset_hint": {}})
    assert claim_next(db) is not None  # queued → running
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 1


def test_completed_record_skipped(ctx):
    client, token, db = ctx
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('asset-1', 'webhook', 'ok', 0, '2026-01-01 00:00:01')"
        )
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 0
    assert len(_events(db, "asset_skipped")) == 1


def test_record_without_applied_at_is_retried(ctx):
    """同谓词的第二份拷贝（hooks._has_completed_record）也必须改口径：
    apply 失败的资产在 webhook 路径上不能被判"已完成"而在入队前丢掉。"""
    client, token, db = ctx
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run) "
            "VALUES ('asset-1', 'webhook', 'ok', 0)"
        )
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 1


def test_dry_run_record_does_not_skip(ctx):
    client, token, db = ctx
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run) "
            "VALUES ('asset-1', 'webhook', 'ok', 1)"
        )
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 1


def test_out_of_scope_not_enqueued(ctx):
    client, token, db = ctx
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO scope_rules (library_id, path_prefix, enabled) "
            "VALUES ('other-lib', '', 1)"
        )
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 0
    assert len(_events(db, "asset_out_of_scope")) == 1


def test_in_scope_by_library(ctx):
    client, token, db = ctx
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO scope_rules (library_id, path_prefix, enabled) "
            "VALUES ('lib-1', '', 1)"
        )
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 1


def test_no_enabled_rules_means_all_in_scope(ctx):
    """无启用规则 = 全在范围（§5.3，与 M2 真机行为一致）。"""
    client, token, db = ctx
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO scope_rules (library_id, path_prefix, enabled) "
            "VALUES ('other-lib', '', 0)"
        )
    assert _post(client, token, {"data": {"asset": _ASSET}}).status_code == 200
    assert len(_job_params(db)) == 1


def test_fallback_scan_cooldown(ctx):
    client, token, db = ctx
    assert _post(client, token, {"no": "id"}).status_code == 200
    assert _scan_count(db) == 1
    # 30s 冷却窗口内第二次 → 丢弃
    assert _post(client, token, {"no": "id"}).status_code == 200
    assert _scan_count(db) == 1
    assert len(_events(db, "fallback_scan_cooldown")) == 1


def test_webhook_disabled_logs_but_does_not_enqueue(ctx):
    client, token, db = ctx
    SettingsStore(db).set("webhook_enabled", False)
    resp = _post(client, token, {"data": {"asset": _ASSET}})
    assert resp.status_code == 200
    assert len(_job_params(db)) == 0
    assert len(_events(db, "webhook_disabled")) == 1


def test_raw_payload_always_logged(ctx):
    """无论能否解析，原始 payload 都落 events。"""
    client, token, db = ctx
    _post(client, token, {"data": {"asset": _ASSET}})
    _post(client, token, b"garbage")
    assert len(_events(db, "payload_received")) == 1
    assert len(_events(db, "parse_fail")) == 1


# --------------------------------------------------- PERF-1: webhook non-blocking


def test_webhook_does_not_block_loop_on_slow_db(tmp_path, monkeypatch):
    """webhook 处理路径中的同步慢调用不得饿死事件循环。

    把 _has_active_single 换成 sleep 0.3s 的慢函数；同环 ASGI 驱动
    webhook POST，并发 tick 协程。若 immich_hook 的同步段在事件循环
    线程直接执行，tick 在处理期间完全无法推进。
    """
    import app.web.routes.hooks as hooks_mod

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir,
                     bootstrap=BootstrapConfig(data_dir=str(data_dir)))

    # 拿 token：直接读 db（不走 TestClient）
    db = Database(BootstrapConfig(data_dir=str(data_dir)).db_path)
    migrate(db)
    SettingsStore(db).set("poll_enabled", False)
    token = SettingsStore(db).webhook_token()

    orig = hooks_mod._has_active_single

    def slow_has_active_single(db, asset_id):
        time.sleep(0.3)
        return orig(db, asset_id)

    monkeypatch.setattr(hooks_mod, "_has_active_single",
                        slow_has_active_single)

    async def run():
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "path": "/api/hooks/immich", "raw_path": b"/api/hooks/immich",
            "root_path": "", "scheme": "http", "query_string": b"",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
                (b"x-webhook-token", token.encode()),
            ],
            "client": ("testclient", 0), "server": ("testserver", 80),
            "state": {},
        }
        body = json.dumps(
            {"asset": {"id": "slow-1", "type": "IMAGE"}}).encode()

        done = asyncio.Event()
        chunks: list[bytes] = []

        async def receive():
            if chunks:
                return {"type": "http.disconnect"}
            chunks.append(b"")
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                done.set()

        progress = []

        async def tick():
            while not done.is_set():
                await asyncio.sleep(0.02)
                progress.append(1)

        got_startup = asyncio.Event()

        async def lifespan_send(message):
            if message["type"] == "lifespan.startup.complete":
                got_startup.set()

        async def lifespan_receive():
            return {"type": "lifespan.startup"}

        lifespan_task = asyncio.ensure_future(
            app({"type": "lifespan", "state": {}},
                lifespan_receive, lifespan_send)
        )
        await asyncio.wait_for(got_startup.wait(), 5)
        task = asyncio.ensure_future(app(scope, receive, send))
        tick_task = asyncio.ensure_future(tick())
        try:
            await asyncio.wait_for(done.wait(), 10)
        finally:
            task.cancel(); tick_task.cancel(); lifespan_task.cancel()
            for t in (task, tick_task, lifespan_task):
                try:
                    await t
                except BaseException:
                    pass
        return progress

    progress = asyncio.run(run())
    assert len(progress) >= 10, (
        f"事件循环在 webhook 处理期间被饿死（仅推进 {len(progress)} 次）")
