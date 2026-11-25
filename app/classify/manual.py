"""人工改判与标记已复核。

与自动分类（提交器 app/classify/commit.py）的区别：
- 改判不是自动分类：无 LLM 调用，不写 asset_records；
- 不动可见性与相册（manual_override 跳过可见性与相册修改）；
- change_log 的 job_id 为 NULL —— 改判不隶属任何 job；
- asset_state.last_job_id 保持原值不动：改判不属于 job，不覆盖它。

仅替换有变更记录且仍由系统管理的标签。
"""
from __future__ import annotations

import json

from app.classify.commit import commit_effects, retire_pending_effects
from app.classify.effectors.base import Effect, Gates
from app.classify.inverse import reconcile_pending_inverse
from app.classify.ownership import (
    TAG,
    matching_resource,
    mutation_lease,
    owned_memberships,
)
from app.clients.immich import ImmichClient, ImmichError
from app.db.connection import Database
from app.store import jobs
from app.store.events import log_event


def _upsert_state_bit(db: Database, asset_id: str, *, field: str, value: int) -> None:
    """只设 manual_override/reviewed 一位；无行则 INSERT（含该位值，其余默认）。"""
    with db.connect() as conn:
        conn.execute(
            f"INSERT INTO asset_state (asset_id, {field}) VALUES (:aid, :value) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            f"  {field} = :value, updated_at = datetime('now')",
            {"aid": asset_id, "value": value},
        )


def mark_reviewed(db: Database, asset_id: str, *, reviewed: bool = True) -> None:
    """标记已复核（或取消）。无 Immich 变更，不写 change_log。
    取消复核时无 state 行则为空操作（不凭空造行）。"""
    if not reviewed:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM asset_state WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        if row is None:
            return
    _upsert_state_bit(db, asset_id, field="reviewed", value=1 if reviewed else 0)
    log_event(
        db, "system", "asset_reviewed", {"asset_id": asset_id, "reviewed": reviewed}
    )


def clear_override(db: Database, asset_id: str) -> None:
    """取消改判：仅恢复"参与自动处理"资格（manual_override=0），不回滚任何标签。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM asset_state WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    if row is None:
        return
    _upsert_state_bit(db, asset_id, field="manual_override", value=0)


def apply_override(
    db: Database,
    client: ImmichClient,
    asset_id: str,
    new_tag: str,
    marker_tags: list[str] | None = None,
) -> None:
    """Apply the selected tags before removing previously managed memberships."""
    with jobs.asset_mutation(db), mutation_lease(db, asset_id) as acquired:
        if not acquired:
            raise ImmichError("资产正在处理，请稍后重试", status_code=409)
        reconcile_pending_inverse(db, client, asset_id)
        retire_pending_effects(db, client, asset_id)
        _apply_selected_tags(db, client, asset_id, new_tag, marker_tags or [])


def _apply_selected_tags(db, client, asset_id, new_tag, marker_tags) -> None:
    kinds = {new_tag: "primary"}
    kinds.update({name: "marker" for name in marker_tags if name != new_tag})
    markers = [name for name in kinds if name != new_tag]
    tags = client.get_asset(asset_id).tags or []
    existing = {tag.name: str(tag.id) for tag in tags}
    managed = {}
    for change in owned_memberships(db, asset_id, TAG):
        if match := matching_resource(change, tags):
            managed[match[1]] = match[0]

    additions = [name for name in kinds if name not in existing]
    tag_ids = client.ensure_tags(additions) if additions else {}
    effects = [
        Effect("tag_add", asset_id, {"tag_name": name, "tag_id": tag_ids[name], "tag_kind": kinds[name]})
        for name in additions
    ]
    for name, tag_id in managed.items():
        if name not in kinds:
            effects.append(Effect("tag_remove", asset_id, {"tag_name": name, "tag_id": tag_id}))

    if effects:
        applied = commit_effects(db, client, effects, asset_id=asset_id, job_id=None,
                                 queue_tag_id=None, queue_tag_name="", gates=Gates())
        if not applied.ok:
            raise ImmichError(applied.error or "Could not apply the selected tags")

    for name in managed.keys() & kinds.keys():
        _write_tag_change(db, asset_id, "tag_add", name, managed[name], protected=True,
                          tag_kind=kinds[name])

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "                        manual_override) "
            "VALUES (:aid, :tag, :markers, 1) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "  current_tag = excluded.current_tag, "
            "  current_markers = excluded.current_markers, "
            "  manual_override = 1, updated_at = datetime('now')",
            {"aid": asset_id, "tag": new_tag, "markers": json.dumps(markers)},
        )

    log_event(db, "system", "asset_manual_override",
              {"asset_id": asset_id, "tag": new_tag, "markers": markers})


def _write_tag_change(db, asset_id, action, name, tag_id, **fields) -> None:
    payload = json.dumps({"tag_name": name, "tag_id": str(tag_id), **fields},
                         ensure_ascii=False)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (NULL, ?, ?, 'apply', ?)",
            (asset_id, action, payload),
        )
