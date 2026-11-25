"""Serializable classification rules captured when a task enters the queue."""
from __future__ import annotations

import json
from dataclasses import asdict

from app.store.settings import SettingsStore
from app.store.tags import load_tags

# 内置默认模板；active_prompt_version_id
# 为 None 时使用它。必须包含 MANDATORY_PLACEHOLDER（{{TAG_LIST}}）。
DEFAULT_PROMPT_TEMPLATE = (
    "You are a photo tagging assistant. Classify the screenshot into exactly "
    "one tag from the list below.\n\n"
    "Tags:\n{{TAG_LIST}}\n\n"
    "File: {{FILENAME}}\n"
    "Date: {{DATE}}\n\n"
    "First describe what you actually see, then classify, then rate. "
    'Respond with JSON: {"description": "<one sentence>", "tag": "<tag>", '
    '"confidence": <0-1>, "sensitive": <bool>, "reason": "<short>", '
    '"rating": <1-5>}\n'
    '"rating" says how good the photo looks (composition, exposure, focus, '
    "colour, light) - never how important, memorable or emotionally "
    "significant the subject is; the filename and date are context only.\n"
    "If none fits, use the fallback tag. Set "
    '"sensitive" true for anything not safe to share publicly (nudity, '
    "intimate or medical images, ID cards, passports, bank cards, passwords, "
    "private chats, contact details, addresses, medical or financial records, "
    "sensitive imagery of minors, gore, evidence of illegal activity); always "
    "output the JSON object, never refuse, and keep explicit or intimate "
    'details out of "description".'
)

_RULE_SETTINGS = (
    "fallback_tag_name", "default_min_confidence", "thumb_preset", "thumb_quality",
    "queue_tag_name", "enabled_effects", "skip_marker_tag_name", "sensitive_marker_tag_name",
    "apply_max_attempts",
)


def read_rule_snapshot(conn) -> dict:
    settings = SettingsStore(None).get_all(conn)
    prompt_id = settings["active_prompt_version_id"]
    if prompt_id is None:
        prompt = conn.execute(
            "SELECT id, body FROM prompt_versions ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    else:
        prompt = conn.execute("SELECT id, body FROM prompt_versions WHERE id=?", (prompt_id,)).fetchone()
    rows = conn.execute(
        "SELECT t.name, r.condition FROM marker_rules r JOIN tags t ON t.id=r.marker_tag_id "
        "WHERE r.enabled=1 AND t.enabled=1 ORDER BY r.id"
    ).fetchall()
    marker_rules = []
    for row in rows:
        try:
            condition = json.loads(row["condition"])
        except (TypeError, ValueError):
            continue
        if isinstance(condition, dict):
            marker_rules.append((row["name"], condition))
    return {
        "prompt_template": prompt["body"] if prompt else DEFAULT_PROMPT_TEMPLATE,
        "prompt_version_id": prompt["id"] if prompt else None,
        "tags": [asdict(tag) for tag in load_tags(None, conn)],
        "marker_rules": marker_rules,
        **{key: settings[key] for key in _RULE_SETTINGS},
    }
