"""Overview page and live snapshots for dashboard clients."""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.clients.immich import ImmichClient
from app.db.connection import Database
from app.store import jobs, queries
from app.store.connections import ConnectionStore, ImmichConn
from app.store.settings import SettingsStore
from app.web.sse import HtmlEvent, html_sse_response, sse_response

router = APIRouter()
logger = logging.getLogger(__name__)

_RECENT_JOBS = 10
IMMICH_OVERVIEW_TTL = 60.0
_immich_overview_lock = Lock()
_immich_overview_cache: tuple[tuple[Path, ImmichConn], float, dict | None] | None = None
_snapshot_lock = Lock()


def _conn_configured(url: str, api_key: str) -> bool:
    return bool(url.strip()) and bool(api_key.strip())


def _refresh_immich_overview(immich: ImmichConn) -> dict | None:
    try:
        client = ImmichClient(
            base_url=immich.url, api_key=immich.api_key,
            verify_ssl=immich.verify_ssl, timeout=5.0,
        )
        try:
            stats = client.statistics()
            libraries = client.list_libraries()
            tags = client.list_tags()
        finally:
            client.close()
    except Exception as exc:
        logger.warning("Immich 概览拉取失败: %s", exc)
        return None
    return {
        "images": stats.images, "videos": stats.videos,
        "libraries": len(libraries), "tags": len(tags),
    }


def _immich_overview(db: Database, immich: ImmichConn) -> dict | None:
    global _immich_overview_cache
    if not _conn_configured(immich.url, immich.api_key):
        return None
    key = (db.path, immich)
    cached = _immich_overview_cache
    same_connection = cached is not None and cached[0] == key
    if same_connection and time.monotonic() - cached[1] < IMMICH_OVERVIEW_TTL:
        return cached[2]
    if not _immich_overview_lock.acquire(blocking=False):
        return cached[2] if same_connection else None
    try:
        cached = _immich_overview_cache
        if cached and cached[0] == key and time.monotonic() - cached[1] < IMMICH_OVERVIEW_TTL:
            return cached[2]
        payload = _refresh_immich_overview(immich)
        _immich_overview_cache = (key, time.monotonic(), payload)
        return payload
    finally:
        _immich_overview_lock.release()


def _local_snapshot(db: Database, tz_name: str | None) -> tuple[dict, ImmichConn]:
    data = queries.overview(db, tz_name=tz_name)
    settings = SettingsStore(db).get_all()
    conns = ConnectionStore(db)
    immich = conns.get_immich()
    llm = conns.get_llm()
    data["connections"] = {
        "immich": _conn_configured(immich.url, immich.api_key),
        "llm": _conn_configured(llm.base_url, llm.api_key),
    }
    data["scheduler"] = {
        "poll_enabled": bool(settings["poll_enabled"]),
        "poll_interval_seconds": int(settings["poll_interval_seconds"]),
    }
    data["tz_name"] = tz_name or "UTC"
    data["recent_jobs"] = [vars(j) for j in jobs.list_jobs(db, limit=_RECENT_JOBS)]
    return data, immich


@lru_cache(maxsize=64)
def _shared_snapshot(
    db: Database, tz_name: str | None, second: int,
) -> tuple[dict, ImmichConn]:
    """At most one local poll per database and time zone in each second."""
    return _local_snapshot(db, tz_name)


def _overview_payload(
    db: Database, tz_name: str | None = None, *, shared: bool = False,
) -> dict:
    if shared:
        with _snapshot_lock:
            data, immich = _shared_snapshot(db, tz_name, int(time.monotonic()))
    else:
        data, immich = _local_snapshot(db, tz_name)
    return {**data, "immich_stats": _immich_overview(db, immich)}


def _tz_from_cookie(request: Request) -> str | None:
    raw = request.cookies.get("client_tz")
    if not raw:
        return None
    try:
        return ZoneInfo(unquote(raw)).key
    except (ValueError, ZoneInfoNotFoundError):
        return None


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def overview_page(request: Request):
    db: Database = request.app.state.db
    payload = _overview_payload(db, tz_name=_tz_from_cookie(request))
    return request.app.state.templates.TemplateResponse(request, "overview.html", payload)


@router.get("/api/sse/overview", include_in_schema=False)
def overview_sse(request: Request, view: str | None = None):
    db: Database = request.app.state.db
    tz_name = _tz_from_cookie(request)

    if view == "html":
        template = request.app.state.templates.env.get_template("_overview_content.html")

        def render(payload: dict) -> HtmlEvent:
            return HtmlEvent("overview", template.render(request=request, **payload))

        return html_sse_response(
            lambda: _overview_payload(db, tz_name=tz_name, shared=True), render,
        )
    return sse_response(lambda: _overview_payload(db, tz_name=tz_name))
