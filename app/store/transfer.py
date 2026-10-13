"""配置导入导出。

导出六张配置表为可序列化 dict；marker_rules 的 tags FK 在导出里用**引用
标签名**表示（tag_name），导入时按名称重映射到重建后的新 id。
导入文件中残留的 categories 段（旧版本导出）自动忽略。
敏感字段不导出：settings.webhook_token、settings.active_prompt_version_id，
以及连接凭证（immich_conn / llm_conn 两张独立表，天然不在导出范围）。

导入是单事务：清空六表 → 按依赖顺序重建（tags 先）→ 敏感字段回写现值 →
active_prompt_version_id 置 null → validate_config 全量校验，任一失败抛
ValidationError 并整体回滚（调用方通过 db.connect() 的异常回滚保证）。
YAML 解析错误、顶层结构非法转 ValidationError（problems 为 i18n key + 参数，
由 web 层按请求语言渲染）。
"""
from __future__ import annotations

import json
from typing import Any

import yaml

from app.db.connection import Database
from app.store.errors import Problem, ValidationError
from app.store.validation import TagRow, validate_config

_TABLES = (
    "tags", "scope_rules", "marker_rules",
    "exclusions", "prompt_versions", "settings",
)

# settings 导出时剔除的敏感键（webhook_token、active_prompt_version_id；
# 连接 api_key 存于 immich_conn/llm_conn 独立表，不在 settings，天然不导出）
_SENSITIVE_SETTINGS_KEYS = ("webhook_token", "active_prompt_version_id")


def export_config(db: Database) -> dict[str, Any]:
    with db.connect() as conn:
        tags = [dict(r) for r in conn.execute(
            "SELECT name, description, kind, archive, album, min_confidence, "
            "       enabled, skip_immich FROM tags ORDER BY id").fetchall()]

        scope = [dict(r) for r in conn.execute(
            "SELECT library_id, path_prefix, enabled FROM scope_rules "
            "ORDER BY id").fetchall()]

        markers = [dict(r) for r in conn.execute(
            "SELECT t.name AS tag_name, m.condition, m.enabled "
            "FROM marker_rules m JOIN tags t ON t.id = m.marker_tag_id "
            "ORDER BY m.id").fetchall()]
        for m in markers:
            m["condition"] = json.loads(m["condition"])

        exclusions = [dict(r) for r in conn.execute(
            "SELECT type, value, enabled FROM exclusions "
            "ORDER BY id").fetchall()]

        prompts = [dict(r) for r in conn.execute(
            "SELECT id, body, note, created_at FROM prompt_versions "
            "ORDER BY id").fetchall()]

        raw_settings = {r["key"]: json.loads(r["value"]) for r in conn.execute(
            "SELECT key, value FROM settings").fetchall()}

    settings = {
        k: v for k, v in raw_settings.items()
        if k not in _SENSITIVE_SETTINGS_KEYS
    }

    return {
        "tags": tags,
        "scope_rules": scope,
        "marker_rules": markers,
        "exclusions": exclusions,
        "prompt_versions": prompts,
        "settings": settings,
    }


def import_config(db: Database, data: Any) -> None:
    if not isinstance(data, dict):
        raise ValidationError([Problem("store.transfer.top_level_mapping")])
    unknown = set(data) - set(_TABLES) - {"categories"}
    if unknown:
        raise ValidationError([Problem(
            "store.transfer.unknown_section", (", ".join(sorted(unknown)),)
        )])

    def section(name: str) -> list[dict]:
        val = data.get(name, [])
        if not isinstance(val, list) or not all(isinstance(r, dict) for r in val):
            raise ValidationError([Problem(
                "store.transfer.section_not_rows", (name,)
            )])
        return val

    settings_in = data.get("settings", {})
    if not isinstance(settings_in, dict):
        raise ValidationError([Problem("store.transfer.settings_not_mapping")])

    try:
        with db.connect() as conn:
            # 敏感字段先读现值，重建后回写
            preserved = {
                k: v for k, v in
                {r["key"]: r["value"] for r in conn.execute(
                    "SELECT key, value FROM settings WHERE key IN (?, ?)",
                    _SENSITIVE_SETTINGS_KEYS).fetchall()}.items()
            }

            for t in ("scope_rules", "exclusions", "prompt_versions",
                      "marker_rules", "tags", "settings"):
                # FK RESTRICT：先删子表（marker_rules→tags）
                conn.execute(f"DELETE FROM {t}")

            # tags 先建，取回名称 → 新 id 映射
            tag_ids: dict[str, int] = {}
            for r in section("tags"):
                cur = conn.execute(
                    "INSERT INTO tags (name, description, kind, archive, album, "
                    "                  min_confidence, enabled, skip_immich) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["name"], r.get("description", ""), r["kind"],
                     int(r.get("archive", 0)), r.get("album"),
                     r.get("min_confidence"), int(r.get("enabled", 1)),
                     int(r.get("skip_immich", 0))),
                )
                tag_ids[r["name"]] = cur.lastrowid

            for r in section("scope_rules"):
                conn.execute(
                    "INSERT INTO scope_rules (library_id, path_prefix, enabled) "
                    "VALUES (?, ?, ?)",
                    (r.get("library_id", ""), r.get("path_prefix", ""),
                     int(r.get("enabled", 1))),
                )

            for r in section("marker_rules"):
                if r["tag_name"] not in tag_ids:
                    raise ValidationError([Problem(
                        "store.transfer.marker_tag_missing", (r["tag_name"],)
                    )])
                conn.execute(
                    "INSERT INTO marker_rules (marker_tag_id, condition, enabled) "
                    "VALUES (?, ?, ?)",
                    (tag_ids[r["tag_name"]],
                     json.dumps(r["condition"], ensure_ascii=False),
                     int(r.get("enabled", 1))),
                )

            for r in section("exclusions"):
                conn.execute(
                    "INSERT INTO exclusions (type, value, enabled) "
                    "VALUES (?, ?, ?)",
                    (r["type"], r.get("value", ""), int(r.get("enabled", 1))),
                )

            for r in section("prompt_versions"):
                conn.execute(
                    "INSERT INTO prompt_versions (body, note, created_at) "
                    "VALUES (?, ?, ?)",
                    (r["body"], r.get("note", ""), r.get("created_at")),
                )

            for k, v in settings_in.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (k, json.dumps(v, ensure_ascii=False)),
                )

            # 敏感字段保留现值；active_prompt_version_id 置 null
            for k, v in preserved.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("active_prompt_version_id", json.dumps(None)),
            )

            # 全量快照校验，任一失败 → 异常 → connect() 回滚
            tag_rows = [TagRow(id=r["id"], name=r["name"], kind=r["kind"],
                               enabled=bool(r["enabled"]))
                        for r in conn.execute(
                            "SELECT id, name, kind, enabled FROM tags").fetchall()]
            validate_config(
                tags=tag_rows,
                marker_rules=[(r["id"], r["marker_tag_id"]) for r in conn.execute(
                    "SELECT id, marker_tag_id FROM marker_rules").fetchall()],
                fallback_tag_name=json.loads(conn.execute(
                    "SELECT value FROM settings WHERE key = 'fallback_tag_name'"
                ).fetchone()["value"]) if conn.execute(
                    "SELECT 1 FROM settings WHERE key = 'fallback_tag_name'"
                ).fetchone() else "other",
                active_prompt_version_id=None,
                prompt_version_ids={r["id"] for r in conn.execute(
                    "SELECT id FROM prompt_versions").fetchall()},
            )
    except ValidationError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise ValidationError([Problem("store.transfer.structure_invalid", (str(e),))]) from None


def render_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def parse_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValidationError([Problem("store.transfer.yaml_parse_failed", (str(e),))]) from None
