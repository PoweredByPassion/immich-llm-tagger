"""`app.clients.llm` 的契约测试。

本模块手写 httpx（无 SDK），所以这里要钉住的是**我们自己的**策略：
请求线格式、200 解析、重试谓词（429+5xx 重试 / 其他 4xx 快速失败 /
网络异常重试）、退避与 retry-after、显式 timeout、以及可注入 http。

规则（吸取前几个任务的教训）：
- 断言**整值**（整个 body、整个 ChatResult、整个 timeout），不用单字段；
- 断言 token 不能在不相关的场景里也出现（否则恒真）；
- 每个能写字段的路径都要有断言；每条设计选择都问一句"会不会为错误的理由通过"。

夹具里注入 httpx.MockTransport，**绝不触网**；autouse 的 no_sleep 把
time.sleep 换成记录器，整个套件不会真的等待。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.clients import llm as llm_module
from app.clients.llm import ChatResult, LLMClient, LLMError

BASE_URL = "http://llm:8000"
API_KEY = "secret-key"
MODEL = "vision-model"


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把 time.sleep 换成记录器并返回记录列表。

    autouse：任何测试都不可能真的 sleep（否则 5xx 退避会把套件拖慢甚至挂住）。
    返回列表，需要断言退避/retry-after 的测试按名字请求它。
    """
    slept: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def _ok(payload: dict[str, Any]) -> Any:
    """返回一个只会回 200 的 handler 函数（交给 _client 包成 MockTransport）。"""
    return lambda request: httpx.Response(200, json=payload)


def _client(
    handler: Any,
    *,
    base_url: str = BASE_URL,
    model: str = MODEL,
    **overrides: Any,
) -> LLMClient:
    """离线客户端：注入带 MockTransport 的 httpx.Client。"""
    http = overrides.pop("http", None)
    if http is None:
        http = httpx.Client(
            transport=httpx.MockTransport(handler), base_url=base_url
        )
    return LLMClient(
        base_url=base_url,
        api_key=API_KEY,
        model=model,
        http=http,
        **overrides,
    )


def _completion(
    content: str,
    *,
    model: str | None = MODEL,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
    if model is not None:
        payload["model"] = model
    if usage is not None:
        payload["usage"] = usage
    return payload


# --------------------------------------------------------------- 请求线格式


def test_request_line_and_headers() -> None:
    """POST 到 /v1/chat/completions，带 Bearer 鉴权与 JSON Content-Type。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json=_completion("ok"))

    _client(handler).chat([{"role": "user", "content": "hi"}])

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["authorization"] == f"Bearer {API_KEY}"
    assert seen["content_type"] == "application/json"


def test_request_body_is_the_full_openai_payload() -> None:
    """断言**整个** body：model / max_tokens / messages 任一丢失都会红。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion("ok"))

    messages = [
        {"role": "system", "content": "you are a tagger"},
        {"role": "user", "content": "classify this image"},
    ]
    _client(handler, max_tokens=512).chat(messages)

    assert captured == {
        "model": MODEL,
        "max_tokens": 512,
        "messages": messages,
    }


def test_default_max_tokens_is_the_literal_400() -> None:
    """默认 max_tokens 是字面量 400 —— 若只断言 captured["max_tokens"] 存在，
    把默认值改成 1 也能过。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion("ok"))

    _client(handler).chat([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 400


# --------------------------------------------------------------- 200 解析


def test_200_maps_content_model_and_usage() -> None:
    """整值断言 ChatResult：三个字段同一次比对，漏一个也会红。"""
    result = _client(
        _ok(
            _completion(
                '{"tag":"风景"}',
                model="my-vision-model",
                usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            )
        )
    ).chat([{"role": "user", "content": "hi"}])

    assert result == ChatResult(
        content='{"tag":"风景"}',
        model="my-vision-model",
        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    )


def test_200_without_usage_key_yields_none() -> None:
    """usage 键缺失 → usage is None（不能是 {}，也不能崩）。"""
    result = _client(_ok(_completion("{}"))).chat([{"role": "user", "content": "hi"}])
    assert result == ChatResult(content="{}", model=MODEL, usage=None)


def test_200_with_empty_content_is_returned_as_empty_string() -> None:
    """content 允许为空字符串（契约明说 may be empty）。"""
    result = _client(_ok(_completion(""))).chat([{"role": "user", "content": "hi"}])
    assert result.content == ""
    assert result == ChatResult(content="", model=MODEL, usage=None)


def test_content_comes_from_first_choice() -> None:
    """多条 choices 时取第一条 —— 用 choices[-1] 的变异靠这条才会红。"""
    payload = {
        "model": MODEL,
        "choices": [
            {"message": {"content": "first"}},
            {"message": {"content": "second"}},
        ],
    }
    result = _client(_ok(payload)).chat([{"role": "user", "content": "hi"}])
    assert result.content == "first"


def test_empty_choices_raises_and_is_not_retried() -> None:
    """无 choices 是畸形 200，不是可重试的瞬时故障 —— 立即失败，不烧配额。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(LLMError):
        _client(handler).chat([{"role": "user", "content": "hi"}], retries=4)
    assert calls["n"] == 1


def test_malformed_json_200_raises_llm_error() -> None:
    """200 但不是 JSON → LLMError（保留 __cause__），不能漏出裸 ValueError。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>not json</html>")
    )
    client = LLMClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=MODEL,
        http=httpx.Client(transport=transport, base_url=BASE_URL),
    )
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert isinstance(excinfo.value.__cause__, ValueError)


# --------------------------------------------------------------- 429 重试


def test_429_then_200_succeeds_and_retried_once() -> None:
    """429 后重试成功：既要有正确 content，也要证明**真的重试了**（2 次调用）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_completion("recovered"))

    result = _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert result.content == "recovered"
    assert calls["n"] == 2


def test_429_honours_retry_after_header(no_sleep: list[float]) -> None:
    """有 retry-after 时按头里的秒数等待（7.0），而不是退避的 1.0。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, json=_completion("recovered"))

    _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert no_sleep == [7.0]


def test_429_without_retry_after_falls_back_to_backoff(no_sleep: list[float]) -> None:
    """无 retry-after 头 → 退回指数退避（首次 1.0，而不是 0）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=_completion("recovered"))

    _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert no_sleep == [1.0]


def test_429_exhausting_retries_raises(no_sleep: list[float]) -> None:
    """一直 429：尝试 retries 次后抛 LLMError，消息里带 429。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"})

    with pytest.raises(LLMError) as excinfo:
        _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert calls["n"] == 3
    assert "429" in str(excinfo.value)
    # 最后一次失败后不应再白睡一次（off-by-one 变异会被这里杀掉）
    assert no_sleep == [0.0, 0.0]


# --------------------------------------------------------------- 5xx 重试


def test_5xx_is_retried_with_exponential_backoff(no_sleep: list[float]) -> None:
    """两次 503 后成功：退避必须是 1 秒、2 秒（2**attempt）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_completion("recovered"))

    result = _client(handler).chat([{"role": "user", "content": "hi"}], retries=4)

    assert result.content == "recovered"
    assert calls["n"] == 3
    assert no_sleep == [1.0, 2.0]


def test_5xx_exhausting_retries_raises(no_sleep: list[float]) -> None:
    """一直 503：耗尽 retries 后抛 LLMError，消息里带 503。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(LLMError) as excinfo:
        _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert calls["n"] == 3
    assert "503" in str(excinfo.value)
    # 退避只在尝试 0、1 之后发生；第 3 次失败后直接抛错，不再白睡
    assert no_sleep == [1.0, 2.0]


def test_500_is_retried_too() -> None:
    """5xx 是区间判定（>=500），不能只认 503。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=_completion("recovered"))

    result = _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)
    assert result.content == "recovered"
    assert calls["n"] == 2


# --------------------------------------------------------------- 4xx 快速失败


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_fails_fast_with_exactly_one_attempt(
    status: int, no_sleep: list[float]
) -> None:
    """400/401/403/404/422 → 只发**一次**请求即抛 LLMError，绝不重试。

    这是对本项目 LLM 配额最重要的一条：认证/参数错误重试只会白烧额度。
    断言调用次数 == 1，并且没有任何 sleep。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text=f"boom {status}")

    with pytest.raises(LLMError) as excinfo:
        _client(handler).chat([{"role": "user", "content": "hi"}], retries=4)

    assert calls["n"] == 1
    assert no_sleep == []
    assert str(status) in str(excinfo.value)
    # 不能把 4xx 误报成重试耗尽的措辞
    assert "尝试后仍失败" not in str(excinfo.value)


def test_404_error_message_does_not_leak_429_wording() -> None:
    """反向 token 检查：404 的场景里不能出现 429 字样（防谓词写反）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such route")

    with pytest.raises(LLMError) as excinfo:
        _client(handler).chat([{"role": "user", "content": "hi"}], retries=2)
    assert "429" not in str(excinfo.value)


# --------------------------------------------------------------- 网络异常


def test_network_error_is_retried_then_succeeds(no_sleep: list[float]) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=_completion("recovered"))

    result = _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert result.content == "recovered"
    assert calls["n"] == 2
    assert no_sleep == [1.0]


def test_network_error_exhausting_retries_raises_with_cause() -> None:
    """网络错误耗尽重试 → LLMError，且保留原始 httpx 异常为 __cause__。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as excinfo:
        _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)

    assert calls["n"] == 3
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


def test_read_timeout_is_retried_then_succeeds() -> None:
    """读取超时（httpx.ReadTimeout）也是网络类错误，应重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("too slow")
        return httpx.Response(200, json=_completion("recovered"))

    result = _client(handler).chat([{"role": "user", "content": "hi"}], retries=3)
    assert result.content == "recovered"
    assert calls["n"] == 2


# --------------------------------------------------------------- list_models


def test_list_models_parses_ids() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "vision-model", "object": "model"},
                    {"id": "text-model", "object": "model"},
                ],
            },
        )

    models = _client(handler).list_models()

    assert models == ["vision-model", "text-model"]
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/models"
    assert seen["authorization"] == f"Bearer {API_KEY}"


def test_list_models_empty_data_returns_empty_list() -> None:
    models = _client(_ok({"object": "list", "data": []})).list_models()
    assert models == []


def test_list_models_4xx_raises_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    with pytest.raises(LLMError) as excinfo:
        _client(handler).list_models()
    assert "401" in str(excinfo.value)


def test_list_models_network_error_raises_with_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as excinfo:
        _client(handler).list_models()
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


# --------------------------------------------------------------- timeout / 注入


def test_configured_timeout_literal_reaches_the_client() -> None:
    """断言**具体配置值** 42：httpx 默认 timeout 也非 None，
    "is not None" 是恒真的；断言整个 Timeout(42.0) 更强。"""
    client = LLMClient(base_url=BASE_URL, api_key=API_KEY, model=MODEL, timeout=42)
    assert client._http.timeout == httpx.Timeout(42.0)


def test_default_timeout_is_the_literal_180() -> None:
    """契约默认 timeout=180；用常量自比会让"改默认值"的变异存活。"""
    client = LLMClient(base_url=BASE_URL, api_key=API_KEY, model=MODEL)
    assert client._http.timeout == httpx.Timeout(180.0)


def test_injected_http_client_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """给了 http= 就必须原样用，**不能再构造第二个客户端**。

    把 httpx.Client 换成会炸的哨兵：若实现里还构造客户端，这里直接失败。
    """
    seen = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        return httpx.Response(200, json=_completion("from-injected"))

    injected = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不应在注入 http 时再构造 httpx.Client")

    monkeypatch.setattr(llm_module.httpx, "Client", boom)

    client = LLMClient(
        base_url=BASE_URL, api_key=API_KEY, model=MODEL, http=injected
    )
    assert client._http is injected
    assert client.chat([{"role": "user", "content": "hi"}]).content == "from-injected"
    assert seen["called"] is True


def test_trailing_slash_base_url_is_normalized() -> None:
    """末尾斜杠要被去掉，客户端 base_url 不能出现 //。"""
    client = LLMClient(base_url=f"{BASE_URL}/", api_key=API_KEY, model=MODEL)
    assert client._base_url == BASE_URL
    assert str(client._http.base_url) == BASE_URL


def test_request_goes_to_the_configured_base_url() -> None:
    """注入客户端时，请求必须打到它自己的 base_url（相对路径拼接正确）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_completion("ok"))

    _client(handler).chat([{"role": "user", "content": "hi"}])
    assert seen["url"] == f"{BASE_URL}/v1/chat/completions"
