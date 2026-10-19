"""标签 effector：主标签 + markers。

跳过标记标签**不在这里**产出 —— 它由管线在 plan_all 之后按
outcome.skipped 追加，保持 planner 之间零引用（§5.4）。
"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext


def plan(ctx: EffectContext, last_written: dict) -> list[Effect]:
    if "tag" not in ctx.enabled or ctx.gates.blocks_all:
        return []
    # 资产已经拥有的标签不再重打：Immich 的批量打标端点对"已打过的标签"返回
    # success=false / error=duplicate，整批（就这张图而言）算失败 → 走到
    # apply 失败重试 → 三次后放弃并打上跳过标记（真机事故：拒答图重复打
    # sensitive，apply_gave_up）。快照里已经有 tags，plan 阶段就能判掉。
    owned = set(ctx.snapshot.tags)
    seen, out = set(), []
    for name in (ctx.analysis.tag, *ctx.analysis.markers):
        if name and name not in seen and name not in owned:
            seen.add(name)
            out.append(Effect("tag_add", ctx.asset_id, {"tag_name": name}))
    return out
