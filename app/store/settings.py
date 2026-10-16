"""settings 表：key/value KV，值以 JSON 编码存 TEXT。

默认值只在读取时兜底，不预先写库 —— 这样「DB 里有没有这一行」保持干净，
也符合「环境变量只在首次 seed，此后以 DB 为准」的设计。
"""
from __future__ import annotations

import copy
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
    "thumb_quality": 95,
    "record_retention_days": 90,
    "last_retention_run": None,     # epoch 秒（float），retention 24h 自管理时间戳
    "active_prompt_version_id": None,
    # --- Effector 开关与重试（§8.1）---
    # 默认全开：升级后评分/描述立即对新资产生效。想"升级即静默"就
    # 从这里移除 "rating"/"description"。
    "enabled_effects": ["tag", "visibility", "album", "rating", "description"],
    # None = 未显式配置 → 按语言解析（zh 保留人工信息 / en manual-kept）。
    # **不能**把中文名写死成默认值：那会让英文实例写出一个中文标签名。
    "skip_marker_tag_name": None,
    "apply_max_attempts": 3,
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
        return copy.deepcopy(stored.get(key, DEFAULTS[key]))

    def get_all(self) -> dict[str, Any]:
        return copy.deepcopy({**DEFAULTS, **self._raw()})

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


# 跳过标记标签名的语言默认值。标签名是**写进 Immich 的数据**，不是界面文案，
# 所以不放进 i18n 字典 —— 它必须是一个确定值，而不是随浏览器语言变的东西。
# store 层也不该反向 import web 层（现状是 web → store 单向）。
_SKIP_MARKER_DEFAULTS = {"zh": "保留人工信息", "en": "manual-kept"}


def resolve_skip_marker_tag_name(settings: "SettingsStore", lang: str = "zh") -> str:
    """跳过标记标签名的唯一真源（§5.8 / §8.1）。

    显式配置优先；未配置时跟随语言。后台任务（scan / classify_item）
    没有请求上下文，一律按默认 "zh" 取，与 Web 首屏一致。
    """
    return settings.get("skip_marker_tag_name") or _SKIP_MARKER_DEFAULTS.get(
        lang, _SKIP_MARKER_DEFAULTS["zh"]
    )
