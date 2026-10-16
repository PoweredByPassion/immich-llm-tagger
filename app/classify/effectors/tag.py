"""标签 effector：主标签 + markers。

跳过标记标签**不在这里**产出 —— 它由管线在 plan_all 之后按
outcome.skipped 追加，保持 planner 之间零引用（§5.4）。
"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext


def plan(ctx: EffectContext, last_written: dict) -> list[Effect]:
    if "tag" not in ctx.enabled or ctx.gates.blocks_all:
        return []
    seen, out = set(), []
    for name in (ctx.analysis.tag, *ctx.analysis.markers):
        if name and name not in seen:
            seen.add(name)
            out.append(Effect("tag_add", ctx.asset_id, {"tag_name": name}))
    return out
