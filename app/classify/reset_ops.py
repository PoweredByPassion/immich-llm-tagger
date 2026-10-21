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


_RESET_DIMENSIONS = frozenset({"tag", "classification", "rating", "description"})


def run_reset_steps(db: Database, client, *, job_id: int | None,
                    is_canceled, dimensions: set[str] | None = None) -> dict:
    """按勾选维度执行：unarchive → 摘标签 → 移出相册 → 还原评分/描述 → 清本地列。

    dimensions=None 表示全集（等价于改动前的固定动作）。D12：**未勾选的
    维度一律不动** —— 整表 DELETE 只在四个维度全勾时才做（§5.10）。
    """
    dims = _RESET_DIMENSIONS if dimensions is None else frozenset(dimensions)
    result: dict = {"unarchived": 0, "untagged_tags": 0, "deleted_tags": 0,
                    "removed_from_album": 0, "restored_metadata": 0}

    # ① unarchive —— 属「分类」维度（D13：分类 = 归档 + 相册）
    if "classification" in dims and not is_canceled():
        asset_ids = _archived_asset_ids(db)
        if asset_ids:
            try:
                client.set_visibility(asset_ids, "timeline")
                result["unarchived"] = len(asset_ids)
            except ImmichError as exc:
                if exc.status_code == 404:
                    log_event(db, "system", "reset_unarchive_404", {
                        "job_id": job_id, "asset_ids": asset_ids})
                else:
                    raise
    if is_canceled():
        result["canceled"] = True
        return result

    # ② 摘管理标签 —— 属「标签」维度
    if "tag" in dims and not is_canceled():
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
                "asset_count": len(asset_ids)})
    if is_canceled():
        result["canceled"] = True
        return result

    # ③ 移出相册 —— 同属「分类」维度
    if "classification" in dims and not is_canceled():
        result["removed_from_album"] = _remove_from_albums(db, client, job_id)
    if is_canceled():
        result["canceled"] = True
        return result

    # ④ 还原本系统写过的评分/描述（带安全阀，见 _restore_metadata）
    if (dims & {"rating", "description"}) and not is_canceled():
        result["restored_metadata"] = _restore_metadata(db, client, dims, job_id)
    if is_canceled():
        result["canceled"] = True
        return result

    # ⑤ 本地 asset_state：列级（仅全勾时才整表 DELETE）
    _clear_state_columns(db, dims)
    return result


def _clear_state_columns(db: Database, dims: frozenset[str]) -> None:
    """按勾选维度**逐列**清理 asset_state（§5.10）。

    整表 DELETE 会连 current_tag / reviewed / manual_override 一起抹掉，
    直接违背 D12「未勾选的维度一律不动」——那正是二审揪出来的问题。

    ⚠ rating 与 description 必须**分开**判断：合起来写
    `if dims & {"rating","description"}: SET rating=NULL, description=NULL`
    会在只勾「评分」时顺手清掉本地镜像的描述，同样违背 D12。
    """
    with db.connect() as conn:
        if dims >= _RESET_DIMENSIONS:
            conn.execute("DELETE FROM asset_state")
            return
        if "tag" in dims:
            conn.execute("UPDATE asset_state SET current_tag = NULL, "
                         "current_markers = '[]'")
        if "classification" in dims:
            conn.execute("UPDATE asset_state SET visibility = NULL")
        if "rating" in dims:
            conn.execute("UPDATE asset_state SET rating = NULL")
        if "description" in dims:
            conn.execute("UPDATE asset_state SET description = NULL")
        if dims & {"rating", "description"}:
            # apply_attempts 归「评分/描述」维度的副作用：让"放弃重试"的资产
            # 恢复可处理（§7.3）。只清这些行，不整表清。
            conn.execute("UPDATE asset_state SET apply_attempts = 0")


def _remove_from_albums(db: Database, client, job_id: int | None) -> int:
    """把本系统加入过相册的资产移出（D13 的「分类」维度半边）。

    依据 change_log 的 album_add。用 list_albums() 先看相册还在不在 ——
    不能直接 ensure_album：它会**创建一个空相册**，用户删过的相册就凭空
    复活了（undo.py:110 有这个问题，本任务不顺手改它，改动面越小越好）。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT asset_id, payload FROM change_log "
            " WHERE action = 'album_add' AND direction = 'apply'").fetchall()
    by_album: dict[str, list[str]] = {}
    for row in rows:
        try:
            name = json.loads(row["payload"]).get("album_name")
        except (TypeError, ValueError):
            continue
        if name:
            by_album.setdefault(name, []).append(row["asset_id"])
    if not by_album:
        return 0

    existing = {a.album_name: str(a.id) for a in client.list_albums()}
    removed = 0
    for name, asset_ids in by_album.items():
        album_id = existing.get(name)
        if album_id is None:
            continue                      # 相册已被用户删掉 → 无需移出
        client.remove_from_album(album_id, asset_ids)
        with db.connect() as conn:
            for aid in asset_ids:
                conn.execute(
                    "INSERT INTO change_log (job_id, asset_id, action, "
                    "direction, payload) "
                    "VALUES (?, ?, 'album_remove', 'apply', ?)",
                    (job_id, aid, json.dumps({"album_name": name},
                                             ensure_ascii=False)))
        removed += 1
    return removed


def _restore_metadata(db: Database, client, dims: frozenset[str],
                      job_id: int | None) -> int:
    """还原本系统写过的评分/描述（§5.10）。

    安全阀：**仅当 Immich 当前值仍等于我们写入的 `new` 时才还原**；
    用户此后改过（current != last_new）→ 跳过，不抹掉他的编辑。
    还原目标取**最早一条 apply 记录的 `old`**（＝我们第一次写之前的值）。
    """
    from app.classify.snapshot import _val

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT asset_id, action, payload FROM change_log "
            " WHERE direction = 'apply' AND action IN ('rating_set','description_set') "
            " ORDER BY id").fetchall()

    first_old: dict[tuple[str, str], object] = {}
    last_new: dict[tuple[str, str], object] = {}
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        key = (r["asset_id"], r["action"])
        first_old.setdefault(key, p.get("old"))
        last_new[key] = p.get("new")

    restored = 0
    for (asset_id, action), old in first_old.items():
        field = "rating" if action == "rating_set" else "description"
        if field not in dims:
            continue
        try:
            exif = getattr(client.get_asset(asset_id), "exif_info", None)
            current = _val(getattr(exif, field, None)) if exif is not None else None
            if current != last_new[(asset_id, action)]:
                continue                      # 用户改过 → 不动
            if field == "rating":
                client.set_asset_properties([asset_id], rating=old)
            else:
                client.set_asset_properties([asset_id], description=old or "")
        except ImmichError as exc:
            if exc.status_code == 404:
                # change_log 引用的资产已被用户在 Immich 删除 → 跳过这一张，
                # 不让整个重置中途失败（镜像 unarchive 步骤的 404 处理）。
                log_event(db, "system", "reset_restore_404", {
                    "job_id": job_id, "asset_id": asset_id})
                continue
            raise
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (?, ?, ?, 'undo', ?)",
                (job_id, asset_id, action,
                 json.dumps({"old": last_new[(asset_id, action)], "new": old},
                            ensure_ascii=False)))
        restored += 1
    return restored


def reset_preview_counts(db: Database) -> dict[str, int]:
    """四个维度的**预估影响资产数**（§5.10）。

    全部来自本地 SQLite：Immich 没有批量取详情的端点，逐张 GET 在大库上
    必然 504。精确的安全阀判定只在真正执行时逐张做。

    四个维度统一用 `COUNT(DISTINCT asset_id)` —— 文案是"预估影响 N **张**"，
    混进"标签名个数"会让用户在两个单位之间对不上。口径也与实际执行一致：
    «标签» 维度摘的就是 tag_add 记录覆盖的那批资产（`_tagged_asset_map`）。
    """
    with db.connect() as conn:
        def assets(action: str) -> int:
            return conn.execute(
                "SELECT COUNT(DISTINCT asset_id) c FROM change_log "
                " WHERE action = ? AND direction = 'apply'", (action,),
            ).fetchone()["c"]
        return {
            "tag": assets("tag_add"),
            "classification": assets("archive"),
            "rating": assets("rating_set"),
            "description": assets("description_set"),
        }
