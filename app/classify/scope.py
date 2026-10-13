"""Scope 匹配器与队列扫描。

本模块只筛选范围内带队列标签的图片，已完成过滤由 executor 处理。

in_scope 语义：
- 无启用规则（或只有禁用规则）→ True，全在范围；
- 有启用规则 → 任一命中即 in scope（library_id 相等 OR path 前缀命中）。
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from app.clients._immich_gen.models.asset_type_enum import AssetTypeEnum
from app.clients._immich_gen.models.metadata_search_dto import MetadataSearchDto
from app.clients._immich_gen.types import Unset
from app.store.events import log_event


@dataclass(frozen=True)
class ScopeRule:
    library_id: str
    path_prefix: str
    enabled: bool


def load_scope_rules(db) -> list[ScopeRule]:
    """读 scope_rules 表（v1.sql:34-39），只返回启用的规则。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT library_id, path_prefix, enabled FROM scope_rules WHERE enabled = 1"
        ).fetchall()
    return [
        ScopeRule(library_id=row["library_id"], path_prefix=row["path_prefix"], enabled=True)
        for row in rows
    ]


def _path_prefix_hit(prefix: str, path: str) -> bool:
    """路径段匹配，不是字符串前缀：/foo 匹配 /foo 和 /foo/bar，不匹配 /foobar。

    即 path 以 prefix 开头，且下一字符是 / 或已到结尾。
    """
    if not prefix or not path.startswith(prefix):
        return False
    rest = path[len(prefix) :]
    return rest == "" or rest.startswith("/")


def load_exclusions(db) -> list[tuple[str, str]]:
    """读 exclusions 表（v1.sql:66-72），只返回启用的规则。

    reviewed 规则 value 恒为空串，这里保留 (type, value) 形式与表一致，
    reviewed 分支不消费 value。
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT type, value FROM exclusions WHERE enabled = 1"
        ).fetchall()
    return [(row["type"], row["value"]) for row in rows]


def in_scope(rules: list[ScopeRule], *, library_id: str | None, path: str | None) -> bool:
    enabled = [r for r in rules if r.enabled]
    if not enabled:
        # 无启用规则 → 全在范围（见模块 docstring）
        return True
    for r in enabled:
        if library_id is not None and r.library_id and library_id == r.library_id:
            return True
        if path is not None and _path_prefix_hit(r.path_prefix, path):
            return True
    return False


def scan_queue_assets(client, db, queue_tag_name: str, rules) -> list[str]:
    """扫描范围内带队列标签的图片，返回 str 化 asset id 列表。

    队列标签按名查找，无则创建（ensure_tags）。
    统一将 asset.id 转为 str。
    排除规则在 scope 过滤之后应用，被排除者不计入返回候选。

    实现策略：path/reviewed 无需资产详情（original_path 候选自带、
    reviewed 查本地 asset_state）；album/tag 需要资产详情，对"还存在
    album/tag 排除规则"时幸存的候选逐个 client.get_asset 判定（N+1
    查询，可接受——scan 本来就逐张处理）。无此类规则时不调 get_asset。
    """
    tag_id = client.ensure_tags([queue_tag_name])[queue_tag_name]
    dto = MetadataSearchDto(type_=AssetTypeEnum.IMAGE, tag_ids=[tag_id])
    exclusions = load_exclusions(db)
    candidates = [
        asset
        for asset in _iter(client, dto)
        if in_scope(rules, library_id=asset.library_id, path=asset.original_path)
    ]
    if not exclusions:
        return [str(asset.id) for asset in candidates]

    # asset_state.reviewed 集合（本地查，无需 API）
    reviewed_ids: set[str] = set()
    if any(t == "reviewed" for t, _ in exclusions):
        with db.connect() as conn:
            reviewed_ids = {
                row["asset_id"]
                for row in conn.execute(
                    "SELECT asset_id FROM asset_state WHERE reviewed = 1"
                )
            }

    # album/tag 排除需要详情；只对相关规则存在时才逐个取详情
    detail_exclusions = [(t, v) for t, v in exclusions if t in ("album", "tag")]
    detail_needed = bool(detail_exclusions)
    details: dict[str, object] = {}
    if detail_needed:
        for asset in candidates:
            details[str(asset.id)] = client.get_asset(str(asset.id))

    result: list[str] = []
    for asset in candidates:
        aid = str(asset.id)
        hit_type = _exclusion_hit(
            exclusions,
            asset_id=aid,
            path=asset.original_path,
            reviewed_ids=reviewed_ids,
            detail=details.get(aid),
        )
        if hit_type is None:
            result.append(aid)
        else:
            log_event(db, "system", "asset_out_of_scope", {
                "asset_id": aid,
                "rule_type": hit_type,
            })
    return result


def _exclusion_hit(exclusions, *, asset_id: str, path, reviewed_ids: set[str],
                   detail) -> str | None:
    """返回命中的排除类型，未命中返回 None。"""
    for t, v in exclusions:
        if t == "path":
            if path is not None and _path_prefix_hit(v, path):
                return t
        elif t == "reviewed":
            if asset_id in reviewed_ids:
                return t
        elif t in ("album", "tag"):
            if detail is not None and _detail_hit(detail, t, v):
                return t
    return None


def _detail_hit(detail, type_: str, value: str) -> bool:
    """资产详情命中 album / tag 排除。

    album：Immich /assets/{id} 响应携带 albums 数组（生成的 DTO 未建模，
    落在 additional_properties 里），元素形如 {"id": ..., "albumName": ...}。
    tag：dto.tags 是 TagResponseDto 列表（未设置时为 UNSET）。
    """
    if type_ == "album":
        albums = getattr(detail, "additional_properties", {}).get("albums") or []
        for album in albums:
            name = album.get("albumName") or album.get("name")
            if name == value:
                return True
        return False
    tags = getattr(detail, "tags", None)
    if tags is None or isinstance(tags, Unset):
        return False
    return any(getattr(tag, "name", None) == value for tag in tags)


def _iter(client, dto: MetadataSearchDto) -> Iterator:
    yield from client.iter_assets(dto)


def iter_scope_assets(client, db, rules) -> Iterator[str]:
    """按 scope 规则迭代范围内所有图片（全量回填用）。

    与 scan_queue_assets 的差别：不要求资产带队列标签（dto.tag_ids 不设），
    也不做排除规则过滤——排除语义属扫描消费层，回填的目的正是把从未
    处理过的图（含被历史规则排除路径外的）补入队。
    """
    dto = MetadataSearchDto(type_=AssetTypeEnum.IMAGE)
    for asset in _iter(client, dto):
        if in_scope(rules, library_id=asset.library_id,
                    path=asset.original_path):
            yield str(asset.id)
