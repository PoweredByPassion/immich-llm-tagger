import sqlite3
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store import crud
from app.store.errors import ValidationError


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


# ---------- scope_rules ----------

def test_scope_rule_insert_update_delete(db: Database) -> None:
    rid = crud.insert_scope_rule(db, library_id="lib1", path_prefix="/a")
    assert rid > 0
    crud.update_scope_rule(db, rid, library_id="lib2", path_prefix="/b")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT library_id, path_prefix, enabled FROM scope_rules WHERE id=?",
            (rid,),
        ).fetchone()
    assert row["library_id"] == "lib2"
    assert row["path_prefix"] == "/b"
    assert row["enabled"] == 1

    crud.delete_scope_rule(db, rid)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM scope_rules WHERE id=?", (rid,)
        ).fetchone() is None


def test_scope_rule_set_enabled_roundtrip(db: Database) -> None:
    rid = crud.insert_scope_rule(db, library_id="", path_prefix="/x")
    crud.set_scope_rule_enabled(db, rid, False)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM scope_rules WHERE id=?", (rid,)
        ).fetchone()["enabled"] == 0
    crud.set_scope_rule_enabled(db, rid, True)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM scope_rules WHERE id=?", (rid,)
        ).fetchone()["enabled"] == 1


# ---------- tags ----------

def test_tag_write_flags_default_on_and_roundtrip(db: Database) -> None:
    """write_rating / write_description（v12）：默认开，可单独关。"""
    tid = crud.insert_tag(db, name="风景", kind="primary")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT write_rating, write_description FROM tags WHERE id=?", (tid,)
        ).fetchone()
    assert (row["write_rating"], row["write_description"]) == (1, 1)

    crud.update_tag(
        db, tid, name="风景", kind="primary", description="", archive=False,
        album=None, min_confidence=None, enabled=True,
        write_rating=False, write_description=True,
    )
    with db.connect() as conn:
        row = conn.execute(
            "SELECT write_rating, write_description FROM tags WHERE id=?", (tid,)
        ).fetchone()
    assert (row["write_rating"], row["write_description"]) == (0, 1)


def test_load_tags_carries_write_flags(db: Database) -> None:
    """load_tags 是管线的输入源：漏选这两列 = 标签开关永远读不到。"""
    from app.store.tags import load_tags

    crud.insert_tag(db, name="风景", kind="primary", write_rating=False)
    tag = load_tags(db)[0]
    assert tag.write_rating is False
    assert tag.write_description is True


def test_tag_insert_none_fields_store_null(db: Database) -> None:
    tid = crud.insert_tag(
        db, name="风景", kind="primary",
        description="", archive=False, album=None, min_confidence=None,
    )
    with db.connect() as conn:
        row = conn.execute(
            "SELECT album, min_confidence FROM tags WHERE id=?", (tid,)
        ).fetchone()
    assert row["album"] is None
    assert row["min_confidence"] is None


def test_tag_insert_rejects_bad_kind(db: Database) -> None:
    with pytest.raises(ValidationError):
        crud.insert_tag(db, name="x", kind="nope")


def test_tag_name_unique_translated(db: Database) -> None:
    crud.insert_tag(db, name="风景", kind="primary")
    with pytest.raises(ValidationError) as e:
        crud.insert_tag(db, name="风景", kind="marker")
    assert e.value.problems[0].key == "store.tag.exists"


def test_tag_update_and_delete(db: Database) -> None:
    tid = crud.insert_tag(db, name="风景", kind="primary", album="A", min_confidence=0.8)
    crud.update_tag(
        db, tid, name="自然", kind="primary", description="d",
        archive=True, album=None, min_confidence=None, enabled=False,
    )
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name, description, archive, album, min_confidence, enabled "
            "FROM tags WHERE id=?", (tid,)
        ).fetchone()
    assert row["name"] == "自然"
    assert row["description"] == "d"
    assert row["archive"] == 1
    assert row["album"] is None
    assert row["min_confidence"] is None
    assert row["enabled"] == 0

    crud.delete_tag(db, tid)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM tags WHERE id=?", (tid,)
        ).fetchone() is None


def test_tag_set_enabled_roundtrip(db: Database) -> None:
    tid = crud.insert_tag(db, name="风景", kind="primary")
    crud.set_tag_enabled(db, tid, False)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM tags WHERE id=?", (tid,)
        ).fetchone()["enabled"] == 0
    crud.set_tag_enabled(db, tid, True)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM tags WHERE id=?", (tid,)
        ).fetchone()["enabled"] == 1


# ---------- prompt_versions ----------

def test_prompt_create_rejects_empty_body(db: Database) -> None:
    for body in ("", "   "):
        with pytest.raises(ValidationError):
            crud.create_prompt_version(db, body=body)


def test_prompt_create_list_and_latest(db: Database) -> None:
    i1 = crud.create_prompt_version(db, body="v1", note="first")
    i2 = crud.create_prompt_version(db, body="v2", note="second")
    rows = crud.list_prompt_versions(db)
    assert [r.id for r in rows] == [i2, i1]
    assert rows[0].body == "v2"

    latest = crud.get_latest_prompt_version(db)
    assert latest is not None
    assert latest.id == i2
    assert latest.body == "v2"


def test_prompt_latest_tie_break_same_second(db: Database) -> None:
    """同秒插入的两条，get_latest 取 id 更大的（created_at DESC, id DESC）。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO prompt_versions (body, created_at) VALUES ('old', '2026-01-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO prompt_versions (body, created_at) VALUES ('new', '2026-01-01 00:00:00')"
        )
    latest = crud.get_latest_prompt_version(db)
    assert latest is not None
    assert latest.body == "new"


def test_prompt_latest_empty_table(db: Database) -> None:
    assert crud.get_latest_prompt_version(db) is None


# ---------- exclusions ----------

def test_exclusion_insert_update_delete(db: Database) -> None:
    rid = crud.insert_exclusion(db, type="path", value="/foo")
    crud.update_exclusion(db, rid, type="album", value="旅行")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT type, value, enabled FROM exclusions WHERE id=?", (rid,)
        ).fetchone()
    assert row["type"] == "album"
    assert row["value"] == "旅行"
    assert row["enabled"] == 1

    crud.delete_exclusion(db, rid)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM exclusions WHERE id=?", (rid,)
        ).fetchone() is None


def test_exclusion_set_enabled_roundtrip(db: Database) -> None:
    rid = crud.insert_exclusion(db, type="path", value="/foo")
    crud.set_exclusion_enabled(db, rid, False)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM exclusions WHERE id=?", (rid,)
        ).fetchone()["enabled"] == 0
    crud.set_exclusion_enabled(db, rid, True)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM exclusions WHERE id=?", (rid,)
        ).fetchone()["enabled"] == 1


@pytest.mark.parametrize("bad_type", ["albumx", "PATH", "", None])
def test_exclusion_rejects_bad_type(db: Database, bad_type) -> None:
    with pytest.raises(ValidationError) as e:
        crud.insert_exclusion(db, type=bad_type, value="x")
    assert e.value.problems[0].key == "store.exclusion.type_invalid"


def test_exclusion_reviewed_forces_empty_value(db: Database) -> None:
    rid = crud.insert_exclusion(db, type="reviewed", value="whatever")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM exclusions WHERE id=?", (rid,)
        ).fetchone()
    assert row["value"] == ""
    # update 同样强制
    crud.update_exclusion(db, rid, type="reviewed", value="x")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM exclusions WHERE id=?", (rid,)
        ).fetchone()
    assert row["value"] == ""


@pytest.mark.parametrize("value", ["", "   "])
def test_exclusion_non_reviewed_requires_value(db: Database, value) -> None:
    with pytest.raises(ValidationError):
        crud.insert_exclusion(db, type="path", value=value)


def test_exclusion_tag_must_exist(db: Database) -> None:
    with pytest.raises(ValidationError) as e:
        crud.insert_exclusion(db, type="tag", value="风景")
    assert e.value.problems[0].key == "store.exclusion.tag_not_found"
    # 存在的标签可以
    crud.insert_tag(db, name="风景", kind="primary")
    rid = crud.insert_exclusion(db, type="tag", value="风景")
    assert rid > 0


# ---------- marker_rules ----------

def test_marker_rule_insert_update_delete(db: Database) -> None:
    tid = crud.insert_tag(db, name="截图", kind="marker")
    rid = crud.insert_marker_rule(db, marker_tag_id=tid,
                                  condition={"confidence_lt": 0.8})
    crud.update_marker_rule(db, rid, marker_tag_id=tid,
                            condition={"confidence_lt": 0.5})
    with db.connect() as conn:
        row = conn.execute(
            "SELECT marker_tag_id, condition, enabled FROM marker_rules WHERE id=?",
            (rid,),
        ).fetchone()
    import json
    assert row["marker_tag_id"] == tid
    assert json.loads(row["condition"]) == {"confidence_lt": 0.5}

    crud.delete_marker_rule(db, rid)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM marker_rules WHERE id=?", (rid,)
        ).fetchone() is None


def test_marker_rule_set_enabled_roundtrip(db: Database) -> None:
    tid = crud.insert_tag(db, name="截图", kind="marker")
    rid = crud.insert_marker_rule(db, marker_tag_id=tid,
                                  condition={"confidence_lt": 0.8})
    crud.set_marker_rule_enabled(db, rid, False)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM marker_rules WHERE id=?", (rid,)
        ).fetchone()["enabled"] == 0


def test_marker_rule_fk_translated(db: Database) -> None:
    with pytest.raises(ValidationError) as e:
        crud.insert_marker_rule(db, marker_tag_id=999,
                                condition={"confidence_lt": 0.8})
    assert e.value.problems[0].key == "store.marker.not_found"
    # update 同样转译
    tid = crud.insert_tag(db, name="截图", kind="marker")
    rid = crud.insert_marker_rule(db, marker_tag_id=tid,
                                  condition={"confidence_lt": 0.8})
    with pytest.raises(ValidationError) as e:
        crud.update_marker_rule(db, rid, marker_tag_id=999,
                                condition={"confidence_lt": 0.8})
    assert e.value.problems[0].key == "store.marker.not_found"


def test_marker_rule_rejects_primary_kind(db: Database) -> None:
    tid = crud.insert_tag(db, name="风景", kind="primary")
    with pytest.raises(ValidationError) as e:
        crud.insert_marker_rule(db, marker_tag_id=tid,
                                condition={"confidence_lt": 0.8})
    assert "marker" in str(e.value)
