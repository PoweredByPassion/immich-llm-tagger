"""config_markers 路由契约：marker 规则列表、增删改启停、condition 可读化、校验。

契约：
- marker_tag_id 下拉只列 kind=marker 的标签。
- condition 两种：{"confidence_lt": float}（表单数值输入，0–1 越界中文报错）与
  {"on_refusal": true}（LLM 拒答时打标，不需要数值）。
- 指向 primary tag 的提交走 crud 的 FK/kind 校验，中文报错不落库。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.crud import (
    delete_marker_rule,
    insert_marker_rule,
    insert_tag,
    set_marker_rule_enabled,
)
from app.web.app import create_app
from app.web.routes import config_markers


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    app = create_app(
        data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))
    )
    app.include_router(config_markers.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def marker_tag(client):
    db = client.app.state.db
    return insert_tag(db, name="低置信", kind="marker", description="")


# ------------------------------------------------------------------- render

def test_markers_page_reachable_empty(client):
    html = client.get("/config/markers").text
    assert 'action="/config/markers"' in html
    assert "还没有 marker 规则" in html  # 空态
    assert "低于阈值" in html  # 语义说明
    assert 'name="condition_type"' in html  # 条件类型可选
    assert ">LLM 拒答</option>" in html


def test_markers_page_renders_rules_readable_condition(client, marker_tag):
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.8})
    html = client.get("/config/markers").text
    assert "低置信" in html
    assert "confidence &lt; 0.8" in html  # 可读化，转义后
    assert f"/config/markers/{rid}/toggle" in html
    assert f"/config/markers/{rid}/delete" in html


def test_markers_page_renders_on_refusal_condition(client, marker_tag):
    """on_refusal 规则的 condition 渲染成可读文案，而不是 confidence < None。"""
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"on_refusal": True})
    html = client.get("/config/markers").text
    assert "LLM 拒答时打标" in html
    assert "confidence &lt;" not in html
    assert f'action="/config/markers/{rid}/update"' in html


def test_markers_dropdown_only_marker_tags(client, marker_tag):
    db = client.app.state.db
    insert_tag(db, name="风景", kind="primary", description="")
    html = client.get("/config/markers").text
    assert ">低置信</option>" in html
    assert ">风景</option>" not in html


def test_markers_edit_form_prefills(client, marker_tag):
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.65})
    html = client.get("/config/markers").text
    assert f'action="/config/markers/{rid}/update"' in html
    assert 'value="0.65"' in html


# ------------------------------------------------------------------- create

def test_markers_create_ok(client, marker_tag):
    resp = client.post(
        "/config/markers", data={"marker_tag_id": str(marker_tag), "confidence_lt": "0.8"}
    )
    assert resp.status_code == 200
    assert "已保存" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        row = conn.execute("SELECT marker_tag_id, condition FROM marker_rules").fetchone()
    assert row["marker_tag_id"] == marker_tag
    assert json.loads(row["condition"]) == {"confidence_lt": 0.8}


def test_markers_create_confidence_out_of_range_rejected(client, marker_tag):
    for bad in ("1.5", "-0.1"):
        resp = client.post(
            "/config/markers",
            data={"marker_tag_id": str(marker_tag), "confidence_lt": bad},
        )
        assert resp.status_code == 200
        assert "0 到 1" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM marker_rules").fetchone()["n"] == 0


def test_markers_create_confidence_not_number_rejected(client, marker_tag):
    resp = client.post(
        "/config/markers",
        data={"marker_tag_id": str(marker_tag), "confidence_lt": "abc"},
    )
    assert resp.status_code == 200
    assert "数字" in resp.text


def test_markers_create_missing_confidence_rejected(client, marker_tag):
    resp = client.post(
        "/config/markers", data={"marker_tag_id": str(marker_tag), "confidence_lt": ""}
    )
    assert resp.status_code == 200
    assert "数字" in resp.text


def test_markers_create_on_refusal_ok(client, marker_tag):
    """on_refusal 条件不需要置信度数值，空着也照建。"""
    resp = client.post(
        "/config/markers",
        data={"marker_tag_id": str(marker_tag), "condition_type": "on_refusal",
              "confidence_lt": ""},
    )
    assert resp.status_code == 200
    assert "已保存" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        row = conn.execute("SELECT condition FROM marker_rules").fetchone()
    assert json.loads(row["condition"]) == {"on_refusal": True}


def test_markers_create_unknown_condition_type_rejected(client, marker_tag):
    resp = client.post(
        "/config/markers",
        data={"marker_tag_id": str(marker_tag), "condition_type": "bogus",
              "confidence_lt": "0.8"},
    )
    assert resp.status_code == 200
    assert "未知的 marker 条件类型" in resp.text
    db = client.app.state.db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM marker_rules").fetchone()["n"] == 0


def test_markers_create_primary_tag_rejected(client):
    db = client.app.state.db
    primary = insert_tag(db, name="风景", kind="primary", description="")
    resp = client.post(
        "/config/markers", data={"marker_tag_id": str(primary), "confidence_lt": "0.8"}
    )
    assert resp.status_code == 200
    assert "必须为 marker" in resp.text
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM marker_rules").fetchone()["n"] == 0


def test_markers_create_unknown_tag_rejected(client):
    resp = client.post(
        "/config/markers", data={"marker_tag_id": "9999", "confidence_lt": "0.8"}
    )
    assert resp.status_code == 200
    assert "不存在" in resp.text


# ------------------------------------------------------------------- update

def test_markers_update_ok(client, marker_tag):
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.8})
    resp = client.post(
        f"/config/markers/{rid}/update",
        data={"marker_tag_id": str(marker_tag), "confidence_lt": "0.55"},
    )
    assert resp.status_code == 200
    assert "已保存" in resp.text
    with db.connect() as conn:
        row = conn.execute(
            "SELECT condition FROM marker_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert json.loads(row["condition"]) == {"confidence_lt": 0.55}


def test_markers_update_out_of_range_rejected(client, marker_tag):
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.8})
    resp = client.post(
        f"/config/markers/{rid}/update",
        data={"marker_tag_id": str(marker_tag), "confidence_lt": "2"},
    )
    assert resp.status_code == 200
    assert "0 到 1" in resp.text
    with db.connect() as conn:
        row = conn.execute(
            "SELECT condition FROM marker_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert json.loads(row["condition"]) == {"confidence_lt": 0.8}  # 未被破坏


def test_markers_update_switches_condition_type(client, marker_tag):
    """修改规则可在两种条件之间来回切换，旧条件不残留。"""
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.8})

    def condition():
        with db.connect() as conn:
            return json.loads(conn.execute(
                "SELECT condition FROM marker_rules WHERE id = ?", (rid,)
            ).fetchone()["condition"])

    client.post(f"/config/markers/{rid}/update",
                data={"marker_tag_id": str(marker_tag),
                      "condition_type": "on_refusal", "confidence_lt": "0.8"})
    assert condition() == {"on_refusal": True}

    client.post(f"/config/markers/{rid}/update",
                data={"marker_tag_id": str(marker_tag),
                      "condition_type": "confidence_lt", "confidence_lt": "0.3"})
    assert condition() == {"confidence_lt": 0.3}


# ------------------------------------------------------------------- toggle

def test_markers_toggle_roundtrip(client, marker_tag):
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.8})
    client.post(f"/config/markers/{rid}/toggle")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM marker_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert row["enabled"] == 0
    resp = client.post(f"/config/markers/{rid}/toggle")
    assert resp.status_code == 200
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM marker_rules WHERE id = ?", (rid,)
        ).fetchone()
    assert row["enabled"] == 1


# ------------------------------------------------------------------- delete

def test_markers_delete_removes_row(client, marker_tag):
    db = client.app.state.db
    rid = insert_marker_rule(db, marker_tag_id=marker_tag, condition={"confidence_lt": 0.8})
    resp = client.post(f"/config/markers/{rid}/delete")
    assert resp.status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM marker_rules").fetchone()["n"] == 0
