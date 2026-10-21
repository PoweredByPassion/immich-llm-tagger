"""重新分类：摘掉本系统管理的旧标签并重新执行分类管线。

语义：摘掉"本系统管理的标签"（系统管理标签 ∩ asset 实际拥有）→ 写
change_log（tag_remove）→ 清空 current_tag/current_markers 并更新 last_job_id
→ 调用 classify_asset 重跑（source="manual"，绕过已完成过滤）。
"""
from __future__ import annotations

import json

from app.classify.manual import _managed_tag_names, owned_tag_names
from app.classify.pipeline import PipelineConfig, PipelineResult, classify_asset
from app.db.connection import Database


def reclassify_asset(
    db: Database,
    client,
    llm,
    cfg: PipelineConfig,
    asset_id: str,
    *,
    job_id: int,
    prompt_version_id: int | None = None,
) -> PipelineResult:
    asset_id = str(asset_id)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM asset_state WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    state = dict(row) if row else None

    # ① 摘旧：本系统管理 ∩ 实际拥有
    managed = _managed_tag_names(db, state)
    to_remove = sorted(managed & owned_tag_names(client, asset_id))
    if to_remove:
        tag_ids = client.ensure_tags(to_remove)
        for name in to_remove:
            client.untag_assets(tag_ids[name], [asset_id])

    # ② change_log + 清空 state（单事务）。manual_override 位不动。
    with db.connect() as conn:
        for name in to_remove:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, "
                "payload) VALUES (:job_id, :aid, 'tag_remove', 'apply', :payload)",
                {"job_id": job_id, "aid": asset_id,
                 "payload": json.dumps({"tag_name": name})},
            )
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "                        apply_attempts, last_job_id) "
            "VALUES (:aid, NULL, '[]', 0, :job_id) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "  current_tag = NULL, current_markers = '[]', "
            "  apply_attempts = 0, "
            "  last_job_id = :job_id, updated_at = datetime('now')",
            {"aid": asset_id, "job_id": job_id},
        )

    # ③ 重跑：source="manual" 绕 _already_done；override_manual=False
    # 走全量 apply（归档/相册照常写，§5.5 显式发起不受改判限制）。
    # DB 里的 manual_override 位不动——只影响本次 apply 行为。
    return classify_asset(
        db, client, llm, asset_id, cfg,
        source="manual", dry_run=False, job_id=job_id,
        prompt_version_id=prompt_version_id, override_manual=False,
        allow_metadata_overwrite=True,
    )
