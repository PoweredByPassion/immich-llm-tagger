"""events 表：事件流写入 + 读取（tail）。

payload 以 JSON 字符串存 TEXT；序列化后超过 64KB 则截断整体重存
（截断后的串可以不是合法 JSON，读取方按文本展示），并保证串内
含 "truncated" 标记，长度 ≤ 64KB。
"""
from __future__ import annotations

import json
from typing import Any

from app.db.connection import Database

MAX_PAYLOAD_BYTES = 64 * 1024


def _serialize_payload(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "{}"
    s = json.dumps(payload, ensure_ascii=False)
    if len(s) <= MAX_PAYLOAD_BYTES:
        return s
    # 截断整体重存：保证 ≤ 64KB 且含标记。keep = 原串前缀长度，使得
    # keep + len(marker) ≤ 64KB。
    marker = ',"_truncated": true}'
    keep = MAX_PAYLOAD_BYTES - len(marker)
    return s[:keep] + marker


def log_event(
    db: Database,
    source: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO events (source, event_type, payload) VALUES (?, ?, ?)",
            (source, event_type, _serialize_payload(payload)),
        )


def tail_events(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, source, event_type, payload "
            "FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
