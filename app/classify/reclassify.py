"""Reclassify using fresh analysis while holding the asset's existing lease."""
from __future__ import annotations

from app.classify.inverse import reconcile_pending_inverse
from app.classify.lease import LLM_MAX_RETRIES, claim, lease_seconds_for, release
from app.classify.ownership import ALBUM, TAG, matching_resource, owned_memberships
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
    ttl = lease_seconds_for(cfg.llm.timeout, LLM_MAX_RETRIES)
    if not claim(db, asset_id, cfg.concurrency_owner, ttl):
        return PipelineResult(asset_id, "", None, (), "error", False)
    try:
        reconcile_pending_inverse(db, client, asset_id)
        owned_tags = owned_memberships(db, asset_id, TAG)
        tags = (client.get_asset(asset_id).tags or []) if owned_tags else []
        tag_ids = {match[1]: match[0] for change in owned_tags
                   if (match := matching_resource(change, tags))}
        owned_albums = owned_memberships(db, asset_id, ALBUM)
        albums = client.list_asset_albums(asset_id) if owned_albums else []
        album_ids = {match[1]: match[0] for change in owned_albums
                     if (match := matching_resource(change, albums))}
        return classify_asset(
            db, client, llm, asset_id, cfg,
            source="manual", dry_run=False, job_id=job_id,
            prompt_version_id=prompt_version_id, override_manual=False,
            allow_metadata_overwrite=True, force_reanalysis=True,
            lease_held=True, replace_owned_tags=tag_ids,
            replace_owned_albums=album_ids,
        )
    finally:
        release(db, asset_id, cfg.concurrency_owner)
