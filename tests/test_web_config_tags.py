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


def test_tag_create_write_flags_default_on_when_form_has_no_marker(client):
    """不带 write_flags 隐藏位（老表单 / 程序化提交）→ 保持默认「写」。

    这两个开关默认是开的，缺键绝不能解释成"取消勾选"——否则一次不带这俩字段的
    提交就会把标签静默改成不写评分/描述。
    """
    c, db = client
    resp = c.post("/config/tags", data={"name": "t", "kind": "primary"})
    assert resp.status_code == 200
    row = _rows(db, "SELECT write_rating, write_description FROM tags")[0]
    assert (row["write_rating"], row["write_description"]) == (1, 1)


def test_tag_create_unchecked_write_flags_land_as_zero(client):
    """新表单带隐藏位：未勾选 = 明确不写。"""
    c, db = client
    resp = c.post("/config/tags", data={
        "name": "t", "kind": "primary", "write_flags": "1",
        "write_description": "1",          # 只勾了描述
    })
    assert resp.status_code == 200
    row = _rows(db, "SELECT write_rating, write_description FROM tags")[0]
    assert (row["write_rating"], row["write_description"]) == (0, 1)


def test_tag_update_toggles_write_flags_both_ways(client):
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary")

    def flags():
        row = _rows(db, "SELECT write_rating, write_description FROM tags")[0]
        return (row["write_rating"], row["write_description"])

    c.post(f"/config/tags/{tid}", data={"name": "t", "kind": "primary",
                                        "write_flags": "1"})
    assert flags() == (0, 0)

    c.post(f"/config/tags/{tid}", data={"name": "t", "kind": "primary",
                                        "write_flags": "1", "write_rating": "1",
                                        "write_description": "1"})
    assert flags() == (1, 1)


def test_tag_update_without_form_marker_keeps_flags(client):
    """没有隐藏位的提交（旧 HTML 的长开标签页）不得把开关关掉。"""
    c, db = client
    tid = crud.insert_tag(db, name="t", kind="primary", write_rating=True,
                          write_description=True)
    c.post(f"/config/tags/{tid}", data={"name": "t", "kind": "primary"})
    row = _rows(db, "SELECT write_rating, write_description FROM tags")[0]
    assert (row["write_rating"], row["write_description"]) == (1, 1)


def test_tags_page_renders_write_checkboxes(client):
    """创建表单默认两个都勾选；已关掉的标签在编辑表单里不勾选、列表挂徽标。"""
    import re
    c, db = client
    crud.insert_tag(db, name="截图", kind="primary", write_rating=False)
    html = c.get("/config/tags").text
    assert 'id="new_write_rating" name="write_rating" value="1" checked' in html
    assert 'id="new_write_description" name="write_description" value="1" checked' in html
    # 编辑表单：write_rating 未勾选（value 后直接跟 class，没有 checked）
    assert re.search(r'name="write_rating" value="1"\s+class="h-4', html)
    assert "不写评分" in html
    assert "不写描述" not in html          # 描述仍为默认（写），不挂徽标


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
