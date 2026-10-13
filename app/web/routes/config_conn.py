"""配置页：Immich / LLM 连接设置与「测试连接」。

api_key 是 write-only：渲染只回显掩码，POST 空串或掩码本身时保留旧值。
测试连接端点绝不让异常冒泡成 500 —— 一切失败都渲染成失败结果。
"""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clients.immich import ImmichClient, ImmichError
from app.clients.llm import LLMClient, LLMError
from app.store.connections import ConnectionStore, ImmichConn, LLMConn
from app.web.i18n import lang_from_cookie, t

router = APIRouter(prefix="/config")


@router.get("", response_class=RedirectResponse)
@router.get("/", response_class=RedirectResponse)
def config_root():
    return RedirectResponse(url="/config/immich", status_code=302)


# 表单里 api_key 字段的掩码：有已存 key 时回显它；POST 回掩码或空串 = 保留旧值。
API_KEY_MASK = "********"

# 「测试识别」用的内置 demo 图与固定问句：测的是模型识别能力，不接活跃分类模板。
# 问句走 i18n（config.conn.recognition_prompt），随请求语言渲染。
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_RECOGNITION_IMAGE = _STATIC_DIR / "img" / "immich-logo.png"
_RECOGNITION_REPLY_LIMIT = 200

# 拉模型列表的固定超时：绝不继承连接配置的 timeout（默认 180s），否则
# 错误/不可达端点在「静默降级」路径上会默默挂起 3 分钟。
_MODELS_TIMEOUT = 5


def _store(request: Request) -> ConnectionStore:
    return ConnectionStore(request.app.state.db)


def _templates(request: Request):
    return request.app.state.templates


def _resolve_api_key(posted: str, existing: str) -> str:
    """空串或掩码 → 保留旧 key；否则采用新 key。"""
    if posted == "" or posted == API_KEY_MASK:
        return existing
    return posted


# ----------------------------------------------------------------- immich

@router.get("/immich", response_class=HTMLResponse)
def immich_form(request: Request):
    conn = _store(request).get_immich()
    return _templates(request).TemplateResponse(
        request,
        "config/immich.html",
        {
            "conn": conn,
            "api_key_mask": API_KEY_MASK if conn.api_key else "",
        },
    )


@router.post("/immich", response_class=HTMLResponse)
async def immich_save(request: Request):
    store = _store(request)
    existing = store.get_immich()
    form = await request.form()
    new_key = _resolve_api_key(str(form.get("api_key", "")), existing.api_key)
    conn = ImmichConn(
        url=str(form.get("url", "")).strip(),
        api_key=new_key,
        verify_ssl=form.get("verify_ssl") is not None,
    )
    store.set_immich(conn)
    return _templates(request).TemplateResponse(
        request,
        "config/immich.html",
        {
            "conn": store.get_immich(),
            "api_key_mask": API_KEY_MASK if new_key else "",
            "saved": True,
        },
    )


@router.post("/immich/test", response_class=HTMLResponse)
def immich_test(request: Request):
    lang = lang_from_cookie(request.cookies)
    store = _store(request)
    conn = store.get_immich()
    if not conn.url:
        return _render_test_result(request, ok=False, message=t("config.conn.immich_not_configured", lang))
    try:
        client = ImmichClient(
            base_url=conn.url,
            api_key=conn.api_key,
            verify_ssl=conn.verify_ssl,
        )
        stats = client.statistics()
        tag_count = len(client.list_tags())
        images = getattr(stats, "images", "?")
        videos = getattr(stats, "videos", "?")
        message = t("config.conn.immich_ok", lang) % (images, videos, tag_count)
    except ImmichError as exc:
        return _render_test_result(request, ok=False, message=t("config.conn.fail", lang) % exc)
    return _render_test_result(request, ok=True, message=message)


# -------------------------------------------------------------------- llm

@router.get("/llm", response_class=HTMLResponse)
def llm_form(request: Request):
    conn = _store(request).get_llm()
    from app.store.settings import SettingsStore
    return _templates(request).TemplateResponse(
        request,
        "config/llm.html",
        {
            "conn": conn,
            "api_key_mask": API_KEY_MASK if conn.api_key else "",
            "settings": SettingsStore(request.app.state.db).get_all(),
        },
    )


@router.post("/llm", response_class=HTMLResponse)
async def llm_save(request: Request):
    lang = lang_from_cookie(request.cookies)
    store = _store(request)
    existing = store.get_llm()
    form = await request.form()
    new_key = _resolve_api_key(str(form.get("api_key", "")), existing.api_key)

    def _int(name: str, default: int) -> int:
        raw = str(form.get(name, "")).strip()
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(t("config.conn.int_field", lang) % name)

    try:
        conn = LLMConn(
            base_url=str(form.get("base_url", "")).strip(),
            api_key=new_key,
            model=str(form.get("model", "")).strip(),
            timeout=_int("timeout", existing.timeout),
            max_tokens=_int("max_tokens", existing.max_tokens),
            multimodal=form.get("multimodal") is not None,
        )
        # 缩略图参数（settings 表，pipeline 消费）。表单缺省（如旧测试
        # POST 不带这些字段）时沿用当前值。
        from app.classify.thumb import PRESETS
        from app.store.settings import SettingsStore
        settings_store = SettingsStore(request.app.state.db)
        current = settings_store.get_all()
        thumb_preset = str(form.get("thumb_preset", "")).strip() \
            or current["thumb_preset"]
        if thumb_preset not in PRESETS:
            raise ValueError(t("config.conn.thumb_size_invalid", lang) % "/".join(PRESETS))
        thumb_quality = _int("thumb_quality", current["thumb_quality"])
        if not 1 <= thumb_quality <= 95:
            raise ValueError(t("config.conn.thumb_quality_invalid", lang))
        store.set_llm(conn)
        settings_store.set("thumb_preset", thumb_preset)
        settings_store.set("thumb_quality", thumb_quality)
    except ValueError as exc:
        # 校验失败：不持久化，回显表单 + 错误横幅。
        return _templates(request).TemplateResponse(
            request,
            "config/llm.html",
            {
                "conn": existing,
                "api_key_mask": API_KEY_MASK if existing.api_key else "",
                "error": str(exc),
                "settings": SettingsStore(request.app.state.db).get_all(),
            },
        )
    return _templates(request).TemplateResponse(
        request,
        "config/llm.html",
        {
            "conn": store.get_llm(),
            "api_key_mask": API_KEY_MASK if new_key else "",
            "saved": True,
            "settings": settings_store.get_all(),
        },
    )


@router.post("/llm/test", response_class=HTMLResponse)
def llm_test(request: Request):
    lang = lang_from_cookie(request.cookies)
    conn = _store(request).get_llm()
    if not conn.base_url:
        return _render_test_result(request, ok=False, message=t("config.conn.llm_not_configured", lang))
    try:
        client = LLMClient(
            base_url=conn.base_url,
            api_key=conn.api_key,
            model=conn.model,
            timeout=conn.timeout,
            max_tokens=conn.max_tokens,
        )
        models = client.list_models()
        message = t("config.conn.llm_ok", lang) % (
            ", ".join(models) if models else t("config.conn.empty_models", lang)
        )
    except LLMError as exc:
        return _render_test_result(request, ok=False, message=t("config.conn.fail", lang) % exc)
    return _render_test_result(request, ok=True, message=message)


@router.post("/api/llm/models")
async def llm_models(request: Request) -> dict:
    """拉模型列表（POST：新鲜 api_key 走请求体，不进 URL/访问日志/Referer）。
    表单值优先（未保存也能试）；api_key 掩码走 _resolve_api_key 回读库真值。
    失败静默（200 + 空列表 + error）。"""
    lang = lang_from_cookie(request.cookies)
    store = _store(request)
    existing = store.get_llm()
    form = await request.form()
    base_url = str(form.get("base_url", "")).strip() or existing.base_url
    resolved = _resolve_api_key(str(form.get("api_key", "")), existing.api_key)
    if not base_url:
        return {"models": [], "error": t("config.conn.llm_not_configured", lang)}
    try:
        client = LLMClient(base_url=base_url, api_key=resolved,
                           model=existing.model, timeout=_MODELS_TIMEOUT)
        return {"models": client.list_models(), "error": None}
    except Exception as exc:  # noqa: BLE001 —— 静默降级，用户手动填写
        return {"models": [], "error": str(exc)}


@router.post("/llm/test-recognition", response_class=HTMLResponse)
def llm_test_recognition(request: Request):
    """用内置 demo 图测一次视觉识别。任何失败都渲染成失败结果，绝不冒泡 500。"""
    lang = lang_from_cookie(request.cookies)
    conn = _store(request).get_llm()
    if not conn.base_url:
        return _render_test_result(request, ok=False, message=t("config.conn.llm_not_configured", lang))
    try:
        client = LLMClient(
            base_url=conn.base_url,
            api_key=conn.api_key,
            model=conn.model,
            timeout=conn.timeout,
            max_tokens=conn.max_tokens,
        )
        recognition_prompt = t("config.conn.recognition_prompt", lang)
        note = ""
        if conn.multimodal:
            image_b64 = base64.b64encode(_RECOGNITION_IMAGE.read_bytes()).decode("ascii")
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": recognition_prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }]
        else:
            messages = [{"role": "user", "content": recognition_prompt}]
            note = t("config.conn.text_only_note", lang)
        result = client.chat(messages)
        reply = (result.content or "").strip()
        if len(reply) > _RECOGNITION_REPLY_LIMIT:
            reply = reply[:_RECOGNITION_REPLY_LIMIT] + "…"
        # usage 附带展示：prompt_tokens 含图片 token——数值明显偏高
        # （远超问句文本长度）即说明模型确实消费了图片。
        usage_note = ""
        if result.usage:
            usage_note = (
                f" [usage: prompt {result.usage.get('prompt_tokens', '?')}"
                f" + completion {result.usage.get('completion_tokens', '?')}"
                f" tokens]"
            )
        message = (t("config.conn.recognition_ok", lang) % (note, reply, usage_note)
                   if reply else t("config.conn.recognition_ok_empty", lang) % (note, usage_note))
    except LLMError as exc:
        return _render_test_result(request, ok=False, message=t("config.conn.recognition_fail", lang) % exc)
    except Exception as exc:  # 读文件/编码等任何意外都不得变成 500
        return _render_test_result(request, ok=False, message=t("config.conn.recognition_fail", lang) % exc)
    return _render_test_result(request, ok=True, message=message)


# ----------------------------------------------------------------- shared

def _render_test_result(request: Request, *, ok: bool, message: str) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        "config/_test_result.html",
        {"ok": ok, "message": message},
    )
