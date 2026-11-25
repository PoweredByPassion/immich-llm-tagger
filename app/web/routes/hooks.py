"""Authenticate Immich notifications and queue asset IDs for background checks."""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request

from app.store.events import log_event
from app.store.jobs import enqueue_auto_job, enqueue_scan
from app.store.settings import SettingsStore

router = APIRouter(prefix="/api/hooks")

# 兜底 scan 冷却窗口（秒）：30s 内最多一次，防止解析失败扇出雪崩。
_SCAN_COOLDOWN_SECONDS = 30
# 上次兜底 scan 入队时间的内存存法。兜底 scan 只防本进程被高频投递打爆，
# 进程级内存状态即可；重启后窗口清零无实际风险（scan 自身还有 has_active_job_of_type 去重）。
_last_fallback_scan_ts: float = 0.0
# to_thread 化后冷却的 check-then-set 可能跨线程并发执行，需锁保证原子。
_fallback_scan_lock = threading.Lock()
# webhook_token 首次访问时惰性生成 + 持久化；并发首投可能各铸一枚（后写胜）。
# 生成仅一次，之后无竞争，锁是 uncontended fast-path。
_token_lock = threading.Lock()
_MAX_WEBHOOK_BYTES = 2 * 1024 * 1024


def _extract_assets(payload) -> list[dict]:
    """Read asset details or IDs from wrapped and unwrapped notifications."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    asset = data.get("asset", payload.get("asset"))
    if asset is not None:
        return asset if isinstance(asset, list) else [asset]
    for item in (data, payload):
        for key in ("assetId", "id"):
            if isinstance(item.get(key), str):
                return [{"id": item[key]}]
    return []


@router.post("/immich")
async def immich_hook(request: Request):
    db = request.app.state.db
    settings = SettingsStore(db)

    def _read_token() -> str:
        with _token_lock:
            return settings.webhook_token()

    token = await asyncio.to_thread(_read_token)
    supplied = request.headers.get("x-webhook-token", "")
    if not supplied or not secrets.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401)

    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413)
        raw.extend(chunk)
    await asyncio.to_thread(_handle_notification, db, settings, raw)
    return {"status": "ok"}


def _handle_notification(db, settings: SettingsStore, raw: bytearray) -> None:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        payload = None

    assets = _extract_assets(payload)
    log_event(db, "webhook", "payload_received" if payload is not None else "parse_fail",
              {"bytes": len(raw), "asset_count": len(assets)})
    if not settings.get("webhook_enabled"):
        log_event(db, "webhook", "webhook_disabled")
        return

    valid_asset_seen = False
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 128:
            continue
        valid_asset_seen = True
        asset_type = asset.get("type")
        if asset_type is not None and asset_type != "IMAGE":
            log_event(db, "webhook", "asset_skipped",
                      {"asset_id": asset_id, "reason": "not_image"})
            continue
        job_id, created = enqueue_auto_job(
            db, "single", {"asset_id": asset_id, "source": "webhook"},
            enabled_setting="webhook_enabled",
        )
        if job_id is None and settings.get("webhook_enabled"):
            log_event(db, "webhook", "asset_skipped",
                      {"asset_id": asset_id, "reason": "already_done"})
        elif job_id is not None and not created:
            log_event(db, "webhook", "asset_deduped", {"asset_id": asset_id})
    if not valid_asset_seen:
        _maybe_enqueue_fallback_scan(db, settings)


def _maybe_enqueue_fallback_scan(db, settings: SettingsStore) -> None:
    """提取不到 id → 兜底 scan，30s 冷却窗口内最多一次。

    冷却的 check-then-set 在 threading.Lock 内原子执行：本函数被
    asyncio.to_thread 调度到线程池，并发 webhook 下无锁会重复入队。
    """
    global _last_fallback_scan_ts
    with _fallback_scan_lock:
        if not settings.get("webhook_enabled"):
            return
        now = time.monotonic()
        if now - _last_fallback_scan_ts < _SCAN_COOLDOWN_SECONDS:
            log_event(db, "webhook", "fallback_scan_cooldown",
                      {"cooldown_seconds": _SCAN_COOLDOWN_SECONDS})
            return
        _, created = enqueue_scan(
            db, {"source": "webhook", "reason": "parse_fail"}, enabled_setting="webhook_enabled",
        )
        if created:
            _last_fallback_scan_ts = now
    if created:
        log_event(db, "webhook", "fallback_scan_triggered")
