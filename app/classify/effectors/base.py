"""效应层契约：Effect 值对象、规划上下文、门控、结果。

Planner 是**纯函数**：只读 ctx 与 last_written，返回 Effect 列表，
不碰 DB / 网络，互不引用（唯一跨 effector 信息流是 outcome.skipped → 提交器）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.classify.parse import Classification
from app.classify.snapshot import AssetSnapshot

# 物理端点分桶（§5.5）。队列桶**不来自任何 planner** —— 它是提交器的
# Finalizer 阶段（§5.6），这里只作为错误上报时的桶名常量。
BUCKET_ASSET_PROPS = "asset_props"   # 合并成一次 PUT /assets
BUCKET_TAGS = "tags"
BUCKET_ALBUM = "album"
BUCKET_QUEUE = "queue"

_ASSET_PROP_ACTIONS = {"archive", "timeline", "rating_set", "description_set"}


def bucket_of(action: str) -> str:
    if action in _ASSET_PROP_ACTIONS:
        return BUCKET_ASSET_PROPS
    if action in ("album_add", "album_remove"):
        return BUCKET_ALBUM
    if action in ("tag_add", "tag_remove"):
        return BUCKET_TAGS
    raise ValueError(f"没有物理桶的 action: {action!r}")


@dataclass(frozen=True)
class Effect:
    action: str
    asset_id: str
    payload: dict[str, Any]

    @property
    def bucket(self) -> str:
        return bucket_of(self.action)


@dataclass(frozen=True)
class Gates:
    """统一门控（§5.7）。由提交器统一裁决，planner 只读。"""

    dry_run: bool = False
    manual_override: bool = False
    skip_immich: bool = False
    allow_metadata_overwrite: bool = False

    @property
    def blocks_all(self) -> bool:
        """一个 Effect 都不产出（标签也不写）。"""
        return self.dry_run or self.skip_immich

    @property
    def metadata_blocked(self) -> bool:
        """评分/描述/可见性/相册是否被禁止写入（标签不受影响）。"""
        return self.blocks_all or self.manual_override


@dataclass(frozen=True)
class EffectContext:
    asset_id: str
    analysis: Classification
    snapshot: AssetSnapshot
    tag_def: Any | None            # store.tags.TagDef（选中的主标签）
    archive: bool
    enabled: set[str]
    gates: Gates


@dataclass
class PlanOutcome:
    """规划期的旁路结果（不打标签、不记账）。"""

    skipped: list[str] = field(default_factory=list)   # ["rating"] / ["description"]


def tag_writes(ctx: EffectContext, field: str) -> bool:
    """该标签是否允许写这个字段（v12 的 per-tag 开关：write_rating / write_description）。

    与 archive（是否归档）、album（进哪个相册）、skip_immich（是否写 Immich）同一套
    「标签即开关」模型 —— 评分/描述不再是全局效应器开关。

    tag_def 为 None（模型给的 tag 没匹配上任何启用的主标签）时返回 True：
    没有标签定义就没有可读的开关，维持既有行为（照常写）。
    """
    return bool(getattr(ctx.tag_def, field, True))
