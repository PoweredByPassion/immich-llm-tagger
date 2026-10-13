"""`app.clients.immich` 写操作与缩略图的契约测试。

与 tests/test_client_immich.py 同一套夹具思路：httpx.MockTransport 注入真实
生成客户端，断言打到**真实 HTTP 路径/method**、分块数量与并集、字节级缩略图、
以及 status_code 逃生口（M2 Task 4 依赖）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.clients import immich as immich_module
from app.clients._immich_gen.models.metadata_search_dto import MetadataSearchDto
from app.clients.immich import ImmichClient, ImmichError

BASE_URL = "http://immich:2283"
API_KEY = "secret-key"

TAG_ID = "11111111-1111-1111-1111-000000000001"
ALBUM_ID = "44444444-4444-4444-4444-444444444444"
ASSET_ID = "22222222-2222-2222-2222-000000000001"


# --------------------------------------------------------------- fixtures


def _iso(day: int = 1) -> str:
    return f"2026-10-0{day}T00:00:00.000Z"


def _tag_payload(idx: str = "1", name: str = "未分类") -> dict[str, Any]:
    return {
        "id": f"11111111-1111-1111-1111-00000000000{idx}",
        "name": name,
        "value": name,
        "createdAt": _iso(),
        "updatedAt": _iso(2),
    }


def _album_payload(idx: str = "1", name: str = "截图相册") -> dict[str, Any]:
    return {
        "albumName": name,
        "albumThumbnailAssetId": None,
        "albumUsers": [],
        "assetCount": 0,
        "createdAt": _iso(),
        "description": "",
        "hasSharedLink": False,
        "id": f"44444444-4444-4444-4444-00000000000{idx}",
        "isActivityEnabled": False,
        "shared": False,
        "updatedAt": _iso(2),
    }


def _asset_payload(idx: str = "1") -> dict[str, Any]:
    return {
        "checksum": f"cafebabe{idx}",
        "createdAt": _iso(),
        "duration": None,
        "fileCreatedAt": _iso(1),
        "fileModifiedAt": _iso(2),
        "hasMetadata": True,
        "height": 100,
        "id": f"22222222-2222-2222-2222-00000000000{idx}",
        "isArchived": False,
        "isEdited": False,
        "isFavorite": False,
        "isOffline": False,
        "isTrashed": False,
        "localDateTime": _iso(3),
        "originalFileName": f"IMG_000{idx}.png",
        "originalPath": f"/upload/IMG_000{idx}.png",
        "ownerId": "33333333-3333-3333-3333-333333333333",
        "thumbhash": None,
        "type": "IMAGE",
        "updatedAt": _iso(4),
        "visibility": "timeline",
        "width": 200,
    }


def _client(handler: Any, **kwargs: Any) -> ImmichClient:
    return ImmichClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        httpx_args={"transport": httpx.MockTransport(handler)},
        **kwargs,
    )


def _unreachable(msg: str) -> Any:
    """路由值：命中即失败。不能直接写 pytest.fail(...) —— 那会在构造期就炸。"""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"{msg}（打到了 {request.method} {request.url.path}）")

    return handler


def _json_bytes(obj: Any) -> bytes:
    """与生成客户端 to_dict → httpx 序列化一致的紧凑 JSON。"""
    return json.dumps(obj, separators=(",", ":")).encode()


class Recorder:
    """记录 (method, path, body) 并按路由表返回响应。"""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append((request.method, request.url.path, request.content))
        responder = self.routes[key]
        if callable(responder):
            responder = responder(request)
        if isinstance(responder, httpx.Response):
            return responder
        status = responder[0] if isinstance(responder, tuple) else 200
        payload = responder[1] if isinstance(responder, tuple) else responder
        return httpx.Response(status, json=payload)


# --------------------------------------------------------------- get_asset


def test_get_asset_hits_show_endpoint() -> None:
    rec = Recorder({("GET", f"/api/assets/{ASSET_ID}"): _asset_payload()})
    asset = _client(rec).get_asset(ASSET_ID)
    assert str(asset.id) == ASSET_ID
    assert asset.original_file_name == "IMG_0001.png"
    assert rec.calls == [("GET", f"/api/assets/{ASSET_ID}", b"")]


def test_get_asset_404_has_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).get_asset(ASSET_ID)
    assert excinfo.value.status_code == 404


# --------------------------------------------------------------- thumbnail


def test_fetch_thumbnail_returns_raw_bytes() -> None:
    payload = bytes(range(256)) * 4  # 二进制、非 UTF-8 安全
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["size"] = request.url.params.get("size")
        return httpx.Response(200, content=payload, headers={"Content-Type": "image/jpeg"})

    data = _client(handler).fetch_thumbnail_bytes(ASSET_ID)
    assert data == payload  # 逐字节相同，不是解析对象
    assert isinstance(data, bytes)
    assert seen["path"] == f"/api/assets/{ASSET_ID}/thumbnail"
    assert seen["size"] == "preview"


def test_fetch_thumbnail_default_and_explicit_size() -> None:
    sizes: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(request.url.params.get("size"))
        return httpx.Response(200, content=b"\x89PNG")

    _client(handler).fetch_thumbnail_bytes(ASSET_ID)
    _client(handler).fetch_thumbnail_bytes(ASSET_ID, size="thumbnail")
    assert sizes == ["preview", "thumbnail"]


def test_fetch_thumbnail_404_carries_status_code() -> None:
    """M2 Task 4 的前提：404 必须能被调用方识别。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no thumbnail"})

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).fetch_thumbnail_bytes(ASSET_ID)
    assert excinfo.value.status_code == 404


# --------------------------------------------------------------- ensure_tags


def test_ensure_tags_upserts_missing_and_maps_all() -> None:
    """已有 tag1，缺 tag2 → 只 upsert tag2，但映射里两个都要有。"""
    rec = Recorder(
        {
            ("GET", "/api/tags"): [_tag_payload("1", "游戏")],
            ("PUT", "/api/tags"): [_tag_payload("1", "游戏"), _tag_payload("2", "风景")],
        }
    )
    mapping = _client(rec).ensure_tags(["游戏", "风景"])
    assert mapping == {
        "游戏": "11111111-1111-1111-1111-000000000001",
        "风景": "11111111-1111-1111-1111-000000000002",
    }
    put = [c for c in rec.calls if c[0] == "PUT"]
    assert len(put) == 1
    assert json.loads(put[0][2]) == {"tags": ["风景"]}


def test_ensure_tags_all_existing_sends_no_put() -> None:
    rec = Recorder(
        {
            ("GET", "/api/tags"): [_tag_payload("1", "游戏")],
            ("PUT", "/api/tags"): _unreachable("不应创建已存在的 tag"),
        }
    )
    mapping = _client(rec).ensure_tags(["游戏"])
    assert mapping == {"游戏": "11111111-1111-1111-1111-000000000001"}
    assert [c[0] for c in rec.calls] == ["GET"]


# --------------------------------------------------------------- tag/untag


def test_tag_assets_single_chunk() -> None:
    rec = Recorder({("PUT", f"/api/tags/{TAG_ID}/assets"): []})
    _client(rec).tag_assets([TAG_ID], [ASSET_ID])
    assert rec.calls == [
        ("PUT", f"/api/tags/{TAG_ID}/assets", _json_bytes({"ids": [ASSET_ID]}))
    ]


def test_tag_assets_chunks_assets_by_1000() -> None:
    """1200 个资产 → 2 个 chunk；2 个 tag → 2×2 = 4 个请求，并集完整。"""
    assets = [f"22222222-2222-2222-2222-{i:012d}" for i in range(1200)]
    tag2 = "11111111-1111-1111-1111-000000000002"
    rec = Recorder({("PUT", f"/api/tags/{t}/assets"): [] for t in (TAG_ID, tag2)})

    _client(rec).tag_assets([TAG_ID, tag2], assets)

    puts = [c for c in rec.calls if c[0] == "PUT"]
    assert len(puts) == 4  # 2 tags × 2 chunks
    per_tag: dict[str, list[list[str]]] = {}
    for method, path, body in puts:
        tag = path.rsplit("/", 1)[0].rsplit("/", 1)[1]
        per_tag.setdefault(tag, []).append(json.loads(body)["ids"])
    for tag, chunks in per_tag.items():
        assert [len(c) for c in chunks] == [1000, 200]
        # 两个 tag 的 chunk 顺序也应一致（tag 外层循环、asset 内层）
        assert sorted(x for c in chunks for x in c) == sorted(assets)
    assert set(per_tag) == {TAG_ID, tag2}


def test_tag_assets_empty_inputs_are_noop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"空列表不应发请求，却打到了 {request.url.path}")

    _client(handler).tag_assets([], [ASSET_ID])
    _client(handler).tag_assets([TAG_ID], [])
    _client(handler).tag_assets([], [])


def test_untag_assets_single_chunk() -> None:
    rec = Recorder({("DELETE", f"/api/tags/{TAG_ID}/assets"): []})
    _client(rec).untag_assets(TAG_ID, [ASSET_ID])
    assert rec.calls == [
        ("DELETE", f"/api/tags/{TAG_ID}/assets", _json_bytes({"ids": [ASSET_ID]}))
    ]


def test_untag_assets_chunks_by_1000() -> None:
    assets = [f"22222222-2222-2222-2222-{i:012d}" for i in range(2001)]
    rec = Recorder({("DELETE", f"/api/tags/{TAG_ID}/assets"): []})
    _client(rec).untag_assets(TAG_ID, assets)
    deletes = [c for c in rec.calls if c[0] == "DELETE"]
    assert len(deletes) == 3
    all_ids = [x for _, _, body in deletes for x in json.loads(body)["ids"]]
    assert sorted(all_ids) == sorted(assets)
    assert [len(json.loads(b)["ids"]) for _, _, b in deletes] == [1000, 1000, 1]


def test_untag_assets_empty_assets_is_noop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("空 asset 列表不应发请求")

    _client(handler).untag_assets(TAG_ID, [])


# --------------------------------------------------------------- visibility


def test_set_visibility_archive_single_chunk() -> None:
    rec = Recorder({("PUT", "/api/assets"): None})
    _client(rec).set_visibility([ASSET_ID], "archive")
    method, path, body = rec.calls[0]
    assert (method, path) == ("PUT", "/api/assets")
    assert json.loads(body) == {"ids": [ASSET_ID], "visibility": "archive"}


def test_set_visibility_chunks_by_500() -> None:
    assets = [f"22222222-2222-2222-2222-{i:012d}" for i in range(1001)]
    rec = Recorder({("PUT", "/api/assets"): None})
    _client(rec).set_visibility(assets, "timeline")
    puts = [c for c in rec.calls if c[0] == "PUT"]
    assert len(puts) == 3
    all_ids = [x for _, _, body in puts for x in json.loads(body)["ids"]]
    assert sorted(all_ids) == sorted(assets)
    assert [len(json.loads(b)["ids"]) for _, _, b in puts] == [500, 500, 1]


def test_set_visibility_empty_is_noop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("空 asset 列表不应发请求")

    _client(handler).set_visibility([], "archive")


def test_set_visibility_empty_skips_validation() -> None:
    """空列表是安全 no-op，连 visibility 校验都不该跑。

    删掉空列表 guard 后，空输入 + 非法值会抛 ImmichError —— 这条钉住 guard。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("空 asset 列表不应发请求")

    _client(handler).set_visibility([], "bogus")


def test_set_visibility_invalid_value_raises() -> None:
    with pytest.raises(ImmichError):
        _client(lambda r: httpx.Response(200)).set_visibility([ASSET_ID], "bogus")


# --------------------------------------------------------------- albums


def test_ensure_album_creates_when_missing() -> None:
    rec = Recorder(
        {
            ("GET", "/api/albums"): lambda req: [],  # 无同名相册
            ("POST", "/api/albums"): (201, _album_payload("9", "新相册")),
        }
    )
    album_id = _client(rec).ensure_album("新相册")
    assert album_id == "44444444-4444-4444-4444-000000000009"
    post = [c for c in rec.calls if c[0] == "POST"]
    assert len(post) == 1
    assert json.loads(post[0][2]) == {"albumName": "新相册"}
    get = [c for c in rec.calls if c[0] == "GET"]
    assert get[0][1] == "/api/albums"
    assert get[0][2] == b""  # name 走 query 参数


def test_ensure_album_reuses_existing_by_name() -> None:
    """已存在同名相册 → 不创建，返回既有 id。"""
    rec = Recorder(
        {
            ("GET", "/api/albums"): [_album_payload("3", "截图相册")],
            ("POST", "/api/albums"): _unreachable("已存在时不应再创建"),
        }
    )
    album_id = _client(rec).ensure_album("截图相册")
    assert album_id == "44444444-4444-4444-4444-000000000003"
    assert [c[0] for c in rec.calls] == ["GET"]


def test_ensure_album_filters_by_name_query() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/albums":
            seen["name"] = request.url.params.get("name")
            return httpx.Response(200, json=[])
        return httpx.Response(201, json=_album_payload("5", "查询相册"))

    _client(handler).ensure_album("查询相册")
    assert seen["name"] == "查询相册"


def test_add_to_album_single_chunk() -> None:
    rec = Recorder({("PUT", f"/api/albums/{ALBUM_ID}/assets"): []})
    _client(rec).add_to_album(ALBUM_ID, [ASSET_ID])
    assert rec.calls == [
        ("PUT", f"/api/albums/{ALBUM_ID}/assets", _json_bytes({"ids": [ASSET_ID]}))
    ]


def test_add_to_album_chunks_by_500() -> None:
    assets = [f"22222222-2222-2222-2222-{i:012d}" for i in range(501)]
    rec = Recorder({("PUT", f"/api/albums/{ALBUM_ID}/assets"): []})
    _client(rec).add_to_album(ALBUM_ID, assets)
    puts = [c for c in rec.calls if c[0] == "PUT"]
    assert len(puts) == 2
    all_ids = [x for _, _, body in puts for x in json.loads(body)["ids"]]
    assert sorted(all_ids) == sorted(assets)
    assert [len(json.loads(b)["ids"]) for _, _, b in puts] == [500, 1]


def test_add_to_album_empty_is_noop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("空 asset 列表不应发请求")

    _client(handler).add_to_album(ALBUM_ID, [])


# --------------------------------------------------------------- 错误归一 + status_code


@pytest.mark.parametrize("status", [404, 401, 500])
def test_write_error_carries_status_code(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).fetch_thumbnail_bytes(ASSET_ID)
    assert excinfo.value.status_code == status


def test_network_error_keeps_cause_and_no_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).tag_assets([TAG_ID], [ASSET_ID])
    assert isinstance(excinfo.value.__cause__, httpx.HTTPError)
    assert excinfo.value.status_code is None


def test_immich_error_default_status_code_is_none() -> None:
    assert ImmichError("boom").status_code is None


def test_status_code_is_int_not_httpstatus() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).get_asset(ASSET_ID)
    assert excinfo.value.status_code == 404
    assert isinstance(excinfo.value.status_code, int)


def test_write_parse_failure_keeps_cause() -> None:
    """200 但 body 缺字段 → ImmichError，__cause__ 保留。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # AssetResponseDto 缺必填字段

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).get_asset(ASSET_ID)
    assert isinstance(excinfo.value.__cause__, (KeyError, ValueError, TypeError))


# --------------------------------------------------------------- 旧接口回归钉子


def test_existing_endpoints_still_work_after_run_switch() -> None:
    """_run 改用 sync_detailed 后，M1 三个接口行为不变。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/assets/statistics":
            return httpx.Response(200, json={"images": 3, "videos": 1, "total": 4})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=[_tag_payload()])
        raise AssertionError(request.url.path)

    client = _client(handler)
    assert client.statistics().total == 4
    assert [t.name for t in client.list_tags()] == ["未分类"]


def test_iter_assets_still_works_after_run_switch() -> None:
    pages = {1: (["1"], "2"), 2: (["2"], None)}

    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        items, next_page = pages[page]
        payload_items = [_asset_payload(i) for i in items]
        return httpx.Response(
            200,
            json={
                "albums": {"count": 0, "facets": [], "items": [], "total": 0},
                "assets": {
                    "count": len(items),
                    "facets": [],
                    "items": payload_items,
                    "nextCursor": None,
                    "nextPage": next_page,
                    "total": len(items),
                },
            },
        )

    ids = [str(a.id)[-1] for a in _client(handler).iter_assets(MetadataSearchDto())]
    assert ids == ["1", "2"]


def test_batch_constants_match_reference_script() -> None:
    """分块常量必须与参考脚本同量级，且是模块级常量（不散落魔数）。"""
    assert immich_module.TAG_ASSET_BATCH_SIZE == 1000
    assert immich_module.ASSET_UPDATE_BATCH_SIZE == 500
    assert immich_module.ALBUM_ASSET_BATCH_SIZE == 500


# ------------------------------------------------- bulk 逐项 success 检查


def _bulk_result(asset_id: str, success: bool, error: str = "not_found") -> dict:
    return {"id": asset_id, "success": success,
            **({} if success else {"error": error, "errorMessage": "nope"})}


def test_untag_assets_item_failure_raises() -> None:
    """HTTP 200 但逐项 success=false 必须抛 ImmichError（真机事故回归：
    25 张图 queue_remove 假成功，标签永远摘不掉）。"""
    rec = Recorder({("DELETE", f"/api/tags/{TAG_ID}/assets"):
                    [_bulk_result(ASSET_ID, False)]})
    with pytest.raises(ImmichError, match="not_found"):
        _client(rec).untag_assets(TAG_ID, [ASSET_ID])


def test_tag_assets_item_failure_raises() -> None:
    rec = Recorder({("PUT", f"/api/tags/{TAG_ID}/assets"):
                    [_bulk_result(ASSET_ID, False, error="duplicate")]})
    with pytest.raises(ImmichError, match="部分失败"):
        _client(rec).tag_assets([TAG_ID], [ASSET_ID])


def test_add_to_album_item_failure_raises() -> None:
    rec = Recorder({("PUT", f"/api/albums/{ALBUM_ID}/assets"):
                    [_bulk_result(ASSET_ID, False)]})
    with pytest.raises(ImmichError, match="not_found"):
        _client(rec).add_to_album(ALBUM_ID, [ASSET_ID])


def test_untag_assets_all_success_no_raise() -> None:
    rec = Recorder({("DELETE", f"/api/tags/{TAG_ID}/assets"):
                    [_bulk_result(ASSET_ID, True)]})
    _client(rec).untag_assets(TAG_ID, [ASSET_ID])  # 不抛即通过
