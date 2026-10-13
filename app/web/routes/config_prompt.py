"""配置页：Prompt 模板编辑（保存即新版本）、版本激活切换与沙盒。

预览必须复用 app.classify.prompt.render_prompt —— 与真实管线同一个渲染函数，
绝不在此处另写拼接逻辑。激活语义：null = 跟随最新版本；
build_pipeline_config 侧同样实现该回退。
"""
from __future__ import annotations

import random

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.classify.prompt import PromptContext, render_prompt
from app.clients.immich import ImmichClient, ImmichError
from app.clients._immich_gen.models.metadata_search_dto import MetadataSearchDto
from app.clients._immich_gen.models.asset_response_dto import AssetTypeEnum
from app.store.connections import ConnectionStore
from app.store.crud import (
    create_prompt_version,
    get_latest_prompt_version,
    list_prompt_versions,
)
from app.store.errors import ValidationError
from app.store.jobs import create_job
from app.store.settings import SettingsStore
from app.store.tags import load_tags
from app.store.validation import TagRow, validate_config
from app.web.i18n import DEFAULT_LANG, lang_from_cookie, render_problems, t

router = APIRouter(prefix="/config")


def _db(request: Request):
    return request.app.state.db


def _templates(request: Request):
    return request.app.state.templates


# ------------------------------------------------------------------ 示例注入块

def sample_prompt_context(lang: str = DEFAULT_LANG) -> PromptContext:
    """构造一组代表性的示例注入数据，供预览端点走真实渲染器。

    结构与管线一致：tag_list 是 (名称, 描述) 元组列表；filename/exif/scope/date
    覆盖全部可选占位符，方便用户看到渲染后的完整效果（缺省的占位符行会被
    渲染器整行省略）。示例数据随请求语言渲染。
    """
    return PromptContext(
        tag_list=[
            (t("config.prompt.sample.tag1_name", lang), t("config.prompt.sample.tag1_desc", lang)),
            (t("config.prompt.sample.tag2_name", lang), t("config.prompt.sample.tag2_desc", lang)),
            (t("config.prompt.sample.tag3_name", lang), t("config.prompt.sample.tag3_desc", lang)),
        ],
        filename="IMG_4032.jpg",
        exif={
            "make": "Apple",
            "model": "iPhone 15 Pro",
            "iso": 125,
            "f_number": 1.78,
            "date_original": "2026-10-01 14:23:00",
        },
        scope=t("config.prompt.sample.scope", lang),
        date="2026-10-01",
    )


# ------------------------------------------------------------------ token 估算

def estimate_tokens(text: str) -> int:
    """按字符近似估算 token 数（不引 tokenizer 依赖）。

    近似公式：非 CJK 按 4 字符/token，CJK（中日韩全角区）按 1 字符 ≈ 1.5 token
    加权，合计取整。仅为规模参考，不等于任何具体 tokenizer 的结果。
    """
    cjk = sum(1 for ch in text if "　" <= ch <= "鿿" or "！" <= ch <= "｡")
    other = len(text) - cjk
    return max(1, round(other / 4 + cjk * 1.5))


def _page_context(db, **extra) -> dict:
    from app.engine.executor import DEFAULT_PROMPT_TEMPLATE
    ctx = {
        "latest": get_latest_prompt_version(db),
        "versions": list_prompt_versions(db),
        "sample": sample_prompt_context(),
        "active_id": SettingsStore(db).get("active_prompt_version_id"),
        # 无任何版本时编辑框预填内置默认模板，保证"所见即生效"
        "default_template": DEFAULT_PROMPT_TEMPLATE,
    }
    ctx.update(extra)
    return ctx


def _check_validate_config(db, lang: str) -> str | None:
    """激活前走 validate_config（active id 必须存在于 prompt_versions）。"""
    with db.connect() as conn:
        rules = conn.execute(
            "SELECT id, marker_tag_id FROM marker_rules WHERE enabled = 1"
        ).fetchall()
        pv_ids = {
            r["id"] for r in conn.execute(
                "SELECT id FROM prompt_versions").fetchall()
        }
    tags = [
        TagRow(id=t.id, name=t.name, kind=t.kind, enabled=t.enabled)
        for t in load_tags(db)
    ]
    settings = SettingsStore(db)
    try:
        validate_config(
            tags=tags,
            marker_rules=[(r["id"], r["marker_tag_id"]) for r in rules],
            fallback_tag_name=settings.get("fallback_tag_name"),
            active_prompt_version_id=settings.get("active_prompt_version_id"),
            prompt_version_ids=pv_ids,
        )
    except ValidationError as exc:
        return render_problems(exc.problems, lang)
    return None


# ------------------------------------------------------------------ 页面

@router.get("/prompt", response_class=HTMLResponse)
def prompt_page(request: Request):
    return _templates(request).TemplateResponse(
        request, "config/prompt.html", _page_context(_db(request))
    )


@router.post("/prompt", response_class=HTMLResponse)
async def prompt_save(request: Request):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    form = await request.form()
    body = str(form.get("body", ""))
    note = str(form.get("note", "")).strip()
    try:
        create_prompt_version(db, body=body, note=note)
    except ValidationError as exc:  # store：空 body
        return _templates(request).TemplateResponse(
            request,
            "config/prompt.html",
            _page_context(db, error=render_problems(exc.problems, lang)),
        )
    return _templates(request).TemplateResponse(
        request,
        "config/prompt.html",
        _page_context(db, saved=True),
    )


@router.post("/prompt/preview", response_class=HTMLResponse)
async def prompt_preview(request: Request):
    lang = lang_from_cookie(request.cookies)
    form = await request.form()
    body = str(form.get("body", ""))
    if not body.strip():
        return HTMLResponse(
            '<p class="text-sm text-amber-300">'
            + t("config.prompt.preview_empty", lang) + '</p>')
    rendered = render_prompt(body, sample_prompt_context(lang))
    tokens = estimate_tokens(rendered)
    return _templates(request).TemplateResponse(
        request,
        "config/_prompt_preview.html",
        {"rendered": rendered, "tokens": tokens},
    )


# ------------------------------------------------------------------ 激活切换

@router.post("/prompt/{version_id}/activate", response_class=HTMLResponse)
def prompt_activate(request: Request, version_id: int):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    if version_id not in {v.id for v in list_prompt_versions(db)}:
        return _templates(request).TemplateResponse(
            request,
            "config/prompt.html",
            _page_context(db, error=t("config.prompt.version_missing", lang) % version_id),
        )
    SettingsStore(db).set_active_prompt_version(version_id)
    problem = _check_validate_config(db, lang)
    if problem is not None:
        return _templates(request).TemplateResponse(
            request,
            "config/prompt.html",
            _page_context(db, error=t("config.prompt.config_invalid", lang) % problem),
        )
    return _templates(request).TemplateResponse(
        request,
        "config/prompt.html",
        _page_context(db, activated=True),
    )


@router.post("/prompt/activate-default", response_class=HTMLResponse)
def prompt_use_default(request: Request):
    db = _db(request)
    SettingsStore(db).set_active_prompt_version(None)
    return _templates(request).TemplateResponse(
        request,
        "config/prompt.html",
        _page_context(db, use_default=True),
    )


# ------------------------------------------------------------------ 沙盒

@router.post("/prompt/sandbox", response_class=HTMLResponse)
async def prompt_sandbox(request: Request):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    form = await request.form()
    asset_id = str(form.get("asset_id", "")).strip()
    if not asset_id:
        return _templates(request).TemplateResponse(
            request,
            "config/prompt.html",
            _page_context(db, sandbox_error=t("config.prompt.sandbox_asset_required", lang)),
        )
    # 沙盒试跑当前编辑框里的模板（未保存的草稿也能试）：存进 job params，
    # executor 侧 build_pipeline_config 检测到该字段时优先使用。
    # 只剥首尾换行/空白，不 strip 内容中间（模板正文原样透传）。
    body = str(form.get("body", "")).strip()
    params = {"asset_id": asset_id, "source": "manual"}
    if body:
        params["prompt_body"] = body
    create_job(db, "dryrun", params)
    return _templates(request).TemplateResponse(
        request,
        "config/prompt.html",
        _page_context(
            db, sandbox_saved=True, sandbox_asset_id=asset_id),
    )


@router.post("/prompt/sandbox/random", response_class=HTMLResponse)
def prompt_sandbox_random(request: Request):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    conn_cfg = ConnectionStore(db).get_immich()
    if not conn_cfg.url:
        return _templates(request).TemplateResponse(
            request,
            "config/prompt.html",
            _page_context(
                db, sandbox_error=t("common.immich_not_configured", lang)),
        )
    try:
        # 显式 timeout（CLAUDE.md 规矩）
        client = ImmichClient(
            base_url=conn_cfg.url,
            api_key=conn_cfg.api_key,
            verify_ssl=conn_cfg.verify_ssl,
            timeout=30.0,
        )
        from app.classify.scope import load_scope_rules, scan_queue_assets
        rules = load_scope_rules(db)
        candidates = scan_queue_assets(
            client, db, SettingsStore(db).get("queue_tag_name"), rules)
        if not candidates:
            # 队列空时退回范围规则内取第一页随机一张
            from app.classify.scope import in_scope
            dto = MetadataSearchDto(type_=AssetTypeEnum.IMAGE)
            dto.page = 1
            dto.size = 50
            result = client._run(
                __import__(
                    "app.clients._immich_gen.api.search",
                    fromlist=["search_assets"]).search_assets.sync_detailed,
                body=dto,
            ).parsed
            items = list(result.assets.items) if result and result.assets else []
            candidates = [
                str(a.id) for a in items
                if in_scope(rules, library_id=a.library_id, path=a.original_path)
            ]
        if not candidates:
            return _templates(request).TemplateResponse(
                request,
                "config/prompt.html",
                _page_context(db, sandbox_error=t("config.prompt.no_assets_in_scope", lang)),
            )
        picked = random.choice(candidates)
    except ImmichError as exc:
        return _templates(request).TemplateResponse(
            request,
            "config/prompt.html",
            _page_context(db, sandbox_error=t("config.prompt.random_fetch_fail", lang) % exc),
        )
    return _templates(request).TemplateResponse(
        request,
        "config/prompt.html",
        _page_context(db, sandbox_random=picked),
    )
