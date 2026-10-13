"""`app.clients.immich` 适配层的契约测试。

底层客户端是 OpenAPI 生成的、不可手改；本文件只针对我们自己的三件事下断言：
URL 前缀、鉴权头、错误归一，外加翻页行为。

夹具用**真实形状**的 payload：生成模型是 strict 的（缺字段抛 KeyError，
id 必须是 UUID，时间戳必须 ISO-8601）。
"""
from __future__ import annotations

import json
import ssl
import time
from typing import Any

import httpx
import pytest

from app.clients import immich as immich_module
from app.clients._immich_gen.models.metadata_search_dto import MetadataSearchDto
from app.clients.immich import ImmichClient, ImmichError

BASE_URL = "http://immich:2283"
API_KEY = "secret-key"


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


def _library_payload(idx: str = "1", name: str = "外置盘") -> dict[str, Any]:
    return {
        "assetCount": 12,
        "createdAt": _iso(),
        "exclusionPatterns": ["**/.DS_Store"],
        "id": f"44444444-4444-4444-4444-00000000000{idx}",
        "importPaths": ["/mnt/photos"],
        "name": name,
        "ownerId": "55555555-5555-5555-5555-555555555555",
        "refreshedAt": _iso(2),
        "updatedAt": _iso(3),
    }


def _asset_payload(idx: str = "1") -> dict[str, Any]:
    return {
        "checksum": "cafebabe",
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


def _search_payload(
    items: list[dict[str, Any]], next_page: str | None
) -> dict[str, Any]:
    """SearchResponseDto：albums 与 assets 都是必填，assets 六个字段全必填。"""
    return {
        "albums": {"count": 0, "facets": [], "items": [], "total": 0},
        "assets": {
            "count": len(items),
            "facets": [],
            "items": items,
            "nextCursor": None,
            "nextPage": next_page,
            "total": len(items),
        },
    }


def _client(handler: Any, **kwargs: Any) -> ImmichClient:
    """离线客户端：httpx_args 里的 transport 会透传给 httpx.Client。"""
    return ImmichClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        httpx_args={"transport": httpx.MockTransport(handler)},
        **kwargs,
    )


# --------------------------------------------------------------- URL 前缀


def test_requests_are_prefixed_with_api() -> None:
    """规范里的路径是 /tags，服务端挂在 /api 下 —— 漏了 /api 会全部 404。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[_tag_payload()])

    _client(handler).list_tags()
    assert seen["path"] == "/api/tags"


def test_trailing_slash_input_does_not_double_slash() -> None:
    """输入末尾带斜杠时不能再拼出 //api/...（httpx 不会把 // 规范化掉）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[_tag_payload()])

    client = ImmichClient(
        base_url=f"{BASE_URL}/",
        api_key=API_KEY,
        httpx_args={"transport": httpx.MockTransport(handler)},
    )
    client.list_tags()
    assert client._raw_base_url == BASE_URL
    assert seen["url"] == f"{BASE_URL}/api/tags"


# --------------------------------------------------------------- 鉴权头


def test_sends_x_api_key_and_no_authorization() -> None:
    """Immich 用 x-api-key；不能发出 Authorization: Bearer。"""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=[_tag_payload()])

    _client(handler).list_tags()
    assert seen["key"] == API_KEY
    assert seen["authorization"] is None


# --------------------------------------------------------------- 解析


def test_statistics_returns_parsed_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/assets/statistics"
        return httpx.Response(200, json={"images": 3, "videos": 1, "total": 4})

    stats = _client(handler).statistics()
    assert (stats.images, stats.videos, stats.total) == (3, 1, 4)


def test_list_tags_returns_parsed_tags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[_tag_payload("1", "未分类"), _tag_payload("2", "风景")]
        )

    tags = _client(handler).list_tags()
    assert [t.name for t in tags] == ["未分类", "风景"]
    assert str(tags[0].id) == "11111111-1111-1111-1111-000000000001"


def test_list_libraries_hits_libraries_endpoint_and_parses() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[_library_payload("1", "外置盘")])

    libs = _client(handler).list_libraries()
    assert seen["path"] == "/api/libraries"
    assert [lib.name for lib in libs] == ["外置盘"]
    assert libs[0].import_paths == ["/mnt/photos"]
    assert str(libs[0].id) == "44444444-4444-4444-4444-000000000001"


def test_list_libraries_empty_body_is_empty_list() -> None:
    """空库列表返回 []（不是 None）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    assert _client(handler).list_libraries() == []


def test_list_libraries_non_2xx_raises_immich_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(ImmichError) as exc_info:
        _client(handler).list_libraries()
    assert exc_info.value.status_code == 403


# --------------------------------------------------------------- 翻页


def test_iter_assets_follows_next_page_and_stops() -> None:
    pages = {
        1: _search_payload([_asset_payload("1"), _asset_payload("2")], "2"),
        2: _search_payload([_asset_payload("3")], None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=pages[body["page"]])

    ids = [str(a.id)[-1] for a in _client(handler).iter_assets(MetadataSearchDto(), size=2)]
    assert ids == ["1", "2", "3"]


def test_iter_assets_stops_when_page_empty_but_has_next_page() -> None:
    """items 为空但 nextPage 有值时必须停 —— 否则死循环。

    handler 在第 4 次调用时抛错，把"没停下来"的问题转成失败而不是挂起。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] > 3:
            raise AssertionError("iter_assets 没有停下，仍在继续翻页")
        return httpx.Response(200, json=_search_payload([], "2"))

    assert list(_client(handler).iter_assets(MetadataSearchDto())) == []
    assert calls["n"] == 1  # 只请求一次


def test_iter_assets_sends_integer_page() -> None:
    """page 必须以**整数**发出（nextPage 是字符串，不能直接透传）。"""
    seen: list[object] = []
    pages = {
        1: _search_payload([_asset_payload("1")], "2"),
        2: _search_payload([_asset_payload("2")], None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["page"])
        return httpx.Response(200, json=pages[int(body["page"])])

    list(_client(handler).iter_assets(MetadataSearchDto(), size=1))
    assert seen == [1, 2]
    assert all(isinstance(p, int) for p in seen)


def _capture_search_body(**iter_kwargs: Any) -> list[dict[str, Any]]:
    """跑一次单页的 iter_assets，返回每次请求的 JSON body。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_search_payload([], None))

    list(_client(handler).iter_assets(MetadataSearchDto(), **iter_kwargs))
    return bodies


def test_iter_assets_sends_size_in_request_body() -> None:
    """size 必须进请求体：把 `dto.size = size` 改成 `dto.size = page` 时这里会红。"""
    bodies = _capture_search_body(size=7)
    assert [b["size"] for b in bodies] == [7]


def test_iter_assets_default_size_is_the_literal_1000() -> None:
    """默认 size 是字面量 1000（不是常量，避免改值变异存活）。"""
    bodies = _capture_search_body()
    assert [b["size"] for b in bodies] == [1000]


def test_iter_assets_mutates_caller_dto_in_place() -> None:
    """文档承诺 page / size 由 iter_assets 接管 —— 即**就地改写**调用方的 dto。

    这条钉住"就地改写"这个已文档化的行为：若改成拷贝（不动 caller 的 dto），
    这里会红，届时必须同步改 docstring 并重新确认语义。
    """
    dto = MetadataSearchDto()
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_payload([], None))

    list(_client(handler).iter_assets(dto, size=5))
    assert dto.page == 1
    assert dto.size == 5


# --------------------------------------------------------------- 错误归一


@pytest.mark.parametrize("status", [500, 400, 401])
def test_error_status_raises_immich_error(status: int) -> None:
    """500 / 正好 400 / 401 都归一成 ImmichError。

    `__cause__ is None` 钉住 raise_on_unexpected_status=False：改成 True 后生成器会
    抛 errors.UnexpectedStatus —— 它不在 _run 捕获的解析类异常里，会直接冒出，
    于是这条 pytest.raises(ImmichError) 就会红。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).statistics()
    assert excinfo.value.__cause__ is None


def test_connection_error_raises_immich_error_with_cause() -> None:
    """网络错误转成 ImmichError，且保留 __cause__ 便于排障。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).statistics()
    assert isinstance(excinfo.value.__cause__, httpx.HTTPError)


def test_malformed_body_raises_immich_error_not_keyerror() -> None:
    """200 但body 解析不了时，必须是 ImmichError，不能把 KeyError 漏给调用方。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # 缺 images/videos/total

    with pytest.raises(ImmichError) as excinfo:
        _client(handler).statistics()
    assert isinstance(excinfo.value.__cause__, KeyError)


class _ProgrammingError(Exception):
    """刻意继承 Exception（不是 RuntimeError）——ImmichError 是 RuntimeError 的子类，
    拿 RuntimeError 做探针根本区分不出"有没有被吞"，只有不相干的异常类型才说明问题。"""


def test_non_parser_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """非解析类异常必须原样冒出，不能被 _run 贴成 "解析失败"。

    否则真正的编程错误会被伪装成 ImmichError，排障时被误导。
    这正是 `except Exception` 太宽时踩过的坑。
    """
    def boom(*, client: object, **kwargs: object) -> object:
        raise _ProgrammingError("programming error, not a parse failure")

    monkeypatch.setattr(immich_module.get_asset_statistics, "sync_detailed", boom)

    with pytest.raises(_ProgrammingError) as excinfo:
        _client(
            lambda r: httpx.Response(200, json={"images": 1, "videos": 0, "total": 1})
        ).statistics()
    assert not isinstance(excinfo.value, ImmichError)


def test_non_numeric_next_page_is_normalized() -> None:
    """nextPage 非数字（如 "abc"）必须归一成 ImmichError，不能漏出裸 ValueError。

    转换发生在 _run 之外，是错误归一最容易漏掉的一条路径。
    第二页起返回空 items，避免"转换被绕过"的变异把测试拖成死循环（应干净地失败）。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_search_payload([_asset_payload("1")], "abc"))
        return httpx.Response(200, json=_search_payload([], None))

    with pytest.raises(ImmichError) as excinfo:
        list(_client(handler).iter_assets(MetadataSearchDto()))
    assert isinstance(excinfo.value.__cause__, ValueError)


# --------------------------------------------------------------- 超时 / TLS


def test_default_timeout_is_the_literal_120() -> None:
    """断言**字面量** 120.0：拿 DEFAULT_TIMEOUT 常量做断言时，
    `DEFAULT_TIMEOUT = 120.0 → 1.0` 这种改值变异会存活（两边一起变）。"""
    client = ImmichClient(base_url=BASE_URL, api_key=API_KEY)
    assert client._client.get_httpx_client().timeout.read == 120.0


def test_custom_timeout_is_applied_with_configured_value() -> None:
    """断言具体值：httpx 的默认 timeout 也非 None，"is not None" 分不出有没有生效。"""
    client = ImmichClient(base_url=BASE_URL, api_key=API_KEY, timeout=30.0)
    assert client._client.get_httpx_client().timeout.read == 30.0


def _verify_mode(client: ImmichClient) -> int:
    """httpx.Client 把 verify 烘进 transport 的 SSLContext，这是唯一能观测到它的地方。"""
    pool = client._client.get_httpx_client()._transport._pool  # type: ignore[attr-defined]
    return pool._ssl_context.verify_mode


def test_verify_ssl_flag_reaches_httpx() -> None:
    """verify_ssl 必须真的透传到 httpx：False → 不校验，True → 校验。"""
    disabled = ImmichClient(base_url=BASE_URL, api_key=API_KEY, verify_ssl=False)
    enabled = ImmichClient(base_url=BASE_URL, api_key=API_KEY, verify_ssl=True)
    assert _verify_mode(disabled) == ssl.CERT_NONE
    assert _verify_mode(enabled) == ssl.CERT_REQUIRED


# ----------------------------------------------------- M5 Task 3: remove_from_album


def test_remove_from_album_uses_delete_endpoint_and_batches() -> None:
    """DELETE /albums/{id}/assets，按 ALBUM_ASSET_BATCH_SIZE 分批。"""
    from app.clients import immich as client_module

    seen: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path.startswith("/api/albums/album-1/assets")
        body = json.loads(request.content)
        seen.append((request.url.path, body["ids"]))
        return httpx.Response(200, json=[{"success": True, "id": i} for i in body["ids"]])

    ids = [f"22222222-2222-2222-2222-{i:012d}" for i in range(501)]
    _client(handler).remove_from_album("album-1", ids)
    assert [len(chunk) for _, chunk in seen] == [
        client_module.ALBUM_ASSET_BATCH_SIZE, 1,
    ]
    assert [i for _, c in seen for i in c] == ids


def test_remove_from_album_empty_is_noop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("empty list must not issue a request")

    _client(handler).remove_from_album("album-1", [])


# ------------------------------------------------- M6 Task 3: delete_tag wrapper

def test_delete_tag_calls_delete_endpoint() -> None:
    """delete_tag 走 DELETE /api/tags/{id}；204 → 无异常。"""
    import uuid as _uuid

    tid = _uuid.uuid4()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    _client(handler).delete_tag(str(tid))
    assert seen["method"] == "DELETE"
    assert seen["path"] == f"/api/tags/{tid}"


def test_delete_tag_404_is_swallowed() -> None:
    """404（标签已被手工删除）按预期路径吞掉，不抛 ImmichError。"""
    import uuid as _uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _client(handler).delete_tag(str(_uuid.uuid4()))  # 不抛即通过


def test_delete_tag_500_raises() -> None:
    """非 404 错误（如 500）必须抛 ImmichError，不能吞。"""
    import uuid as _uuid

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(ImmichError) as e:
        _client(handler).delete_tag(str(_uuid.uuid4()))
    assert e.value.status_code == 500


def test_iter_assets_overall_timeout_returns_collected(monkeypatch, caplog) -> None:
    """翻页整体超 ITER_TIME_LIMIT 后记 warning 并停止，返回已收集部分，不抛异常。"""
    clock = [0.0]
    monkeypatch.setattr(immich_module.time, "monotonic", lambda: clock[0])
    pages = {
        1: _search_payload([_asset_payload("1")], "2"),
        2: _search_payload([_asset_payload("2")], None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        clock[0] += 1000.0  # 第二页检查时已"耗尽"时限
        return httpx.Response(200, json=pages[body["page"]])

    with caplog.at_level("WARNING", logger="app.clients.immich"):
        ids = [
            str(a.id)[-1]
            for a in _client(handler).iter_assets(MetadataSearchDto(), size=1)
        ]
    assert ids == ["1"]
    assert any("超时" in r.message for r in caplog.records)
