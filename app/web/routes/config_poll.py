"""配置页：识别设置（定时轮询 + 效应器 + 应用重试上限）。

Webhook 接入已拆到 `config_webhook.py`（仍属「连接配置」组）；本页只管识别行为。

- 开关即全局暂停入口：关闭 poll_enabled = 停止轮询触发（webhook 仍可入队）。
- §8.1 效应器开关 / 跳过标记标签名 / 应用重试上限并入本页表单。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.store.settings import SettingsStore
from app.web.i18n import lang_from_cookie, t

router = APIRouter(prefix="/config")

MIN_INTERVAL_SECONDS = 10
# 与 SettingsStore.DEFAULTS["enabled_effects"] 的取值/顺序一致（无导出的常量源）。
EFFECT_OPTIONS = ("tag", "visibility", "album", "rating", "description")


def _store(request: Request) -> SettingsStore:
    return SettingsStore(request.app.state.db)


def _render(request: Request, settings: SettingsStore, **extra):
    ctx = {
        "poll_enabled": settings.get("poll_enabled"),
        "poll_interval_seconds": settings.get("poll_interval_seconds"),
        "queue_tag_name": settings.get("queue_tag_name"),
        "enabled_effects": settings.get("enabled_effects"),
        "skip_marker_tag_name": settings.get("skip_marker_tag_name") or "",
        "apply_max_attempts": settings.get("apply_max_attempts"),
        "effect_options": EFFECT_OPTIONS,
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
    # --- 效应器 / 跳过标记 / 重试上限（§8.1）---
    # 复选组缺键（全部取消勾选）就是 [] —— 必须整组覆盖，不能"缺键即保持"。
    enabled_effects = list(form.getlist("enabled_effects"))
    skip_marker_tag_name = str(form.get("skip_marker_tag_name", "")).strip()
    raw_attempts = str(form.get("apply_max_attempts", "")).strip()
    attempts: int | None = None
    if raw_attempts:
        try:
            attempts = int(raw_attempts)
        except ValueError:
            attempts = None
        if attempts is None or attempts < 1:
            return _render(
                request, settings,
                error=t("config.effects.max_attempts_invalid", lang),
            )
    settings.set("poll_enabled", form.get("poll_enabled") is not None)
    settings.set("poll_interval_seconds", interval)
    settings.set("queue_tag_name", queue_tag_name)
    settings.set("enabled_effects", enabled_effects)
    settings.set("skip_marker_tag_name", skip_marker_tag_name or None)
    if attempts is not None:
        settings.set("apply_max_attempts", attempts)
    return _render(request, settings, saved=True)
