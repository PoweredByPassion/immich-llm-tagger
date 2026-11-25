"""Polling streams for JSON clients and htmx HTML fragments."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

from fastapi.responses import StreamingResponse

_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def poll_stream(fetch: Callable[[], dict], interval: float = 1.0):
    """Run synchronous snapshot reads outside the event loop."""
    while True:
        yield _frame(await asyncio.to_thread(fetch))
        await asyncio.sleep(interval)


def sse_response(fetch: Callable[[], dict], interval: float = 1.0) -> StreamingResponse:
    return StreamingResponse(
        poll_stream(fetch, interval), media_type="text/event-stream", headers=_HEADERS,
    )


@dataclass(frozen=True)
class HtmlEvent:
    name: str
    content: str
    final: bool = False

    def frame(self) -> str:
        lines = [f"event: {self.name}"]
        content = self.content.replace("\r\n", "\n").replace("\r", "\n")
        lines.extend(f"data: {line}" for line in content.split("\n"))
        return "\n".join(lines) + "\n\n"


def html_sse_response[T](
    fetch: Callable[[], T], render: Callable[[T], HtmlEvent],
    interval: float = 1.0,
) -> StreamingResponse:
    async def stream():
        previous = object()
        loop = asyncio.get_running_loop()
        last_sent = loop.time()
        while True:
            payload = await asyncio.to_thread(fetch)
            if payload != previous:
                event = await asyncio.to_thread(render, payload)
                yield event.frame()
                if event.final:
                    return
                previous = payload
                last_sent = loop.time()
            elif loop.time() - last_sent >= 15:
                yield ": keepalive\n\n"
                last_sent = loop.time()
            await asyncio.sleep(interval)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_HEADERS)
