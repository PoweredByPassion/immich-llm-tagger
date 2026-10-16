"""效应层注册表。新增 effector = 新模块 + 在这里登记一行。"""
from __future__ import annotations

from app.classify.effectors import album, description, rating, tag, visibility
from app.classify.effectors.base import Effect, EffectContext, PlanOutcome


def plan_all(ctx: EffectContext, *, last_written: dict | None = None,
             outcome: PlanOutcome | None = None,
             has_system_archive: bool = False) -> list[Effect]:
    """按固定顺序规划全部 effector。顺序即提交顺序的前半段。

    唯一跨 effector 的信息流：rating/description 往 outcome.skipped 里
    写事实，管线据此决定是否打跳过标记 —— planner 之间不互相引用。
    """
    last_written = last_written or {}
    outcome = outcome if outcome is not None else PlanOutcome()
    effects: list[Effect] = []
    effects += tag.plan(ctx, last_written)
    effects += visibility.plan(ctx, last_written, has_system_archive)
    effects += album.plan(ctx, last_written)
    effects += rating.plan(ctx, last_written, outcome)
    effects += description.plan(ctx, last_written, outcome)
    return effects


__all__ = ["Effect", "EffectContext", "PlanOutcome", "plan_all"]
