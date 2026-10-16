"""store.transfer 契约：导出全表 + 剔除敏感字段；导入单事务重建 + 敏感保留 +
active_prompt_version_id 置 null + validate_config 全量校验失败整体回滚。
导出断言基于序列化后的 YAML 文本，确保文件本身不含凭证。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store import crud
from app.store.errors import ValidationError
from app.store.settings import SettingsStore
from app.store.transfer import export_config, import_config, parse_yaml, render_yaml


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


REPO_ROOT = Path(__file__).resolve().parents[1]


def _shipped_prompt(filename: str) -> str:
    """出厂配置里的那唯一一条 Prompt 版本 body。"""
    data = parse_yaml((REPO_ROOT / filename).read_text(encoding="utf-8"))
    return data["prompt_versions"][0]["body"]


def _seed(db: Database) -> None:
    primary = crud.insert_tag(db, name="人物", kind="primary", description="人")
    crud.insert_tag(db, name="other", kind="primary")  # 兜底 tag（默认 fallback_tag_name）
    marker = crud.insert_tag(db, name="模糊", kind="marker")
    crud.insert_marker_rule(db, marker_tag_id=marker, condition={"confidence_lt": 0.5})
    crud.insert_scope_rule(db, library_id="lib1", path_prefix="/photos")
    crud.insert_exclusion(db, type="tag", value="人物")
    crud.insert_exclusion(db, type="path", value="/secret")
    crud.create_prompt_version(db, body="prompt v1", note="n1")
    s = SettingsStore(db)
    s.set("webhook_token", "tok-secret-123")
    s.set("poll_interval_seconds", 42)
    s.set("active_prompt_version_id", 999)


def test_export_contains_all_tables_and_no_secrets(db: Database) -> None:
    _seed(db)
    data = export_config(db)
    for table in (
        "tags", "scope_rules", "marker_rules",
        "exclusions", "prompt_versions", "settings",
    ):
        assert table in data, table

    text = render_yaml(data)
    assert "tok-secret-123" not in text
    # settings 里不含任何敏感键（连接凭证在独立表 immich_conn/llm_conn，天然不在导出范围）
    settings = data["settings"]
    for key in ("webhook_token", "active_prompt_version_id",
                "immich_api_key", "llm_api_key"):
        assert key not in settings
    assert settings["poll_interval_seconds"] == 42


def test_import_roundtrip_equivalent_and_remap(db: Database) -> None:
    _seed(db)
    data = export_config(db)
    text = render_yaml(data)

    # 破坏现库再导入（验证清空重建）
    with db.connect() as conn:
        conn.execute("DELETE FROM marker_rules")
        conn.execute("DELETE FROM tags")

    import_config(db, parse_yaml(text))

    with db.connect() as conn:
        tags = conn.execute(
            "SELECT name, kind FROM tags ORDER BY id"
        ).fetchall()
        markers = conn.execute(
            "SELECT t.name AS tag_name, m.condition FROM marker_rules m "
            "JOIN tags t ON t.id = m.marker_tag_id"
        ).fetchall()
        excl = conn.execute(
            "SELECT type, value FROM exclusions ORDER BY id"
        ).fetchall()
    assert {(r["name"], r["kind"]) for r in tags} == {
        ("人物", "primary"), ("模糊", "marker"), ("other", "primary"),
    }
    assert markers[0]["tag_name"] == "模糊"
    assert json.loads(markers[0]["condition"]) == {"confidence_lt": 0.5}
    assert {(r["type"], r["value"]) for r in excl} == {
        ("tag", "人物"), ("path", "/secret"),
    }
    # settings 非敏感值被导入文件覆盖
    assert SettingsStore(db).get("poll_interval_seconds") == 42


def test_import_preserves_secrets_and_nulls_active_prompt(db: Database) -> None:
    _seed(db)
    data = export_config(db)

    # 导入前改变敏感值，验证保留现值
    s = SettingsStore(db)
    s.set("webhook_token", "current-token")
    s.set("active_prompt_version_id", 7)

    import_config(db, data)

    assert s.get("webhook_token") == "current-token"
    assert s.get("active_prompt_version_id") is None


def test_import_validation_failure_rolls_back(db: Database) -> None:
    _seed(db)
    data = export_config(db)
    # 构造非法：marker 规则锚定不存在的 tag 名
    data["marker_rules"] = [{"tag_name": "不存在的标签",
                             "condition": {"confidence_lt": 0.5}, "enabled": 1}]

    with pytest.raises(ValidationError):
        import_config(db, data)

    # 原配置原样
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM tags").fetchone()["c"] == 3
        assert conn.execute(
            "SELECT COUNT(*) c FROM marker_rules"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM scope_rules"
        ).fetchone()["c"] == 1


def test_import_yaml_error_validation_error(db: Database) -> None:
    with pytest.raises(ValidationError) as ei:
        import_config(db, parse_yaml("not: [valid: yaml"))
    assert ei.value.problems[0].key == "store.transfer.yaml_parse_failed"


def test_import_missing_tables_treated_as_empty(db: Database) -> None:
    # fallback_tag_name 兜底 tag 必须随文件导入（§3.1 校验）
    import_config(db, {"tags": [
        {"name": "人物", "kind": "primary"},
        {"name": "other", "kind": "primary"},
    ]})
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM scope_rules"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM tags"
        ).fetchone()["c"] == 2


def test_write_flags_survive_export_import(db: Database) -> None:
    """write_rating / write_description 是标签配置的一部分，必须随导出走。"""
    crud.insert_tag(db, name="人物", kind="primary", write_rating=False)
    crud.insert_tag(db, name="other", kind="primary")   # 兜底 tag
    data = export_config(db)
    row = next(t for t in data["tags"] if t["name"] == "人物")
    assert (row["write_rating"], row["write_description"]) == (0, 1)

    import_config(db, parse_yaml(render_yaml(data)))
    # 导入按名称重映射，id 会变 → 按名字查
    with db.connect() as conn:
        got = conn.execute(
            "SELECT write_rating, write_description FROM tags WHERE name = '人物'"
        ).fetchone()
    assert (got["write_rating"], got["write_description"]) == (0, 1)


def test_import_legacy_tags_default_to_writing_metadata(db: Database) -> None:
    """旧版导出文件没有这两列 → 按默认（写）导入，不能静默变成不写。"""
    import_config(db, {"tags": [
        {"name": "人物", "kind": "primary"},
        {"name": "other", "kind": "primary"},
    ]})
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT write_rating, write_description FROM tags").fetchall()
    assert all((r["write_rating"], r["write_description"]) == (1, 1) for r in rows)


def test_shipped_configs_are_importable_and_skip_rating_when_archived(
        tmp_path: Path) -> None:
    """出厂配置（zh/en）：必须能直接导入，且归档标签不写评分、仍写描述。

    归档 = 从主时间线隐去，星级评分对这类图没有意义；描述留着还能被搜到。
    """
    for name in ("autotag-config.zh.yaml", "autotag-config.en.yaml"):
        data = parse_yaml((REPO_ROOT / name).read_text(encoding="utf-8"))

        for tag in data["tags"]:
            if tag["archive"]:
                assert tag["write_rating"] is False, (name, tag["name"])
            else:
                assert tag["write_rating"] is True, (name, tag["name"])
            assert tag["write_description"] is True, (name, tag["name"])

        # 导入一整套（含 prompt 版本 / marker 规则 / settings）不报错
        fresh = Database(tmp_path / f"{name}.db")
        migrate(fresh)
        import_config(fresh, data)


def test_shipped_prompts_rate_visual_quality_not_importance() -> None:
    """出厂 Prompt 钉住评分语义：评画面质量，不是重要性/纪念价值。

    这是产品语义，不是措辞 —— 旧版 Prompt 正是按「1 = 最不重要」评的，回退不易察觉。
    """
    zh = _shipped_prompt("autotag-config.zh.yaml")
    en = _shipped_prompt("autotag-config.en.yaml")

    assert "不是重要性、纪念价值或情感价值" in zh
    assert "not how important, memorable or emotionally significant" in en
    # confidence 不是照片质量分（两者语义不能混）
    assert "不是照片质量分" in zh
    assert "not a photo quality score" in en
    # 文件名 / 拍摄日期只作上下文
    assert "不作为评分依据" in zh
    assert "never a basis for the rating" in en


def test_import_unknown_extra_keys_rejected(db: Database) -> None:
    with pytest.raises(ValidationError):
        import_config(db, {"tags": [], "bogus_table": []})


def test_import_allocates_new_prompt_ids(db: Database) -> None:
    _seed(db)
    data = export_config(db)
    old_ids = [p["id"] for p in data["prompt_versions"]]
    import_config(db, data)
    with db.connect() as conn:
        new_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM prompt_versions").fetchall()]
    assert new_ids != old_ids


def test_import_legacy_categories_section_ignored(db: Database) -> None:
    """旧版本导出的 categories 段自动忽略，不算未知段。"""
    import_config(db, {
        "tags": [
            {"name": "人物", "kind": "primary"},
            {"name": "other", "kind": "primary"},
        ],
        "categories": [{"name": "旧分类", "tag_name": "人物", "description": ""}],
    })


def test_skip_immich_exported_and_reimported(db: Database) -> None:
    """skip_immich（忽略标签）随导出/导入往返保持。"""
    crud.insert_tag(db, name="日常", kind="primary", skip_immich=True)
    crud.insert_tag(db, name="other", kind="primary")
    s = SettingsStore(db)
    s.set("fallback_tag_name", "other")
    import_config(db, export_config(db))
    with db.connect() as conn:
        rows = {r["name"]: r["skip_immich"] for r in conn.execute(
            "SELECT name, skip_immich FROM tags").fetchall()}
    assert rows["日常"] == 1 and rows["other"] == 0
