"""settings 表：key/value KV，值以 JSON 编码存 TEXT。

默认值只在读取时兜底，不预先写库 —— 这样「DB 里有没有这一行」保持干净，
也符合「环境变量只在首次 seed，此后以 DB 为准」的设计。
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from app.db.connection import Database

DEFAULTS: dict[str, Any] = {
    "queue_tag_name": "未分类",
    "webhook_enabled": True,
    "webhook_token": None,          # None → 首次访问时生成
    "poll_enabled": True,
    "poll_interval_seconds": 300,
    "default_min_confidence": 0.8,
    "fallback_tag_name": "other",
    "thumb_preset": "large",
    "thumb_quality": 85,
    "record_retention_days": 90,
    "last_retention_run": None,     # epoch 秒（float），retention 24h 自管理时间戳
    "active_prompt_version_id": None,
}


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _raw(self) -> dict[str, Any]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            out[row["key"]] = json.loads(row["value"])
        return out

    def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            raise KeyError(key)
        stored = self._raw()
        return stored.get(key, DEFAULTS[key])

    def get_all(self) -> dict[str, Any]:
        return {**DEFAULTS, **self._raw()}

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ------------------------------------------------------------ 专用操作

    def webhook_token(self) -> str:
        token = self.get("webhook_token")
        if token is None:
            token = self.rotate_webhook_token()
        return token

    def rotate_webhook_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self.set("webhook_token", token)
        return token

    def set_active_prompt_version(self, version_id: int | None) -> None:
        """单点切换激活版本。"""
        self.set("active_prompt_version_id", version_id)
