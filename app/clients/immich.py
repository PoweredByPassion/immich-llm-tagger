"""Immich API 适配层。

底层是 `_immich_gen/` 里由官方 OpenAPI 规范（v3.3.1）生成的客户端；
本模块是**唯一**的业务出口，负责三件事：

1. **URL 前缀**：规范里的路径不带 /api（如 /tags），而服务端挂在 /api 下，
   所以 base_url 必须补上 /api —— 漏了会全部 404。
2. **鉴权头**：生成器默认发 Authorization: Bearer，而 Immich 用 x-api-key，
   靠 prefix="" + auth_header_name="x-api-key" 覆盖 —— 漏了会全部 401。
3. **错误归一**：把生成器的异常/意外状态码统一成 ImmichError，
   调用方只需 catch 一种异常。

`_immich_gen/` 由 `make generate` 产出，gitignored，**不要手改、不要评审、不要计入覆盖率**。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from app.clients._immich_gen.api.albums import add_assets_to_album as _add_assets_to_album
from app.clients._immich_gen.api.albums import (
    remove_asset_from_album as _remove_asset_from_album,
)
from app.clients._immich_gen.api.albums import create_album as _create_album
from app.clients._immich_gen.api.albums import get_all_albums as _get_all_albums
from app.clients._immich_gen.api.assets import get_asset_statistics
from app.clients._immich_gen.api.assets import (
    get_asset_info as _get_asset_info,
)
from app.clients._immich_gen.api.libraries import (
    get_all_libraries as _get_all_libraries,
)
from app.clients._immich_gen.api.search import search_assets
from app.clients._immich_gen.api.assets import update_assets as _update_assets
from app.clients._immich_gen.api.assets import view_asset as _view_asset
from app.clients._immich_gen.api.tags import delete_tag as _delete_tag
from app.clients._immich_gen.api.tags import get_all_tags
from app.clients._immich_gen.api.tags import (
    tag_assets as _tag_assets,
)
from app.clients._immich_gen.api.tags import (
    untag_assets as _untag_assets,
)
from app.clients._immich_gen.api.tags import (
    upsert_tags as _upsert_tags,
)
from app.clients._immich_gen.client import AuthenticatedClient
from app.clients._immich_gen.models.album_response_dto import AlbumResponseDto
from app.clients._immich_gen.models.asset_bulk_update_dto import AssetBulkUpdateDto
from app.clients._immich_gen.models.asset_media_size import AssetMediaSize
from app.clients._immich_gen.models.asset_response_dto import AssetResponseDto
from app.clients._immich_gen.models.asset_stats_response_dto import AssetStatsResponseDto
from app.clients._immich_gen.models.asset_visibility import AssetVisibility
from app.clients._immich_gen.models.bulk_id_response_dto import BulkIdResponseDto
from app.clients._immich_gen.models.bulk_ids_dto import BulkIdsDto
from app.clients._immich_gen.models.create_album_dto import CreateAlbumDto
from app.clients._immich_gen.models.library_response_dto import LibraryResponseDto
from app.clients._immich_gen.models.metadata_search_dto import MetadataSearchDto
from app.clients._immich_gen.models.tag_response_dto import TagResponseDto
from app.clients._immich_gen.models.tag_upsert_dto import TagUpsertDto
from app.clients._immich_gen.types import UNSET, Response, Unset

DEFAULT_TIMEOUT = 120.0

# 批量端点的每批上限分块大小。
TAG_ASSET_BATCH_SIZE = 1000  # 打标 / 摘标
ASSET_UPDATE_BATCH_SIZE = 500  # 批量可见性更新（PUT /assets）
ALBUM_ASSET_BATCH_SIZE = 500  # 相册批量添加资产


class ImmichError(RuntimeError):
    """Immich 调用失败（网络错误、非 2xx、或响应无法解析）。

    status_code 在**非 2xx** 路径上携带服务端状态码（int）；
    网络/解析失败路径上为 None。调用方可用它区分 404 与其他错误。
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# iter_assets 分页循环的整体时间上限（秒）：大库止损，超时返回已收集部分。
ITER_TIME_LIMIT = 300.0


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        httpx_args: dict[str, Any] | None = None,
    ) -> None:
        self._raw_base_url = base_url.rstrip("/")
        api_base = f"{self._raw_base_url}/api"
        # 注意：timeout / verify_ssl 必须作为 AuthenticatedClient 的独立参数传，
        # 不能塞进 httpx_args —— 生成器构造 httpx.Client 时已经显式传了 timeout=
        # 和 verify=，再在 httpx_args 里给同名键会 TypeError（multiple values）。
        self._client = AuthenticatedClient(
            base_url=api_base,
            token=api_key,
            prefix="",
            auth_header_name="x-api-key",
            timeout=httpx.Timeout(timeout),
            verify_ssl=verify_ssl,
            httpx_args=dict(httpx_args or {}),
            raise_on_unexpected_status=False,
        )

    def _run(self, call: Callable[..., Response[Any]], **kwargs: Any) -> Response[Any]:
        # 生成的 sync_detailed 函数签名是 `sync_detailed(*, client, ...)` —— client
        # 是 keyword-only。用 sync_detailed 而不是 sync，是为了拿到 Response 对象，
        # 把状态码带进 ImmichError.status_code（404 逃生口依赖它）。
        try:
            result = call(client=self._client, **kwargs)
        except httpx.HTTPError as exc:
            raise ImmichError(f"Immich 请求失败: {exc}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            # 只兜住生成模型 from_dict / 参数序列化的真实失败模式。
            # 其余异常（RuntimeError / AttributeError 等）是编程错误，
            # 必须原样冒出来，不能被贴上"解析失败"的标签掩盖掉。
            raise ImmichError(f"Immich 响应解析失败: {exc}") from exc
        if result is None:
            raise ImmichError("Immich 返回了意外状态码或空响应")
        if result.status_code >= 400:
            # 带 status_code：调用方需要区分 404 与 401/500 等，不能全压成同一条消息。
            raise ImmichError(
                f"Immich 返回了非成功状态码: {result.status_code}",
                status_code=int(result.status_code),
            )
        return result

    @staticmethod
    def _as_page(next_page: Any) -> int:
        # nextPage 是字符串，而规范里 page 是 integer，必须显式转换。
        # 转换本身也在归一化路径里 —— 非数字的 nextPage 要变成 ImmichError，
        # 不能把裸 ValueError 漏给调用方。
        try:
            return int(next_page)
        except (TypeError, ValueError) as exc:
            raise ImmichError(f"Immich 返回了非法的 nextPage: {next_page!r}") from exc

    # ------------------------------------------------------------- 只读接口

    def statistics(self) -> AssetStatsResponseDto:
        return self._run(get_asset_statistics.sync_detailed).parsed

    def list_tags(self) -> list[TagResponseDto]:
        return self._run(get_all_tags.sync_detailed).parsed or []

    def list_libraries(self) -> list[LibraryResponseDto]:
        """外部库列表。/libraries 通常要求 admin API key，无权限会抛 ImmichError。"""
        return self._run(_get_all_libraries.sync_detailed).parsed or []

    def get_asset(self, asset_id: str) -> AssetResponseDto:
        return self._run(_get_asset_info.sync_detailed, id=asset_id).parsed

    def iter_assets(
        self, dto: MetadataSearchDto, size: int = 1000
    ) -> Iterator[AssetResponseDto]:
        """按页遍历搜索结果。
        Immich 的 total 不可靠，以 nextPage 为准。
        dto 的 page / size 由本方法接管，调用方设的值会被覆盖。
        """
        page = 1
        deadline = time.monotonic() + ITER_TIME_LIMIT
        while True:
            if time.monotonic() > deadline:
                logging.warning(
                    "iter_assets 翻页整体超时（限 %ss），提前停止迭代",
                    ITER_TIME_LIMIT,
                )
                return
            dto.page = page
            dto.size = size
            result = self._run(search_assets.sync_detailed, body=dto).parsed
            assets = result.assets
            items = list(assets.items)
            yield from items
            next_page = assets.next_page
            if not items or not next_page:
                break
            page = self._as_page(next_page)

    def fetch_thumbnail_bytes(self, asset_id: str, size: str = "preview") -> bytes:
        """取缩略图的**原始字节**。端点返回二进制，不做任何 JSON 解析。
        404 时抛出的 ImmichError 带 status_code=404，供降级链路使用。
        """
        media_size = AssetMediaSize(size)
        response = self._run(_view_asset.sync_detailed, id=asset_id, size=media_size)
        return response.content

    # ------------------------------------------------------------- 写接口

    def ensure_tags(self, names: list[str]) -> dict[str, str]:
        """确保给定名字的 tag 都存在，返回 name -> tag_id 映射。

        已存在的直接复用，缺的才 upsert（一次批量请求）。
        """
        existing = {tag.name: str(tag.id) for tag in self.list_tags()}
        missing = [n for n in names if n not in existing]
        if missing:
            response = self._run(_upsert_tags.sync_detailed, body=TagUpsertDto(tags=missing))
            if response.parsed:
                for tag in response.parsed:
                    existing[tag.name] = str(tag.id)
        return {n: existing[n] for n in names}

    # tag/untag/album 批量端点（PUT/DELETE /tags/{id}/assets、PUT /albums/{id}/assets）
    # 的返回是逐项结果 [{"id":..., "success": true/false, "error": "duplicate"|"not_found"|...}]。
    # HTTP 200 不代表全部成功 —— 逐项 success=false 必须抛错，否则假成功会让
    # 队列标签永远摘不掉（真机事故：722 条 queue_remove 落日志，25 张图仍挂着标签）。
    def _check_bulk_results(self, response: Response[Any]) -> None:
        results: list[BulkIdResponseDto] | None = response.parsed
        if not results:
            return
        failures = [
            f"{r.id}: {getattr(r.error, 'value', r.error) or ''}"
            f"{'' if isinstance(r.error_message, Unset) else f' ({r.error_message})'}"
            for r in results if not r.success
        ]
        if failures:
            raise ImmichError(
                f"Immich 批量操作部分失败: {', '.join(failures[:5])}"
                + (f" ...共 {len(failures)} 项" if len(failures) > 5 else "")
            )

    def tag_assets(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        """给资产批量打标。任一列表为空即为安全 no-op（不发请求）。"""
        if not tag_ids or not asset_ids:
            return
        for tag_id in tag_ids:
            for i in range(0, len(asset_ids), TAG_ASSET_BATCH_SIZE):
                chunk = asset_ids[i : i + TAG_ASSET_BATCH_SIZE]
                self._check_bulk_results(self._run(
                    _tag_assets.sync_detailed,
                    id=tag_id,
                    body=BulkIdsDto(ids=chunk),
                ))

    def delete_tag(self, tag_id: str) -> None:
        """删除标签对象本身（DELETE /tags/{id}）。

        语义是删除标签实体——同名标签一并消失。404
        （标签已被手工删除）按预期路径返回，不抛错。
        """
        from uuid import UUID as _UUID
        try:
            self._run(_delete_tag.sync_detailed, id=_UUID(tag_id))
        except ImmichError as exc:
            if exc.status_code == 404:
                return
            raise

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        """从 tag 上批量摘除资产。空列表即安全 no-op。"""
        if not asset_ids:
            return
        for i in range(0, len(asset_ids), TAG_ASSET_BATCH_SIZE):
            chunk = asset_ids[i : i + TAG_ASSET_BATCH_SIZE]
            self._check_bulk_results(self._run(_untag_assets.sync_detailed, id=tag_id, body=BulkIdsDto(ids=chunk)))

    def set_visibility(self, asset_ids: list[str], visibility: str) -> None:
        """批量设置资产可见性（"timeline" | "archive"）。空列表即安全 no-op。"""
        if not asset_ids:
            return
        try:
            vis = AssetVisibility(visibility)
        except ValueError as exc:
            raise ImmichError(f"非法的 visibility: {visibility!r}") from exc
        for i in range(0, len(asset_ids), ASSET_UPDATE_BATCH_SIZE):
            chunk = asset_ids[i : i + ASSET_UPDATE_BATCH_SIZE]
            self._run(
                _update_assets.sync_detailed,
                body=AssetBulkUpdateDto(ids=chunk, visibility=vis),
            )

    def set_asset_properties(
        self, asset_ids: list[str], *,
        visibility: str | Unset = UNSET,
        rating: int | None | Unset = UNSET,
        description: str | Unset = UNSET,
    ) -> None:
        """批量写资产属性（PUT /assets，成功返回 204 无 body）。

        `UNSET`（默认）= 不动该字段；`rating=None` = 显式置为"未评分"。
        description 只接受 str（没有 None），清空传 `""`。
        visibility 收**字符串**并在这里转枚举：调用方（效应层）不该依赖生成的
        DTO 类型；且 AssetBulkUpdateDto.to_dict 对它调 `.value`，传裸 str 会在
        序列化期 AttributeError —— 报错点离现场很远，所以在这里就挡住。
        **不要**调 _check_bulk_results：204 没有逐项结果。
        """
        if not asset_ids:
            return
        vis: AssetVisibility | Unset = UNSET
        if not isinstance(visibility, Unset):
            try:
                vis = AssetVisibility(visibility)
            except ValueError as exc:
                raise ImmichError(f"非法的 visibility: {visibility!r}") from exc
        for i in range(0, len(asset_ids), ASSET_UPDATE_BATCH_SIZE):
            chunk = asset_ids[i : i + ASSET_UPDATE_BATCH_SIZE]
            self._run(
                _update_assets.sync_detailed,
                body=AssetBulkUpdateDto(ids=chunk, visibility=vis,
                                        rating=rating, description=description),
            )

    def ensure_album(self, name: str) -> str:
        """按名查找相册，存在则复用既有 id，不存在才创建。"""
        response = self._run(_get_all_albums.sync_detailed, name=name)
        albums: list[AlbumResponseDto] = response.parsed or []
        for album in albums:
            if album.album_name == name:
                return str(album.id)
        created = self._run(
            _create_album.sync_detailed, body=CreateAlbumDto(album_name=name)
        ).parsed
        if created is None:  # pragma: no cover - 201 无 body 属服务端违约
            raise ImmichError("创建相册后未返回相册对象")
        return str(created.id)

    def remove_from_album(self, album_id: str, asset_ids: list[str]) -> None:
        """批量把资产移出相册。空列表即安全 no-op。"""
        if not asset_ids:
            return
        for i in range(0, len(asset_ids), ALBUM_ASSET_BATCH_SIZE):
            chunk = asset_ids[i : i + ALBUM_ASSET_BATCH_SIZE]
            self._run(
                _remove_asset_from_album.sync_detailed,
                id=album_id,
                body=BulkIdsDto(ids=chunk),
            )

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        """批量把资产加入相册。空列表即安全 no-op。"""
        if not asset_ids:
            return
        for i in range(0, len(asset_ids), ALBUM_ASSET_BATCH_SIZE):
            chunk = asset_ids[i : i + ALBUM_ASSET_BATCH_SIZE]
            self._check_bulk_results(self._run(
                _add_assets_to_album.sync_detailed,
                id=album_id,
                body=BulkIdsDto(ids=chunk),
            ))
