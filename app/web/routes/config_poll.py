"""配置页：Webhook / 轮询。

- 开关即全局暂停入口：关闭 poll_enabled = 停止轮询触发（webhook 仍可入队）。
- 可复制的 URL / Header name / Header value：token 渲染必须走
  SettingsStore.webhook_token()（DB 无行时首次生成并持久化），
  不能读 get_all()["webhook_token"]（兜底 None 会渲染成假空）。
- token 轮换带二次确认，旧 token 立即失效。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.store.settings import SettingsStore
from app.web.i18n import lang_from_cookie, t

router = APIRouter(prefix="/config")

HEADER_NAME = "x-webhook-token"
MIN_INTERVAL_SECONDS = 10


def _store(request: Request) -> SettingsStore:
    return SettingsStore(request.app.state.db)


def _webhook_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/hooks/immich"


def _render(request: Request, settings: SettingsStore, **extra):
    ctx = {
        "webhook_url": _webhook_url(request),
        "header_name": HEADER_NAME,
        "token": settings.webhook_token(),
        "poll_enabled": settings.get("poll_enabled"),
        "poll_interval_seconds": settings.get("poll_interval_seconds"),
        "queue_tag_name": settings.get("queue_tag_name"),
        **extra,
    }
    return request.app.state.templates.TemplateResponse(
        request, "config/poll.html", ctx
    )


@router.get("/poll", response_class=HTMLResponse)
def poll_form(request: Request):
    return _render(request, _store(request))


@router.post("/poll", response_class=HTMLResponse)
async def poll_save(request: Request):
    lang = lang_from_cookie(request.cookies)
    settings = _store(request)
    form = await request.form()
    raw = str(form.get("poll_interval_seconds", "")).strip()
    try:
        interval = int(raw)
    except ValueError:
        return _render(
            request, settings,
            error=t("config.poll.interval_invalid", lang) % MIN_INTERVAL_SECONDS,
        )
    if interval < MIN_INTERVAL_SECONDS:
        return _render(
            request, settings,
            error=t("config.poll.interval_invalid", lang) % MIN_INTERVAL_SECONDS,
        )
    queue_tag_name = str(form.get("queue_tag_name", "")).strip()
    if not queue_tag_name:
        return _render(request, settings, error=t("config.poll.queue_tag_required", lang))
    settings.set("poll_enabled", form.get("poll_enabled") is not None)
    settings.set("poll_interval_seconds", interval)
    settings.set("queue_tag_name", queue_tag_name)
    return _render(request, settings, saved=True)


@router.post("/poll/rotate", response_class=HTMLResponse)
def poll_rotate(request: Request):
    settings = _store(request)
    settings.rotate_webhook_token()
    return _render(
        request,
        settings,
        rotated=True,
        rotate_message=t("config.poll.rotated", lang_from_cookie(request.cookies)),
    )
