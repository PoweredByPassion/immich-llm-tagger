"""概览页（GET /）+ SSE 端点（GET /api/sse/overview）。

SSE 每轮查询自建短命 Database（连接不可跨协程复用，见 app/web/sse.py）。
immich 概览走模块级 60s 缓存：_overview_payload 被 SSE 每秒重算，网络调用
绝不能落在每次 payload 计算里。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.clients.immich import ImmichClient
from app.db.connection import Database
from app.store import jobs, queries
from app.store.connections import ConnectionStore
from app.store.settings import SettingsStore
from app.web.sse import sse_response

router = APIRouter()

logger = logging.getLogger(__name__)

_RECENT_JOBS = 10

# immich 概览缓存 TTL（秒）。
IMMICH_OVERVIEW_TTL = 60.0

# (刷新时刻, 载荷)。载荷为 None 表示未配置或拉取失败。
# 失败同样缓存满 TTL：SSE 每秒重算 payload，若失败不缓存会每秒重试网络调用。
_immich_overview_cache: tuple[float, dict | None] = (float("-inf"), None)


def _conn_configured(url: str, api_key: str) -> bool:
    return bool(url.strip()) and bool(api_key.strip())


def _refresh_immich_overview(db: Database) -> dict | None:
    """真正打 immich 取一次概览数据；未配置或任何失败都返回 None。"""
    immich = ConnectionStore(db).get_immich()
    if not _conn_configured(immich.url, immich.api_key):
        return None
    try:
        client = ImmichClient(
            base_url=immich.url,
            api_key=immich.api_key,
            verify_ssl=immich.verify_ssl,
        )
        stats = client.statistics()
        libraries = client.list_libraries()
        tags = client.list_tags()
    except Exception as exc:  # noqa: BLE001 - 任何失败都降级为"未连接"
        # 含 ImmichError（网络/非 2xx/解析）与库端点缺 admin 权限的情况。
        logger.warning("Immich 概览拉取失败，降级为未连接: %s", exc)
        return None
    return {
        "images": stats.images,
        "videos": stats.videos,
        "libraries": len(libraries),
        "tags": len(tags),
    }


def _immich_overview(db: Database) -> dict | None:
    """读缓存；过期或未初始化才同步刷新一次。"""
    global _immich_overview_cache
    ts, payload = _immich_overview_cache
    if time.monotonic() - ts >= IMMICH_OVERVIEW_TTL:
        payload = _refresh_immich_overview(db)
        _immich_overview_cache = (time.monotonic(), payload)
    return payload


def _overview_payload(db: Database, tz_name: str | None = None) -> dict:
    data = queries.overview(db, tz_name=tz_name)
    settings = SettingsStore(db)
    immich = ConnectionStore(db).get_immich()
    llm = ConnectionStore(db).get_llm()
    data["connections"] = {
        "immich": _conn_configured(immich.url, immich.api_key),
        "llm": _conn_configured(llm.base_url, llm.api_key),
    }
    data["immich_stats"] = _immich_overview(db)
    # 状态栏：真实调度模式与间隔（poll 启用 → 轮询模式，否则 webhook-only）
    poll_enabled = bool(settings.get("poll_enabled"))
    data["scheduler"] = {
        "poll_enabled": poll_enabled,
        "poll_interval_seconds": int(settings.get("poll_interval_seconds")),
    }
    data["tz_name"] = tz_name or "UTC"
    data["recent_jobs"] = [
        vars(j) for j in jobs.list_jobs(db, limit=_RECENT_JOBS)
    ]
    return data


def _tz_from_cookie(request: Request) -> str | None:
    """base.html 写入的 client_tz cookie 经 encodeURIComponent 编码
    （Asia%2FShanghai）；此处显式解码。Starlette 的 Cookie 参数不做解码。"""
    raw = request.cookies.get("client_tz")
    return unquote(raw) if raw else None


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def overview_page(request: Request):
    db: Database = request.app.state.db
    payload = _overview_payload(db, tz_name=_tz_from_cookie(request))
    return request.app.state.templates.TemplateResponse(
        request, "overview.html", {**payload, "connections": payload["connections"]}
    )


@router.get("/api/sse/overview", include_in_schema=False)
def overview_sse(request: Request):
    db: Database = request.app.state.db
    tz_name = _tz_from_cookie(request)
    # 每轮查询开新连接：lambda 里重新构造 Database（同 path）
    return sse_response(
        lambda: _overview_payload(Database(db.path), tz_name=tz_name),
        interval=1.0,
    )
