"""评分 effector：标签开关（write_rating）+ 字段非空判定 + 三步跳过规则（§5.8.1）。"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext, PlanOutcome, tag_writes


def plan(ctx: EffectContext, last_written: dict, outcome: PlanOutcome) -> list[Effect]:
    # 标签声明不写评分 → 直接返回，**不**记 outcome.skipped：
    # 跳过标记（"保留人工信息"）的语义是"我们自己想写但被你写的值挡住"，
    # 这里压根没打算写，打标记反而误导。
    if ctx.gates.metadata_blocked or not tag_writes(ctx, "write_rating"):
        return []
    new = ctx.analysis.rating
    if new is None:
        return []

    current = ctx.snapshot.rating
    if current is None:
        return [Effect("rating_set", ctx.asset_id, {"old": None, "new": new})]

    if last_written.get("rating_set") == current:
        # 是我们自己上次写的
        if ctx.gates.allow_metadata_overwrite and current != new:
            return [Effect("rating_set", ctx.asset_id, {"old": current, "new": new})]
        return []                      # 值已正确；不打"保留人工信息"
    outcome.skipped.append("rating")   # 用户写的 → 跳过 + 打标记
    return []
