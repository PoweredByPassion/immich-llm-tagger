"""配置页：Webhook 接入（URL / Header / token / 轮换 + Immich Workflow 直达）。

从 `config_poll.py` 拆出：轮询/效应器升为顶级「识别设置」tab 后，Webhook 接入
留在「连接配置」组。

- 可复制的 URL / Header name / Header value：token 渲染必须走
  SettingsStore.webhook_token()（DB 无行时首次生成并持久化），
  不能读 get_all()["webhook_token"]（兜底 None 会渲染成假空）。
- token 轮换带二次确认，旧 token 立即失效。
- `immich_workflow_url`：Immich 的 webhook workflow 在 `{server}/workflow`，
  给个新标签直达按钮，省去用户自己拼地址。未配 Immich 时为 None（模板不渲染死链）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.store.connections import ConnectionStore
from app.store.settings import SettingsStore
from app.web.i18n import lang_from_cookie, t

router = APIRouter(prefix="/config")

HEADER_NAME = "x-webhook-token"


def _store(request: Request) -> SettingsStore:
    return SettingsStore(request.app.state.db)


def _webhook_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/hooks/immich"


def _immich_workflow_url(request: Request) -> str | None:
    url = ConnectionStore(request.app.state.db).get_immich().url.strip()
    if not url:
        return None
    return url.rstrip("/") + "/workflow"


def _render(request: Request, settings: SettingsStore, **extra):
    ctx = {
        "webhook_url": _webhook_url(request),
        "header_name": HEADER_NAME,
        "token": settings.webhook_token(),
        "immich_workflow_url": _immich_workflow_url(request),
        **extra,
    }
    return request.app.state.templates.TemplateResponse(
        request, "config/webhook.html", ctx
    )


@router.get("/webhook", response_class=HTMLResponse)
def webhook_page(request: Request):
    return _render(request, _store(request))


@router.post("/webhook/rotate", response_class=HTMLResponse)
def webhook_rotate(request: Request):
    settings = _store(request)
    settings.rotate_webhook_token()
    return _render(
        request,
        settings,
        rotated=True,
        rotate_message=t("config.poll.rotated", lang_from_cookie(request.cookies)),
    )
