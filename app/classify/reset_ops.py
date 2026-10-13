"""危险区重置逻辑。

范围（避免误伤用户在 Immich 自行管理的数据）：
1. unarchive：只恢复本系统曾归档的 asset，set_visibility timeline。
2. 摘标签：只对本系统打过标的资产摘除管理标签（标签实体保留，不动用户手工标签）。
3. 清空本地 asset_state，change_log 保留供历史追溯。
"""
from __future__ import annotations

import json

from app.clients.immich import ImmichError
from app.classify.manual import _managed_tag_names
from app.db.connection import Database
from app.store.events import log_event


def _archived_asset_ids(db: Database) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT asset_id FROM change_log "
            "WHERE action = 'archive' AND direction = 'apply'"
        ).fetchall()
    return [r["asset_id"] for r in rows]


def _state_managed_names(db: Database) -> set[str]:
    """所有 asset_state 行的 current_tag / current_markers 并集。"""
    names: set[str] = set()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT current_tag, current_markers FROM asset_state"
        ).fetchall()
    for row in rows:
        names.update(_managed_tag_names(db, dict(row)))
    return names


def reset_preview(db: Database, client) -> tuple[int, list[str]]:
    """(unarchive 的 asset 数, 将摘除的标签名列表)。"""
    unarchive_count = len(_archived_asset_ids(db))
    managed = {t.name for t in _load_tag_defs(db)} | _state_managed_names(db)
    existing = {t.name for t in client.list_tags()}
    return unarchive_count, sorted(managed & existing)


def _load_tag_defs(db: Database) -> list:
    from app.store.tags import load_tags
    return load_tags(db)


def _tagged_asset_map(db: Database, managed: set[str]) -> dict[str, list[str]]:
    """tag_name -> 曾被本系统打标的 asset_id 列表。

    口径：change_log 中 action='tag_add' AND direction='apply' 的
    (payload.tag_name, asset_id)；含改判路径（job_id=NULL）的记录。
    undo 不做抵消——untag 对未打标资产是 no-op，多余调用无害。
    """
    if not managed:
        return {}
    out: dict[str, list[str]] = {name: [] for name in managed}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT asset_id, payload FROM change_log "
            "WHERE action = 'tag_add' AND direction = 'apply'"
        ).fetchall()
    for row in rows:
        try:
            name = json.loads(row["payload"]).get("tag_name")
        except (TypeError, ValueError):
            continue
        if name in out:
            out[name].append(row["asset_id"])
    return out


def run_reset_steps(db: Database, client, *, job_id: int | None,
                    is_canceled) -> dict:
    """顺序执行：unarchive → 摘标签 → 清 asset_state。is_canceled 在
    每步之间轮询；取消则提前返回（canceled=True，已完成步骤的结果保留）。
    返回计数字典供 executor 的 job_complete 事件使用。"""
    result: dict = {"unarchived": 0, "untagged_tags": 0, "deleted_tags": 0}

    # ① unarchive
    if not is_canceled():
        asset_ids = _archived_asset_ids(db)
        if asset_ids:
            try:
                client.set_visibility(asset_ids, "timeline")
                result["unarchived"] = len(asset_ids)
            except ImmichError as exc:
                if exc.status_code == 404:
                    log_event(db, "system", "reset_unarchive_404", {
                        "job_id": job_id, "asset_ids": asset_ids,
                    })
                else:
                    raise
    if is_canceled():
        result["canceled"] = True
        return result

    # ② 摘管理标签（非破坏性：只摘本系统打过的资产，标签实体保留）
    if not is_canceled():
        _, tag_names = reset_preview(db, client)
        existing = {t.name: str(t.id) for t in client.list_tags()}
        asset_map = _tagged_asset_map(db, set(tag_names))
        for name in tag_names:
            asset_ids = asset_map.get(name, [])
            if name not in existing or not asset_ids:
                continue
            client.untag_assets(existing[name], asset_ids)
            result["untagged_tags"] += 1
            with db.connect() as conn:
                for aid in asset_ids:
                    conn.execute(
                        "INSERT INTO change_log (job_id, asset_id, action, "
                        "direction, payload) "
                        "VALUES (?, ?, 'tag_remove', 'apply', ?)",
                        (job_id, aid, json.dumps({"tag_name": name})),
                    )
            log_event(db, "system", "reset_tag_untagged", {
                "job_id": job_id, "tag_name": name,
                "asset_count": len(asset_ids),
            })
    if is_canceled():
        result["canceled"] = True
        return result

    # ③ 清空 asset_state（change_log / jobs / asset_records 保留）
    with db.connect() as conn:
        conn.execute("DELETE FROM asset_state")
    return result
