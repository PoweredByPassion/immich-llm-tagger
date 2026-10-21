"""提交器：把逻辑 Effect 按物理端点分桶执行，并**逐桶立即记账**，
最后由成功闸门驱动的 Finalizer 摘队列标签。

不变式（§5.5）：
    对 Immich 的每一次成功写入，都必须有一条已提交的 change_log 记录。
    没有例外 —— 账丢了，"这个值是不是我们写的"就判不出来（§5.8.1），
    而且该写入将无法被 Undo / Reset 还原。

Finalizer（§5.6）：
    全部桶成功 AND not dry_run  → 摘队列标签 + 记 queue_remove。
    队列**不是** planner 产出的 Effect：它没有独立的规划语义，只是收尾。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.classify.effectors.base import (
    BUCKET_ALBUM, BUCKET_ASSET_PROPS, BUCKET_QUEUE, BUCKET_TAGS,
    Effect, Gates,
)
from app.db.connection import Database
from app.store.events import log_event

# 物理桶的执行顺序。
# 属性桶排最前：它是"部分提交后重试对不上"的重灾区（§5.8.1 情形①），
# 让它的账在标签桶失败**之前**就落好，是这条不变式最容易失效的地方。
# 标签桶在队列之前：先写新标签、最后摘队列标签 —— 中途崩溃不会让图片
# 「既没标签又不在队列」（沿用基础 §5.5 的既有意图）。
_BUCKET_ORDER = (BUCKET_ASSET_PROPS, BUCKET_TAGS, BUCKET_ALBUM)


@dataclass
class CommitResult:
    ok: bool
    failed_bucket: str | None = None
    error: str | None = None
    committed: list[Effect] = field(default_factory=list)   # 真正落地的 Effect


def commit_effects(db: Database, client, effects: list[Effect], *,
                   asset_id: str, job_id: int | None,
                   queue_tag_id: str | None, queue_tag_name: str,
                   gates: Gates) -> CommitResult:
    """执行 effects 并逐桶记账；全部成功才摘队列标签。

    queue_tag_id=None 表示"不摘队列"（dry_run / 测试）。asset_id 必须显式传：
    skip_immich 时 effects 为空，但队列仍要摘（§5.7）。
    `committed` 让调用方能从"真正落地的 Effect"推导 asset_state，而不是拿
    意图当事实去猜（管线用它写本地镜像）。
    """
    if gates.dry_run:
        return CommitResult(ok=False, error="dry_run")

    buckets: dict[str, list[Effect]] = {}
    for e in effects:
        buckets.setdefault(e.bucket, []).append(e)

    committed: list[Effect] = []
    for bucket in _BUCKET_ORDER:
        items = buckets.get(bucket) or []
        if not items:
            continue
        try:
            _apply_bucket(client, bucket, items)      # 物理写入
        except Exception as exc:                      # noqa: BLE001
            log_event(db, "system", "apply_failed", {
                "asset_id": asset_id, "bucket": bucket, "error": str(exc)})
            return CommitResult(ok=False, failed_bucket=bucket, error=str(exc),
                                committed=committed)
        _ledger(db, items, job_id)                    # ← 立即记账，不攒到最后
        committed.extend(items)

    if queue_tag_id is None:
        return CommitResult(ok=True, committed=committed)
    queue_effect = Effect("queue_remove", asset_id,
                          {"queue_tag_name": queue_tag_name})
    result = _finalize_queue(db, client, asset_id=asset_id, job_id=job_id,
                             queue_tag_id=queue_tag_id,
                             queue_effect=queue_effect)
    result.committed = [*committed, *result.committed]
    return result


def _finalize_queue(db, client, *, asset_id, job_id, queue_tag_id,
                    queue_effect) -> CommitResult:
    """成功闸门通过：摘队列标签 + 记账。失败则保留队列标签，等下一轮重试。"""
    try:
        client.untag_assets(queue_tag_id, [asset_id])
    except Exception as exc:                          # noqa: BLE001
        log_event(db, "system", "apply_failed", {
            "asset_id": asset_id, "bucket": BUCKET_QUEUE, "error": str(exc)})
        return CommitResult(ok=False, failed_bucket=BUCKET_QUEUE, error=str(exc))
    _ledger(db, [queue_effect], job_id)
    return CommitResult(ok=True, committed=[queue_effect])


def _apply_bucket(client, bucket: str, items: list[Effect]) -> None:
    # 同一桶内的 Effect 必属同一 asset（管线是逐图处理），故取 items[0]。
    asset_id = items[0].asset_id
    if bucket == BUCKET_TAGS:
        names = [e.payload["tag_name"] for e in items]
        tag_ids = client.ensure_tags(names)
        client.tag_assets([tag_ids[n] for n in names], [asset_id])
    elif bucket == BUCKET_ASSET_PROPS:
        _apply_asset_props(client, asset_id, items)
    elif bucket == BUCKET_ALBUM:
        album_id = client.ensure_album(items[0].payload["album_name"])
        client.add_to_album(album_id, [asset_id])
    else:  # pragma: no cover - bucket_of 已限定取值
        raise ValueError(f"未知物理桶: {bucket!r}")


def _apply_asset_props(client, asset_id: str, items: list[Effect]) -> None:
    """分桶聚合：visibility + rating + description → **一次** PUT /assets。

    三个字段的取值语义不同，别混：
      · visibility：传字符串，客户端转枚举（AssetBulkUpdateDto.to_dict 调 .value）
      · rating：None 是合法值（"未评分"），不能被 `or` 吃掉
      · description：类型是 `str | Unset`，清空只能传 ""
    """
    kwargs: dict = {}
    for e in items:
        if e.action == "archive":
            kwargs["visibility"] = "archive"
        elif e.action == "timeline":
            kwargs["visibility"] = "timeline"
        elif e.action == "rating_set":
            kwargs["rating"] = e.payload["new"]
        elif e.action == "description_set":
            kwargs["description"] = e.payload["new"] or ""
    if not kwargs:
        return
    client.set_asset_properties([asset_id], **kwargs)


def _ledger(db: Database, items: list[Effect], job_id: int | None) -> None:
    """紧跟自己那一次成功的物理写入（独立事务，§5.5）。"""
    with db.connect() as conn:
        for e in items:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (:job_id, :aid, :action, 'apply', :payload)",
                {"job_id": job_id, "aid": e.asset_id, "action": e.action,
                 "payload": json.dumps(e.payload, ensure_ascii=False)},
            )
