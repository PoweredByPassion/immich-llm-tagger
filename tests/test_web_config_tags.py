"""config_tags 路由契约：标签/分类 CRUD、空串→NULL、kind 白名单、FK 删除保护、锚定校验。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store import crud
from app.web.app import create_app


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        yield c, c.app.state.db


def _rows(db, sql):
    with db.connect() as conn:
        return conn.execute(sql).fetchall()


# ------------------------------------------------------------------ tags CRUD

def test_tags_page_reachable_and_empty_state(client):
    c, _ = client
    resp = c.get("/config/tags")
    assert resp.status_code == 200
    assert "还没有标签" in resp.text


def test_tag_create_round_trip(client):
    c, db = client
    resp = c.post("/config/tags", data={
        "name": "风景", "description": "自然风光", "kind": "primary",
        "archive": "1", "album": "scenery-album", "min_confidence": "0.85",
    })
    assert resp.status_code == 200
    assert "已创建" in resp.text
    rows = _rows(db, "SELECT * FROM tags")
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "风景"
    assert row["description"] == "自然风光"
    assert row["kind"] == "primary"
    assert row["archive"] == 1
    assert row["album"] == "scenery-album"
    assert row["min_confidence"] == pytest.approx(0.85)
    assert row["enabled"] == 1


def test_tag_create_empty_strings_become_null(client):
    c, db = client
    resp = c.post("/config/tags", data={
        "name": "t", "description": "", "kind": "marker",
        "album": "", "min_confidence": "",
    })
    assert resp.status_code == 200
    rows = _rows(db, "SELECT album, min_confidence, archive FROM tags")
    assert rows[0]["album"] is None
    assert rows[0]["min_confidence"] is None
    assert rows[0]["archive"] == 0


def test_tag_create_invalid_kind_rejected(client):
    c, db = client
    resp = c.post("/config/tags", data={
        "name": "t", "kind": "bogus", "album": "", "min_confidence": "",
    })
    assert resp.status_code == 200
    assert "出错了" in resp.text
    assert "primary" in resp.text
    assert _rows(db, "SELECT * FROM tags") == []


def test_tag_create_min_confidence_not_number_echoes_error(client):
    c, db = client
    resp = c.post("/config/tags", data={
        "name": "t", "kind": "primary", "min_confidence": "abc",
    })
    assert resp.status_code == 200
    assert "出错了" in resp.text
    assert "min_confidence" in resp.text
    assert _rows(db, "SELECT * FROM tags") == []


def test_tag_toggle_disable_then_enable_is_symmetric(client):
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary")
    c.post(f"/config/tags/{tid}/toggle", data={"enabled": ""})
    c.post(f"/config/tags/{tid}/toggle", data={"enabled": "1"})
    c.post(f"/config/tags/{tid}/toggle", data={"enabled": ""})
    with db.connect() as conn:
        row = conn.execute("SELECT enabled FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["enabled"] == 0
    c.post(f"/config/tags/{tid}/toggle", data={"enabled": "1"})
    c.post(f"/config/tags/{tid}/toggle", data={"enabled": "1"})
    c.post(f"/config/tags/{tid}/toggle", data={"enabled": "1"})
    with db.connect() as conn:
        row = conn.execute("SELECT enabled FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["enabled"] == 1


def test_tag_update(client):
    c, db = client
    tid = crud.insert_tag(db, name="old", kind="primary")
    resp = c.post(f"/config/tags/{tid}", data={
        "name": "new", "description": "d", "kind": "marker",
        "archive": "1", "album": "a1", "min_confidence": "0.5",
        "enabled": "1",
    })
    assert resp.status_code == 200
    assert "已保存" in resp.text
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["name"] == "new"
    assert row["kind"] == "marker"
    assert row["archive"] == 1
    assert row["album"] == "a1"
    assert row["min_confidence"] == pytest.approx(0.5)
    assert row["enabled"] == 1


def test_tag_update_clears_fields_to_null(client):
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary", album="a", min_confidence=0.5)
    c.post(f"/config/tags/{tid}", data={
        "name": "t2", "kind": "primary", "album": "", "min_confidence": "",
        "enabled": "1",
    })
    with db.connect() as conn:
        row = conn.execute("SELECT album, min_confidence FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["album"] is None
    assert row["min_confidence"] is None


def test_tag_update_invalid_kind_rejected_and_not_persisted(client):
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary")
    resp = c.post(f"/config/tags/{tid}", data={
        "name": "t", "kind": "archive", "album": "", "min_confidence": "",
    })
    assert resp.status_code == 200
    assert "出错了" in resp.text
    with db.connect() as conn:
        row = conn.execute("SELECT kind FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["kind"] == "primary"


def test_tag_toggle_disable_shows_spec_notice(client):
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary")
    resp = c.post(f"/config/tags/{tid}/toggle", data={"enabled": ""})
    assert resp.status_code == 200
    assert "已标记的历史图片不受影响" in resp.text
    with db.connect() as conn:
        row = conn.execute("SELECT enabled FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["enabled"] == 0
    resp2 = c.post(f"/config/tags/{tid}/toggle", data={"enabled": "1"})
    assert resp2.status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT enabled FROM tags WHERE id = ?", (tid,)).fetchone()
    assert row["enabled"] == 1


def test_tag_delete(client):
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary")
    resp = c.post(f"/config/tags/{tid}/delete")
    assert resp.status_code == 200
    assert _rows(db, "SELECT * FROM tags") == []


def test_tag_duplicate_name_shows_error(client):
    c, db = client
    crud.insert_tag(db, name="dup", kind="primary")
    resp = c.post("/config/tags", data={
        "name": "dup", "kind": "primary", "album": "", "min_confidence": "",
    })
    assert resp.status_code == 200
    assert "出错了" in resp.text
    assert "已存在" in resp.text
