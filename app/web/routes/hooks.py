"""POST /api/hooks/immich —— Immich Workflow webhook 接收端点。

职责边界：本端点只做校验、守卫与入队（create_job），**不执行任何处理**，
立即返回 200。worker 线程池负责 claim 与执行。

Immich 不在乎响应码，非 JSON body 也回 200 并落 parse_fail 事件 ——
原始留存的目的就是缓冲 Immich 版本变化，回 400 反而丢失原始 payload。

scope 判定复用 app/classify/scope.py 的 in_scope（无启用规则 = 全在范围）。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request

from app.classify.scope import in_scope, load_scope_rules
from app.store.events import log_event
from app.store.jobs import create_job
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

# asset_hint 摘取字段：payload 自带则省 worker 一次 API 查询。
_HINT_FIELDS = ("originalFileName", "exifInfo", "libraryId", "originalPath", "tags", "type")


def _extract_assets(payload) -> list[dict]:
    """容错提取 asset 列表：data.asset.id / asset.id / assetId / id 依次。

    返回的列表可能为空（提取不到 → 兜底 scan）。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    asset = data.get("asset", payload.get("asset"))
    if asset is not None:
        return asset if isinstance(asset, list) else [asset]
    for key in ("assetId", "id"):
        if isinstance(payload.get(key), str):
            return [{"id": payload[key]}]
    return []


def _hint(asset: dict) -> dict:
    if not isinstance(asset, dict):
        return {}
    return {k: asset[k] for k in _HINT_FIELDS if asset.get(k) is not None}


def _has_completed_record(db, asset_id: str) -> bool:
    """与 executor._already_done 同口径 —— 曾经的独立副本是隐患源（§7.1）。"""
    from app.engine.executor import _already_done

    return _already_done(db, asset_id)


def _has_active_single(db, asset_id: str) -> bool:
    """同 asset_id 已有 queued/running 的 single job → 在途去重。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE type = 'single' "
            "AND status IN ('queued', 'running') "
            "AND json_extract(params, '$.asset_id') = ? LIMIT 1",
            (asset_id,),
        ).fetchone()
    return row is not None


@router.post("/immich")
async def immich_hook(request: Request):
    global _last_fallback_scan_ts
    db = request.app.state.db
    raw = await request.body()
    settings = await asyncio.to_thread(lambda: SettingsStore(db))

    def _read_token() -> str:
        with _token_lock:
            return settings.webhook_token()

    token = await asyncio.to_thread(_read_token)
    supplied = request.headers.get("x-webhook-token", "")
    if not supplied or not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=401)

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        payload = None
    # 原始 payload 永远落 events（无论能否解析、开关与否）——排查 Immich 版本变化的唯一依据。
    await asyncio.to_thread(
        log_event,
        db,
        "webhook",
        "payload_received" if payload is not None else "parse_fail",
        payload if isinstance(payload, dict)
        else {"raw": raw.decode("utf-8", "replace")[:2000]},
    )
    if payload is None:
        await asyncio.to_thread(_maybe_enqueue_fallback_scan, db, settings)
        return {"status": "ok"}

    if not await asyncio.to_thread(lambda: settings.get("webhook_enabled")):
        await asyncio.to_thread(log_event, db, "webhook", "webhook_disabled")
        return {"status": "ok"}

    assets = _extract_assets(payload)
    if not assets:
        await asyncio.to_thread(_maybe_enqueue_fallback_scan, db, settings)
        return {"status": "ok"}

    rules = await asyncio.to_thread(load_scope_rules, db)
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        # type 缺失（assetId/id 简化形态）不算非图片；只有明确非 IMAGE 才跳过
        asset_type = asset.get("type")
        if asset_type is not None and asset_type != "IMAGE":
            # 非 IMAGE（VIDEO 等）直接忽略
            await asyncio.to_thread(
                log_event, db, "webhook", "asset_skipped",
                {"asset_id": asset.get("id"), "reason": "not_image"})
            continue
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        hint = _hint(asset)
        if await asyncio.to_thread(_has_active_single, db, asset_id):
            await asyncio.to_thread(
                log_event, db, "webhook", "asset_deduped",
                {"asset_id": asset_id})
            continue
        if await asyncio.to_thread(_has_completed_record, db, asset_id):
            await asyncio.to_thread(
                log_event, db, "webhook", "asset_skipped",
                {"asset_id": asset_id, "reason": "already_done"})
            continue
        if not in_scope(
            rules,
            library_id=hint.get("libraryId"),
            path=hint.get("originalPath"),
        ):
            await asyncio.to_thread(
                log_event, db, "webhook", "asset_out_of_scope",
                {"asset_id": asset_id})
            continue
        await asyncio.to_thread(
            create_job, db, "single",
            params={"asset_id": asset_id, "source": "webhook",
                    "asset_hint": hint})
    return {"status": "ok"}


def _maybe_enqueue_fallback_scan(db, settings: SettingsStore) -> None:
    """提取不到 id → 兜底 scan，30s 冷却窗口内最多一次。

    冷却的 check-then-set 在 threading.Lock 内原子执行：本函数被
    asyncio.to_thread 调度到线程池，并发 webhook 下无锁会重复入队。
    """
    global _last_fallback_scan_ts
    with _fallback_scan_lock:
        now = time.monotonic()
        if now - _last_fallback_scan_ts < _SCAN_COOLDOWN_SECONDS:
            log_event(db, "webhook", "fallback_scan_cooldown",
                      {"cooldown_seconds": _SCAN_COOLDOWN_SECONDS})
            return
        _last_fallback_scan_ts = now
    create_job(db, "scan", params={"source": "webhook", "reason": "parse_fail"})
    log_event(db, "webhook", "fallback_scan_triggered")
