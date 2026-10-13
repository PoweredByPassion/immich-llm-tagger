"""人工改判与标记已复核。

与 apply.py 的区别：
- 改判不是自动分类：无 LLM 调用，不写 asset_records；
- 不动可见性与相册（manual_override 跳过可见性与相册修改）；
- change_log 的 job_id 为 NULL —— 改判不隶属任何 job；
- asset_state.last_job_id 保持原值不动：改判不属于 job，不覆盖它。

本系统管理的标签＝tags 表全部标签名 ∪ asset_state 当前 current_tag/current_markers。
且只在 Immich 上摘除该 asset 实际拥有的这些标签。
"""
from __future__ import annotations

import json

from app.clients.immich import ImmichClient
from app.db.connection import Database
from app.store.events import log_event
from app.store.tags import load_tags


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


def _managed_tag_names(db: Database, state: dict | None) -> set[str]:
    """本系统管理的标签名 = tags 表全部 ∪ asset_state 当前值。
    供 manual.apply_override 与 reclassify.reclassify_asset 共用。"""
    names = {t.name for t in load_tags(db)}
    if state is not None:
        if state.get("current_tag"):
            names.add(state["current_tag"])
        try:
            names.update(json.loads(state.get("current_markers") or "[]"))
        except (TypeError, ValueError):
            pass
    return names


def owned_tag_names(client, asset_id: str) -> set[str]:
    """该 asset 在 Immich 上实际拥有的标签名。"""
    tags = client.get_asset(asset_id).tags or []
    return {t.name for t in tags}


def apply_override(
    db: Database,
    client: ImmichClient,
    asset_id: str,
    new_tag: str,
    marker_tags: list[str] | None = None,
) -> None:
    """人工改判：摘旧（本系统管理 ∩ 实际拥有）→ 打新 → change_log(job_id=NULL)
    → asset_state(manual_override=1)。不动可见性 / 相册 / asset_records。"""
    marker_tags = list(marker_tags or [])
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM asset_state WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    state = dict(row) if row else None

    # ① 摘旧：管理标签 ∩ 该 asset 在 Immich 实际拥有的标签
    managed = _managed_tag_names(db, state)
    owned_names = owned_tag_names(client, asset_id)
    to_remove = sorted(managed & owned_names - {new_tag, *marker_tags})
    if to_remove:
        tag_ids = client.ensure_tags(to_remove)
        for name in to_remove:
            client.untag_assets(tag_ids[name], [asset_id])

    # ② 打新
    new_ids = client.ensure_tags([new_tag, *marker_tags])
    client.tag_assets(
        [new_ids[new_tag], *(new_ids[m] for m in marker_tags)], [asset_id]
    )

    # ③ change_log（job_id=NULL）+ asset_state，单事务
    changes = [("tag_remove", asset_id, json.dumps({"tag_name": n}))
               for n in to_remove]
    changes.append(("tag_add", asset_id, json.dumps({"tag_name": new_tag})))
    changes.extend(("tag_add", asset_id, json.dumps({"tag_name": m}))
                   for m in marker_tags)
    with db.connect() as conn:
        for action, aid, payload in changes:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (NULL, :aid, :action, 'apply', :payload)",
                {"aid": aid, "action": action, "payload": payload},
            )
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "                        manual_override) "
            "VALUES (:aid, :tag, :markers, 1) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "  current_tag = excluded.current_tag, "
            "  current_markers = excluded.current_markers, "
            "  manual_override = 1, updated_at = datetime('now')",
            {"aid": asset_id, "tag": new_tag, "markers": json.dumps(marker_tags)},
        )

    log_event(
        db,
        "system",
        "asset_manual_override",
        {"asset_id": asset_id, "tag": new_tag, "markers": marker_tags},
    )
