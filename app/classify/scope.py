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
    if prefix == "/":
        return path.startswith("/")
    prefix = prefix.rstrip("/")
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
        if library_id is not None and r.library_id and str(library_id) == r.library_id:
            return True
        if isinstance(path, str) and _path_prefix_hit(r.path_prefix, path):
            return True
    return False


def check_auto_eligibility(
    client, db, asset_id: str, *, asset=None, rules=None, exclusions=None,
) -> tuple[bool, str | None, object]:
    """Evaluate automatic processing policy using data retrieved from Immich."""
    canonical = asset is None
    if canonical:
        asset = client.get_asset(asset_id)
    if rules is None:
        rules = load_scope_rules(db)
    if exclusions is None:
        exclusions = load_exclusions(db)

    asset_type = getattr(asset, "type_", None if canonical else AssetTypeEnum.IMAGE)
    if asset_type != AssetTypeEnum.IMAGE:
        return False, "not_image", asset
    path = getattr(asset, "original_path", None)
    library = getattr(asset, "library_id", None)
    if not in_scope(rules, library_id=library, path=path):
        return False, "scope", asset

    paths = {value for kind, value in exclusions if kind == "path"}
    if paths and not isinstance(path, str):
        return False, "path_metadata_unavailable", asset
    if any(_path_prefix_hit(prefix, path) for prefix in paths):
        return False, "path", asset
    if any(kind == "reviewed" for kind, _ in exclusions):
        with db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM asset_state WHERE asset_id = ? AND reviewed = 1",
                (asset_id,),
            ).fetchone():
                return False, "reviewed", asset

    tag_names = {value for kind, value in exclusions if kind == "tag"}
    if tag_names:
        tags = getattr(asset, "tags", None)
        if not canonical and (tags is None or isinstance(tags, Unset)):
            asset = client.get_asset(asset_id)
            tags = getattr(asset, "tags", None)
        if tags is None or isinstance(tags, Unset):
            return False, "tag_metadata_unavailable", asset
        if any(tag.name in tag_names for tag in tags):
            return False, "tag", asset

    album_names = {value for kind, value in exclusions if kind == "album"}
    if album_names and any(
        album.album_name in album_names for album in client.list_asset_albums(asset_id)
    ):
        return False, "album", asset
    return True, None, asset


def iter_queue_assets(client, db, queue_tag_name: str, rules) -> Iterator[str]:
    tag_id = client.ensure_tags([queue_tag_name])[queue_tag_name]
    dto = MetadataSearchDto(type_=AssetTypeEnum.IMAGE, tag_ids=[tag_id])
    yield from _iter_eligible(client, db, dto, rules)


def scan_queue_assets(client, db, queue_tag_name: str, rules) -> list[str]:
    return list(iter_queue_assets(client, db, queue_tag_name, rules))


def _iter_eligible(client, db, dto, rules) -> Iterator[str]:
    exclusions = load_exclusions(db)
    for asset in _iter(client, dto):
        asset_id = str(asset.id)
        eligible, reason, _ = check_auto_eligibility(
            client, db, asset_id, asset=asset, rules=rules, exclusions=exclusions,
        )
        if eligible:
            yield asset_id
        elif reason not in {"scope", "not_image"}:
            log_event(db, "system", "asset_out_of_scope", {
                "asset_id": asset_id, "rule_type": reason,
            })


def _iter(client, dto: MetadataSearchDto) -> Iterator:
    yield from client.iter_assets(dto)


def iter_scope_assets(client, db, rules) -> Iterator[str]:
    """Yield eligible images for backfill, regardless of current queue tag."""
    dto = MetadataSearchDto(type_=AssetTypeEnum.IMAGE)
    yield from _iter_eligible(client, db, dto, rules)
