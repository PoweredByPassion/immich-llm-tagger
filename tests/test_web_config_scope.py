"""config_scope 路由契约：规则列表渲染、新增正反例、启停往返、删除、空列表引导。

校验契约：library_id 与 path_prefix 至少一个非空，否则 ValidationError，
problems 中文描述回显在页面上（错误横幅），不落库。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.clients.immich import ImmichError
from app.config import BootstrapConfig
from app.store.connections import ConnectionStore, ImmichConn
from app.store.crud import (
    delete_scope_rule,
    insert_scope_rule,
    set_scope_rule_enabled,
)
from app.web.app import create_app
from app.web.routes import config_scope


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    app = create_app(
        data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))
    )
    app.include_router(config_scope.router)
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------- routes

def test_scope_page_reachable_empty(client):
    html = client.get("/config/scope").text
    assert html.find('action="/config/scope"') != -1  # 新增表单存在
    # 空列表引导文案：解释规则语义与 fallback 行为
    assert "library" in html
    assert "path_prefix" in html
    assert "都视为在处理范围内" in html  # 无启用规则 → 不拦截（fallback）


def test_scope_page_renders_rules(client):
    db = client.app.state.db
    rid = insert_scope_rule(db, library_id="lib-1", path_prefix="/photos")
    html = client.get("/config/scope").text
    assert "lib-1" in html
    assert "/photos" in html
    assert f"/config/scope/{rid}/toggle" in html
    assert f"/config/scope/{rid}/delete" in html


# ------------------------------------------------------------------- create

def test_scope_create_inserts_and_shows_row(client):
    resp = client.post(
        "/config/scope", data={"library_id": "lib-9", "path_prefix": "/photos/new"}
    )
    assert resp.status_code == 200
    assert "lib-9" in resp.text
    assert "/photos/new" in resp.text
    # 确实落库
    html = client.get("/config/scope").text
    assert "lib-9" in html


def test_scope_create_both_empty_rejected(client):
    resp = client.post("/config/scope", data={"library_id": "", "path_prefix": "  "})
    assert resp.status_code == 200
    # 错误回显，且列表区无任何规则行（未落库）
    assert "至少" in resp.text or "非空" in resp.text
    assert "/toggle" not in resp.text  # 列表区无规则行（未落库）


def test_scope_create_error_not_persisted(client):
    client.post("/config/scope", data={"library_id": "", "path_prefix": ""})
    db = client.app.state.db
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM scope_rules").fetchone()["n"]
    assert count == 0


# ------------------------------------------------------------------- toggle

def test_scope_toggle_roundtrip(client):
    db = client.app.state.db
    rid = insert_scope_rule(db, library_id="lib-1", path_prefix="")
    # 默认 enabled=1 → 关
    client.post(f"/config/scope/{rid}/toggle")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM scope_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert row["enabled"] == 0
    # 再开
    client.post(f"/config/scope/{rid}/toggle")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM scope_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert row["enabled"] == 1


def test_scope_toggle_renders_updated_state(client):
    db = client.app.state.db
    rid = insert_scope_rule(db, library_id="lib-1", path_prefix="")
    resp = client.post(f"/config/scope/{rid}/toggle")
    assert resp.status_code == 200
    assert "已停用" in resp.text


# ------------------------------------------------------------------- delete

def test_scope_delete_removes_row(client):
    db = client.app.state.db
    rid = insert_scope_rule(db, library_id="lib-1", path_prefix="")
    resp = client.post(f"/config/scope/{rid}/delete")
    assert resp.status_code == 200
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM scope_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert row is None
    assert "lib-1" not in client.get("/config/scope").text


def test_scope_delete_missing_id_is_safe(client):
    resp = client.post("/config/scope/9999/delete")
    assert resp.status_code == 200


# ------------------------------------------------- M6 Task 1: 全量回填按钮

def test_scope_page_has_backfill_button(client):
    html = client.get("/config/scope").text
    assert 'action="/config/scope/backfill"' in html
    assert "全量回填" in html
    # 二次确认文案（不带数字版决策）
    assert "将为范围内未处理图片入队" in html
    assert "下次触发" in html


def test_scope_backfill_post_creates_job(client):
    resp = client.post("/config/scope/backfill", follow_redirects=False)
    assert resp.status_code == 303
    db = client.app.state.db
    from app.store import jobs as jobs_store
    active = [j for j in jobs_store.list_jobs(db) if j.type == "backfill"]
    assert len(active) == 1
    assert f"/jobs/{active[0].id}" == resp.headers["location"]


def test_scope_backfill_button_disabled_when_active(client):
    from app.store import jobs as jobs_store
    db = client.app.state.db
    jobs_store.create_job(db, "backfill", {})
    html = client.get("/config/scope").text
    assert "disabled" in html
    # 防重：仍可 POST（引擎侧由唯一性约束兜底），但 UI 置灰


# --------------------------------- 任务 E：库/路径下拉（服务端渲染注入）

LIB_A = "11111111-1111-1111-1111-111111111111"
LIB_B = "22222222-2222-2222-2222-222222222222"


class _FakeImmichClient:
    """最小替身：只实现 scope 页用到的 list_libraries（签名兼容构造）。"""

    def __init__(self, **kw):
        pass

    def list_libraries(self):
        return [
            SimpleNamespace(id=LIB_A, name="Photos", import_paths=["/photos", "/shared"]),
            SimpleNamespace(id=LIB_B, name="Archive", import_paths=["/shared", "/archive"]),
        ]


def _configure_immich(client):
    ConnectionStore(client.app.state.db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )


def test_scope_page_renders_library_select_and_datalist(client, monkeypatch):
    _configure_immich(client)
    monkeypatch.setattr(config_scope, "ImmichClient", _FakeImmichClient)
    html = client.get("/config/scope").text
    # library_id 变下拉，选项含库名与库 ID
    assert '<select id="library_id"' in html
    assert f'value="{LIB_A}"' in html
    assert "Photos" in html
    assert "其他（手动输入）" in html  # 保留手动输入逃生口
    # 页面根元素带 id（htmx hx-target="#scope-page" 的落点）
    assert 'id="scope-page"' in html
    # path_prefix 的 datalist：全量聚合 + 按库联动两份
    assert '<datalist id="import-paths-all">' in html
    assert '<datalist id="import-paths-lib">' in html
    # lib_paths 联动数据：每库各自的路径
    assert 'libPaths' in html
    assert '<option value="/photos">' in html
    assert '<option value="/archive">' in html
    assert html.count('<option value="/shared">') == 1  # 两个库都含 /shared → 去重


def test_scope_page_immich_failure_falls_back_to_text_inputs(client, monkeypatch):
    _configure_immich(client)

    class _BoomClient:
        def __init__(self, **kw):
            pass

        def list_libraries(self):
            raise ImmichError("403 无权限")

    monkeypatch.setattr(config_scope, "ImmichClient", _BoomClient)
    html = client.get("/config/scope").text
    assert '<select id="library_id"' not in html
    assert '<input type="text" id="library_id"' in html
    # 降级时不渲染库选项；空 datalist 无碍但不应有路径选项
    assert '<option value="/photos">' not in html


def test_scope_page_no_libraries_falls_back_to_text_inputs(client, monkeypatch):
    _configure_immich(client)

    class _EmptyClient:
        def __init__(self, **kw):
            pass

        def list_libraries(self):
            return []

    monkeypatch.setattr(config_scope, "ImmichClient", _EmptyClient)
    html = client.get("/config/scope").text
    assert '<select id="library_id"' not in html
    assert '<input type="text" id="library_id"' in html


def test_scope_page_unconfigured_falls_back_to_text_inputs(client):
    # 未配置 Immich 连接：不构造客户端，直接文本输入（不联网）
    html = client.get("/config/scope").text
    assert '<select id="library_id"' not in html
    assert '<input type="text" id="library_id"' in html


def test_scope_create_still_works_with_dropdown(client, monkeypatch):
    # 字段名不变：提交下拉选项值时正常落库（scope_create 无需改动）
    _configure_immich(client)
    monkeypatch.setattr(config_scope, "ImmichClient", _FakeImmichClient)
    resp = client.post(
        "/config/scope", data={"library_id": LIB_B, "path_prefix": ""}
    )
    assert resp.status_code == 200
    with client.app.state.db.connect() as conn:
        row = conn.execute("SELECT library_id FROM scope_rules").fetchone()
    assert row["library_id"] == LIB_B


def test_scope_create_re_render_keeps_dropdown(client, monkeypatch):
    """POST 添加后 htmx 换回的页面仍是下拉（不再退化为文本输入）。"""
    _configure_immich(client)
    monkeypatch.setattr(config_scope, "ImmichClient", _FakeImmichClient)
    resp = client.post(
        "/config/scope", data={"library_id": LIB_A, "path_prefix": "/photos"}
    )
    assert resp.status_code == 200
    assert '<select id="library_id"' in resp.text
    assert 'id="scope-page"' in resp.text
