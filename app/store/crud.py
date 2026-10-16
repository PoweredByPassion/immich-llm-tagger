"""配置表 CRUD：scope_rules / tags / prompt_versions.

错误转译约定：sqlite3.IntegrityError（UNIQUE / FK RESTRICT）一律转成
ValidationError，problems 里放 i18n key（+ 参数），由 web 层按请求语言渲染
（见 app.web.i18n.render_problems）。校验复用 validation.validate_config 的
风格，不另造错误类型。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.db.connection import Database
from app.store.errors import Problem, ValidationError

_KINDS = ("primary", "marker")


def _translate(exc: sqlite3.IntegrityError, problem: Problem) -> ValidationError:
    return ValidationError([problem])


# ---------------- scope_rules ----------------

def insert_scope_rule(db: Database, *, library_id: str, path_prefix: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO scope_rules (library_id, path_prefix, enabled) "
            "VALUES (?, ?, 1)",
            (library_id, path_prefix),
        )
    return cur.lastrowid  # type: ignore[return-value]


def update_scope_rule(db: Database, rule_id: int, *,
                      library_id: str, path_prefix: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE scope_rules SET library_id = ?, path_prefix = ? WHERE id = ?",
            (library_id, path_prefix, rule_id),
        )


def delete_scope_rule(db: Database, rule_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM scope_rules WHERE id = ?", (rule_id,))


def set_scope_rule_enabled(db: Database, rule_id: int, enabled: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE scope_rules SET enabled = ? WHERE id = ?",
            (int(enabled), rule_id),
        )


# ---------------- tags ----------------

def insert_tag(db: Database, *, name: str, kind: str, description: str = "",
               archive: bool = False, album: str | None = None,
               min_confidence: float | None = None,
               skip_immich: bool = False,
               write_rating: bool = True, write_description: bool = True) -> int:
    if kind not in _KINDS:
        raise ValidationError([Problem("store.tag.kind_invalid", (repr(kind),))])
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO tags (name, description, kind, archive, album, "
                "                  min_confidence, enabled, skip_immich, "
                "                  write_rating, write_description) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (name, description, kind, int(archive), album, min_confidence,
                 int(skip_immich), int(write_rating), int(write_description)),
            )
    except sqlite3.IntegrityError:
        raise ValidationError([Problem("store.tag.exists", (name,))]) from None
    return cur.lastrowid  # type: ignore[return-value]


def update_tag(db: Database, tag_id: int, *, name: str, kind: str,
               description: str, archive: bool, album: str | None,
               min_confidence: float | None, enabled: bool,
               skip_immich: bool = False,
               write_rating: bool = True, write_description: bool = True) -> None:
    if kind not in _KINDS:
        raise ValidationError([Problem("store.tag.kind_invalid", (repr(kind),))])
    try:
        with db.connect() as conn:
            conn.execute(
                "UPDATE tags SET name = ?, description = ?, kind = ?, "
                "                archive = ?, album = ?, min_confidence = ?, "
                "                enabled = ?, skip_immich = ?, "
                "                write_rating = ?, write_description = ? "
                "WHERE id = ?",
                (name, description, kind, int(archive), album, min_confidence,
                 int(enabled), int(skip_immich), int(write_rating),
                 int(write_description), tag_id),
            )
    except sqlite3.IntegrityError:
        raise ValidationError([Problem("store.tag.exists", (name,))]) from None


def delete_tag(db: Database, tag_id: int) -> None:
    """tag 被 marker 规则等 FK RESTRICT 保护时的兜底，转译为可读错误。"""
    try:
        with db.connect() as conn:
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    except sqlite3.IntegrityError:
        raise ValidationError([
            Problem("store.tag.in_use")
        ]) from None


def set_tag_enabled(db: Database, tag_id: int, enabled: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE tags SET enabled = ? WHERE id = ?", (int(enabled), tag_id)
        )


# ---------------- prompt_versions ----------------

@dataclass(frozen=True)
class PromptVersionRow:
    id: int
    body: str
    note: str
    created_at: str


def create_prompt_version(db: Database, *, body: str, note: str = "") -> int:
    if not body or not body.strip():
        raise ValidationError([Problem("store.prompt.body_required")])
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO prompt_versions (body, note) VALUES (?, ?)",
            (body, note),
        )
    return cur.lastrowid  # type: ignore[return-value]


def list_prompt_versions(db: Database) -> list[PromptVersionRow]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, body, note, created_at FROM prompt_versions "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [
        PromptVersionRow(
            id=r["id"], body=r["body"], note=r["note"], created_at=r["created_at"]
        )
        for r in rows
    ]


def get_latest_prompt_version(db: Database) -> PromptVersionRow | None:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT id, body, note, created_at FROM prompt_versions "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if r is None:
        return None
    return PromptVersionRow(
        id=r["id"], body=r["body"], note=r["note"], created_at=r["created_at"]
    )


# ---------------- exclusions ----------------

_EXCLUSION_TYPES = ("album", "path", "tag", "reviewed")


def _validate_exclusion(db: Database, *, type: str, value: str) -> None:
    if type not in _EXCLUSION_TYPES:
        raise ValidationError([
            Problem("store.exclusion.type_invalid", (repr(type),))
        ])
    if type == "reviewed":
        return  # reviewed 无值，调用方强制存空串
    if not value or not value.strip():
        raise ValidationError([Problem("store.exclusion.value_required", (type,))])
    if type == "tag":
        with db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tags WHERE name = ?", (value,)
            ).fetchone()
        if row is None:
            raise ValidationError([Problem("store.exclusion.tag_not_found", (value,))])


def insert_exclusion(db: Database, *, type: str, value: str) -> int:
    _validate_exclusion(db, type=type, value=value)
    if type == "reviewed":
        value = ""
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO exclusions (type, value, enabled) VALUES (?, ?, 1)",
            (type, value),
        )
    return cur.lastrowid  # type: ignore[return-value]


def update_exclusion(db: Database, rule_id: int, *,
                     type: str, value: str) -> None:
    _validate_exclusion(db, type=type, value=value)
    if type == "reviewed":
        value = ""
    with db.connect() as conn:
        conn.execute(
            "UPDATE exclusions SET type = ?, value = ? WHERE id = ?",
            (type, value, rule_id),
        )


def delete_exclusion(db: Database, rule_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM exclusions WHERE id = ?", (rule_id,))


def set_exclusion_enabled(db: Database, rule_id: int, enabled: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE exclusions SET enabled = ? WHERE id = ?",
            (int(enabled), rule_id),
        )


# ---------------- marker_rules ----------------

def _validate_marker_tag(db: Database, marker_tag_id: int) -> None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name, kind FROM tags WHERE id = ?", (marker_tag_id,)
        ).fetchone()
    if row is None:
        raise ValidationError([
            Problem("store.marker.not_found", (marker_tag_id,))
        ])
    if row["kind"] != "marker":
        raise ValidationError([
            Problem("store.marker.kind_invalid", (row["name"], row["kind"]))
        ])


def insert_marker_rule(db: Database, *, marker_tag_id: int,
                       condition: dict) -> int:
    _validate_marker_tag(db, marker_tag_id)
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO marker_rules (marker_tag_id, condition, enabled) "
                "VALUES (?, ?, 1)",
                (marker_tag_id, json.dumps(condition, ensure_ascii=False)),
            )
    except sqlite3.IntegrityError:
        raise _translate(None, Problem("store.marker.not_found", (marker_tag_id,))) from None
    return cur.lastrowid  # type: ignore[return-value]


def update_marker_rule(db: Database, rule_id: int, *, marker_tag_id: int,
                       condition: dict) -> None:
    _validate_marker_tag(db, marker_tag_id)
    try:
        with db.connect() as conn:
            conn.execute(
                "UPDATE marker_rules SET marker_tag_id = ?, condition = ? "
                "WHERE id = ?",
                (marker_tag_id, json.dumps(condition, ensure_ascii=False), rule_id),
            )
    except sqlite3.IntegrityError:
        raise _translate(None, Problem("store.marker.not_found", (marker_tag_id,))) from None


def delete_marker_rule(db: Database, rule_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM marker_rules WHERE id = ?", (rule_id,))


def set_marker_rule_enabled(db: Database, rule_id: int, enabled: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE marker_rules SET enabled = ? WHERE id = ?",
            (int(enabled), rule_id),
        )
