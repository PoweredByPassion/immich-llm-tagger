"""config_exclusions 路由契约：排除规则列表渲染、增删启停全路径、校验错误中文回显。

校验契约（app/store/crud.py exclusions 段）：
- type ∈ {album, path, tag, reviewed}，非法 type 报错不落库。
- type=reviewed 无 value（value 强制存空串）。
- type=tag 时 value 必须是 tags 表中已存在的标签名。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.crud import (
    delete_exclusion,
    insert_exclusion,
    insert_tag,
    set_exclusion_enabled,
)
from app.web.app import create_app


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    app = create_app(
        data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))
    )
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------- render

def test_exclusions_page_reachable_empty(client):
    html = client.get("/config/scope").text
    assert 'action="/config/exclusions"' in html  # 新增表单存在
    assert "还没有排除规则" in html  # 空态
    assert "排除的图片不会进入自动分类" in html


def test_exclusions_legacy_url_redirects_to_merged_page(client):
    """排除规则页已并入 /config/scope；旧 URL 保留 302，老书签不致死。"""
    resp = client.get("/config/exclusions", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/config/scope"


def test_exclusions_page_renders_rules(client):
    db = client.app.state.db
    rid = insert_exclusion(db, type="path", value="/photos/private")
    html = client.get("/config/scope").text
    assert "/photos/private" in html
    assert f"/config/exclusions/{rid}/toggle" in html
    assert f"/config/exclusions/{rid}/delete" in html
    # 类型徽章（中文文案）
    assert ">路径</span>" in html


def test_exclusions_reviewed_row_has_no_value(client):
    db = client.app.state.db
    insert_exclusion(db, type="reviewed", value="")
    html = client.get("/config/scope").text
    assert ">reviewed<" in html
    assert "已复核" in html


def test_exclusions_type_dropdown_lists_all_tags(client):
    db = client.app.state.db
    insert_tag(db, name="风景", kind="primary", description="")
    insert_tag(db, name="低置信", kind="marker", description="")
    html = client.get("/config/scope").text
    assert ">风景</option>" in html  # tag 下拉列全部标签（不分 kind）
    assert ">低置信</option>" in html


# ------------------------------------------------------------------- create

def test_exclusions_create_path_ok(client):
    resp = client.post(
        "/config/exclusions", data={"type": "path", "value": "/photos/cat"}
    )
    assert resp.status_code == 200
    assert "/photos/cat" in resp.text
    assert "已保存" in resp.text


def test_exclusions_create_reviewed_without_value(client):
    resp = client.post("/config/exclusions", data={"type": "reviewed", "value": ""})
    assert resp.status_code == 200
    assert "已保存" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        row = conn.execute("SELECT type, value FROM exclusions").fetchone()
    assert row["type"] == "reviewed"
    assert row["value"] == ""


def test_exclusions_create_tag_existing(client):
    db = client.app.state.db
    insert_tag(db, name="截图", kind="primary", description="")
    resp = client.post(
        "/config/exclusions", data={"type": "tag", "value": "截图"}
    )
    assert resp.status_code == 200
    assert "已保存" in resp.text


def test_exclusions_create_tag_missing_rejected(client):
    resp = client.post("/config/exclusions", data={"type": "tag", "value": "不存在"})
    assert resp.status_code == 200
    assert "不存在" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM exclusions").fetchone()["n"] == 0


def test_exclusions_create_invalid_type_rejected(client):
    resp = client.post(
        "/config/exclusions", data={"type": "library", "value": "lib-1"}
    )
    assert resp.status_code == 200
    assert "排除类型必须" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM exclusions").fetchone()["n"] == 0


def test_exclusions_create_empty_value_rejected(client):
    resp = client.post("/config/exclusions", data={"type": "album", "value": "  "})
    assert resp.status_code == 200
    assert "不能为空" in resp.text


# ------------------------------------------------------------------- toggle

def test_exclusions_toggle_roundtrip(client):
    db = client.app.state.db
    rid = insert_exclusion(db, type="path", value="/x")
    client.post(f"/config/exclusions/{rid}/toggle")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM exclusions WHERE id = ?", (rid,)
        ).fetchone()
    assert row["enabled"] == 0
    resp = client.post(f"/config/exclusions/{rid}/toggle")
    assert resp.status_code == 200
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM exclusions WHERE id = ?", (rid,)
        ).fetchone()
    assert row["enabled"] == 1


# ------------------------------------------------------------------- delete

def test_exclusions_delete_removes_row(client):
    db = client.app.state.db
    rid = insert_exclusion(db, type="path", value="/x")
    resp = client.post(f"/config/exclusions/{rid}/delete")
    assert resp.status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM exclusions").fetchone()["n"] == 0
