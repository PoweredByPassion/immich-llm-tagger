"""SSE 基础设施：可复用的轮询式 async generator。

SQLite 连接不可跨线程/协程复用（见 app/db/connection.py）——
每轮快照查询都新建短命 Database（沿用 app.state.db 的 path）。

用法：
    async def stream(request):
        db: Database = request.app.state.db
        return EventSourceResponse(
            sse_stream(lambda: queries.overview(Database(db.path)), 1.0)
        )
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from fastapi.responses import StreamingResponse


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def poll_stream(fetch: Callable[[], dict], interval: float = 1.0):
    """每 interval 秒调用 fetch() 取快照，yield SSE 帧。

    fetch 经 asyncio.to_thread 跑在线程池：fetch 可能含同步网络 I/O
    （overview 缓存过期时的 Immich 调用，timeout 最长 120s），直接在
    事件循环里调会饿死所有并发协程（PERF-1）。
    GeneratorExit / CancelledError 时干净退出，不产生 error 日志。
    """
    try:
        while True:
            yield _frame(await asyncio.to_thread(fetch))
            await asyncio.sleep(interval)
    except (GeneratorExit, asyncio.CancelledError):
        raise


def sse_response(fetch: Callable[[], dict], interval: float = 1.0) -> StreamingResponse:
    """把 poll_stream 包成 text/event-stream 响应。"""
    return StreamingResponse(
        poll_stream(fetch, interval),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
