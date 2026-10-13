"""资产租约：同一张图同一时刻只允许一个 worker 处理。

lease_expires_at 是 SQLite datetime('now') 的 'YYYY-MM-DD HH:MM:SS' 格式，
与数据库列上的 CHECK GLOB 约束配合；所有时间计算都交给 SQLite 的
datetime()，不要在 Python 里拼字符串（isoformat() 的 'T' 会被 CHECK 挡下）。
"""
from __future__ import annotations

import sqlite3

from app.db.connection import Database

# 与 LLMClient.chat 的默认 retries=4 保持一致。
LLM_MAX_RETRIES = 4

# 租约应覆盖：LLM 全部重试的最坏耗时 + 安全余量
_SAFETY_MARGIN_SECONDS = 120


def lease_seconds_for(llm_timeout: int, max_retries: int) -> int:
    return llm_timeout * (max_retries + 1) + _SAFETY_MARGIN_SECONDS


def claim(db: Database, asset_id: str, owner: int, lease_seconds: int) -> bool:
    """抢租约：先保证 asset_state 行存在，再仅当无租约/已过期时占用。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id) VALUES (:aid) "
            "ON CONFLICT(asset_id) DO NOTHING",
            {"aid": asset_id},
        )
        cur = conn.execute(
            "UPDATE asset_state "
            "   SET lease_owner = :owner, "
            "       lease_expires_at = datetime('now', :plus) "
            " WHERE asset_id = :aid "
            "   AND (lease_owner = :owner OR lease_expires_at IS NULL "
            "        OR lease_expires_at < datetime('now'))",
            {"owner": owner, "plus": f"+{lease_seconds} seconds", "aid": asset_id},
        )
        return cur.rowcount > 0


def release(db: Database, asset_id: str, owner: int) -> None:
    """释放自己的租约；owner 不匹配则不动别人的租约。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE asset_state "
            "   SET lease_owner = NULL, lease_expires_at = NULL "
            " WHERE asset_id = :aid AND lease_owner = :owner",
            {"aid": asset_id, "owner": owner},
        )


def renew(db: Database, asset_id: str, owner: int, lease_seconds: int) -> bool:
    """延长自己的租约；行不存在或 owner 不匹配 → False。"""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE asset_state "
            "   SET lease_expires_at = datetime('now', :plus) "
            " WHERE asset_id = :aid AND lease_owner = :owner",
            {"plus": f"+{lease_seconds} seconds", "aid": asset_id, "owner": owner},
        )
        return cur.rowcount > 0
