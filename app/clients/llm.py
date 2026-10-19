"""OpenAI 兼容的 `/v1/chat/completions` 客户端。

职责只有一个：把 messages 发出去，把 `choices[0].message.content` 原样取回来。
**不做拒答识别** —— 上游安全层拒答时返回的是正常 200（content 是一段说明性
文字，而不是 prompt 要求的 JSON），本模块照常返回该 content；是否把这次调用
判为 `refused` 属于上层分类器的判断，不要在这里拦截。

重试策略：
- 429 → 重试，优先采用 `retry-after` 头，缺省时指数退避；
- 5xx → 指数退避重试；
- 其他 4xx（401/400/404…）→ **立即失败，不重试**（认证/参数错误重试只会白烧配额）；
- 网络/传输错误 → 重试。
超过 retries 次尝试仍失败 → `LLMError`，并保留原始异常为 `__cause__`。

所有出站请求都带**显式 timeout**（项目硬性要求）：构造自有客户端时把 timeout
烘进 `httpx.Client`；调用方注入 `http=` 时按原样使用（timeout 由注入方负责）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"


class LLMError(RuntimeError):
    """LLM 调用失败（网络错误、非 2xx、或响应无法解析）。

    status_code：上游返回的 HTTP 状态码；网络/解析失败时为 None。调用方靠它
    区分"这张图/这次请求本身有问题"（4xx：参数被拒、图片不被接受，重试无用）
    与"上游暂时不行"（5xx/429/网络，重试有意义）。
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str | None
    usage: dict[str, Any] | None


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 180,
        max_tokens: int = 400,
        http: httpx.Client | None = None,
    ) -> None:
        # 末尾斜杠必须去掉，否则相对路径拼接会出 //v1/...
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = float(timeout)
        self._max_tokens = max_tokens
        # 显式 timeout：httpx 默认 5s，对 vision 大模型远远不够。
        self._http = http or httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(2**attempt)

    @classmethod
    def _retry_delay(cls, resp: httpx.Response, attempt: int) -> float:
        """429 优先用 retry-after 头；缺失或不可解析时退回指数退避。"""
        header = resp.headers.get("retry-after")
        if header is not None:
            try:
                return float(header)
            except ValueError:
                pass
        return cls._backoff(attempt)

    def chat(self, messages: list[dict[str, Any]], retries: int = 4) -> ChatResult:
        """POST /v1/chat/completions；失败时按重试策略处理，耗尽后抛 LLMError。"""
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": messages,
        }
        last_exc: BaseException | None = None
        for attempt in range(retries):
            try:
                resp = self._http.post(CHAT_PATH, headers=self._headers(), json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 < retries:
                    time.sleep(self._backoff(attempt))
                continue

            # 429：重试，尊重 retry-after。
            if resp.status_code == 429:
                last_exc = LLMError(f"LLM 返回 429: {resp.text[:200]}",
                                    status_code=429)
                if attempt + 1 < retries:
                    time.sleep(self._retry_delay(resp, attempt))
                continue

            # 5xx：指数退避重试。
            if resp.status_code >= 500:
                last_exc = LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:200]}",
                                    status_code=resp.status_code)
                if attempt + 1 < retries:
                    time.sleep(self._backoff(attempt))
                continue

            # 其他 4xx：认证/参数错误，重试无意义，立即失败。
            if resp.status_code >= 400:
                raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:200]}",
                               status_code=resp.status_code)

            return self._parse_chat(resp)

        raise LLMError(f"LLM 调用在 {retries} 次尝试后仍失败: {last_exc}") from last_exc

    @staticmethod
    def _parse_chat(resp: httpx.Response) -> ChatResult:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMError(f"LLM 响应不是合法 JSON: {exc}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("LLM 响应中没有 choices")
        try:
            content = (choices[0].get("message") or {}).get("content") or ""
        except (AttributeError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构非法: {exc}") from exc
        return ChatResult(
            content=content,
            model=payload.get("model"),
            usage=payload.get("usage"),
        )

    def list_models(self) -> list[str]:
        """GET /v1/models，返回 data 里各条目的 id。"""
        try:
            resp = self._http.get(MODELS_PATH, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LLMError(f"获取 LLM 模型列表失败: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(
                f"获取 LLM 模型列表返回 {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json().get("data") or []
        except (ValueError, AttributeError) as exc:
            raise LLMError(f"LLM 模型列表响应非法: {exc}") from exc
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m]
