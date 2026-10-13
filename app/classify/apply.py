"""应用器（Applier）：把分类结果执行到 Immich 并更新本地状态。

应用顺序：① 标签 → ② 可见性 → ③ 相册 → ④ 摘队列标签 → ⑤ 本地状态。
先写新标签、最后摘队列标签 —— 中途崩溃不会让图片「既没标签又不在队列」。
本地写（多条 change_log + asset_state upsert）合并为单个事务。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.clients.immich import ImmichClient
from app.db.connection import Database
from app.store.assets import has_system_archive, write_changes_and_state


@dataclass(frozen=True)
class ApplyPlan:
    asset_id: str
    primary_tag: str
    marker_tags: tuple[str, ...]
    archive: bool  # 最终归档决定
    album: str | None
    queue_tag: str
    skip_immich: bool = False  # True 时只摘队列标签 + 落本地状态，不写 Immich


def apply_plan(
    db: Database,
    client: ImmichClient,
    plan: ApplyPlan,
    *,
    job_id: int,
    queue_tag_id: str,
    manual_override: bool,
) -> None:
    # ① 主标签 + marker 标签（全部写入）；skip_immich 的标签不写 Immich，
    #    也不落 tag_add 变更日志（change_log 描述"改过 Immich"）。
    all_tags = [plan.primary_tag, *plan.marker_tags]
    changes: list[tuple[str, str, str]] = []
    if not plan.skip_immich:
        tag_ids = client.ensure_tags(all_tags)
        client.tag_assets(
            [tag_ids[plan.primary_tag], *(tag_ids[m] for m in plan.marker_tags)],
            [plan.asset_id],
        )
        changes.append(
            ("tag_add", plan.asset_id, json.dumps({"tag_name": plan.primary_tag}))
        )
        changes.extend(
            ("tag_add", plan.asset_id, json.dumps({"tag_name": m}))
            for m in plan.marker_tags
        )

    # ② 可见性：只有本系统的处理才主动设置（含改判回 timeline）。
    #    用户自行归档且与本系统无关的图，不要动它的可见性。
    #    skip_immich：只查询现状（本地状态需要），不设置。
    if manual_override or plan.skip_immich:
        visibility = client.get_asset(plan.asset_id).visibility
    elif plan.archive:
        client.set_visibility([plan.asset_id], "archive")
        visibility = "archive"
        changes.append(
            ("archive", plan.asset_id, json.dumps({"from_visibility": "timeline"}))
        )
    elif has_system_archive(db, plan.asset_id):
        client.set_visibility([plan.asset_id], "timeline")
        visibility = "timeline"
        changes.append(
            ("timeline", plan.asset_id, json.dumps({"from_visibility": "archive"}))
        )
    else:
        visibility = client.get_asset(plan.asset_id).visibility

    # ③ 相册（manual_override / skip_immich 跳过）
    if plan.album is not None and not manual_override and not plan.skip_immich:
        album_id = client.ensure_album(plan.album)
        client.add_to_album(album_id, [plan.asset_id])
        changes.append(
            ("album_add", plan.asset_id, json.dumps({"album_name": plan.album}))
        )

    # ④ 最后摘队列标签
    client.untag_assets(queue_tag_id, [plan.asset_id])
    changes.append(
        ("queue_remove", plan.asset_id, json.dumps({"queue_tag_name": plan.queue_tag}))
    )

    # ⑤ 本地状态：本模块的 change_log + asset_state 在单个事务里提交
    with db.connect() as conn:
        write_changes_and_state(
            conn,
            asset_id=plan.asset_id,
            job_id=job_id,
            changes=changes,
            current_tag=plan.primary_tag,
            current_markers=list(plan.marker_tags),
            visibility=visibility,
        )
