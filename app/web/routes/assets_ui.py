"""图片记录页：列表（筛选 + 分页）、详情（快照 + 历史 + 原始返回）、
从 Immich 同步 asset_state。

同步的标签口径与 app/classify/apply.py 写回逻辑对齐：current_tag 取
Immich 资产上命中的本地 tags 表 primary 标签，current_markers 取命中的
marker 标签（两者都只认本地 tags 表里登记过的名字，Immich 上的其他标签
不进本地快照）；visibility 直接采用 Immich 返回的 visibility 值
（"timeline"/"archive"/…，与 apply.py 的 set_visibility 取值同一来源）。
所有失败路径渲染错误横幅，绝不 500。
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse


from app.classify.manual import apply_override, clear_override, mark_reviewed
from app.store.jobs import create_job, get_job
from app.clients.immich import ImmichClient, ImmichError
from app.store.assets import (
    count_records,
    current_tag_counts,
    get_state,
    query_records,
    records_for_asset,
)
from app.store.connections import ConnectionStore
from app.store.settings import SettingsStore
from app.store.tags import load_tags
from app.web.i18n import lang_from_cookie, t

router = APIRouter(prefix="/assets")

PAGE_SIZE = 50

# 测试 hook：记录构造出的客户端实例（生产代码不用它）
_fake_instances: list = []


def _db(request: Request):
    return request.app.state.db


def _templates(request: Request):
    return request.app.state.templates


def _int_or_none(raw: str | None) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@router.get("/", response_class=HTMLResponse)
def asset_list(request: Request):
    db = _db(request)
    status = request.query_params.get("status") or None
    current_tag = request.query_params.get("current_tag") or None
    source = request.query_params.get("source") or None
    job_id = _int_or_none(request.query_params.get("job_id"))
    dry_run_raw = request.query_params.get("dry_run")
    dry_run = None if dry_run_raw in (None, "") else (
        1 if dry_run_raw == "1" else 0)
    page = max(1, _int_or_none(request.query_params.get("page")) or 1)

    filters = dict(
        status=status, current_tag=current_tag, source=source, job_id=job_id,
        dry_run=dry_run,
    )
    total = count_records(db, **filters)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    records = query_records(db, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                            **filters)
    tag_names = sorted({t.name for t in load_tags(db)})
    tag_counts = current_tag_counts(db)
    return _templates(request).TemplateResponse(
        request,
        "assets/list.html",
        {
            "records": records,
            "total": total,
            "page": page,
            "pages": pages,
            "has_prev": page > 1,
            "has_next": page < pages,
            "f_status": status or "",
            "f_current_tag": current_tag or "",
            "f_source": source or "",
            "f_job_id": "" if job_id is None else str(job_id),
            "f_dry_run": "" if dry_run is None else str(dry_run),
            "tag_names": tag_names,
            "tag_counts": tag_counts,
            "sources": ["webhook", "poll", "manual"],
        },
    )


@router.get("/{asset_id}", response_class=HTMLResponse)
def asset_detail(request: Request, asset_id: str):
    db = _db(request)
    records = records_for_asset(db, asset_id)
    if not records:
        return _templates(request).TemplateResponse(
            request,
            "assets/notfound.html",
            {"asset_id": asset_id},
            status_code=404,
        )
    state = get_state(db, asset_id)
    if state is not None:
        state["current_markers_list"] = json.loads(state["current_markers"] or "[]")
    tag_defs = load_tags(db)
    primary_tags = [t for t in tag_defs if t.kind == "primary" and t.enabled]
    marker_tags = [t for t in tag_defs if t.kind == "marker" and t.enabled]
    # last_job_id 是软引用（job 可能已被 retention 清理）——过期则不渲染跳转链接
    state_job_exists = (
        bool(state and state.get("last_job_id"))
        and get_job(db, state["last_job_id"]) is not None
    )
    return _templates(request).TemplateResponse(
        request,
        "assets/detail.html",
        {
            "asset_id": asset_id,
            "state": state,
            "records": records,
            "primary_tags": primary_tags,
            "marker_tags": marker_tags,
            "state_job_exists": state_job_exists,
            # _state_block.html 的重试胶囊需要「尝试次数/上限」两个数
            "apply_max_attempts": SettingsStore(db).get("apply_max_attempts"),
        },
    )


@router.get("/{asset_id}/thumb")
def asset_thumb(request: Request, asset_id: str):
    """代理 Immich 缩略图（原始字节直出）。失败一律回落 404，由前端
    onerror 显示占位图标，绝不 500、绝不冒泡。"""
    conn = ConnectionStore(_db(request)).get_immich()
    if not conn.url:
        return Response(status_code=404)
    try:
        client = ImmichClient(
            base_url=conn.url,
            api_key=conn.api_key,
            verify_ssl=conn.verify_ssl,
            timeout=30.0,
        )
        _fake_instances.append(client)
        raw = client.fetch_thumbnail_bytes(asset_id, "thumbnail")
    except ImmichError:
        return Response(status_code=404)
    return Response(content=raw, media_type="image/jpeg")


def _review_result(request: Request, asset_id: str) -> HTMLResponse:
    db = _db(request)
    state = get_state(db, asset_id)
    if state is not None:
        state["current_markers_list"] = json.loads(state["current_markers"] or "[]")
    return _templates(request).TemplateResponse(
        request,
        "assets/_review_result.html",
        {"ok": True, "asset_id": asset_id, "state": state,
         "apply_max_attempts": SettingsStore(db).get("apply_max_attempts")},
    )


@router.post("/{asset_id}/review", response_class=HTMLResponse)
def asset_review(request: Request, asset_id: str):
    """标记/取消已复核（toggle）。无 Immich 变更，不写 change_log。"""
    db = _db(request)
    state = get_state(db, asset_id)
    mark_reviewed(db, asset_id, reviewed=not (state or {}).get("reviewed"))
    return _review_result(request, asset_id)


@router.post("/{asset_id}/override", response_class=HTMLResponse)
async def asset_override(request: Request, asset_id: str):
    """人工改判：摘旧 → 打新 → change_log(job_id=NULL)。"""
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    form = await request.form()
    new_tag = str(form.get("tag") or "").strip()
    markers = [str(m) for m in form.getlist("markers") if str(m).strip()]
    if not new_tag:
        return _templates(request).TemplateResponse(
            request,
            "assets/_override_result.html",
            {"ok": False, "message": t("assets.override.select_tag", lang), "asset_id": asset_id},
        )
    conn = ConnectionStore(db).get_immich()
    if not conn.url:
        return _templates(request).TemplateResponse(
            request,
            "assets/_override_result.html",
            {"ok": False, "message": t("common.immich_not_configured", lang),
             "asset_id": asset_id},
        )
    try:
        client = ImmichClient(
            base_url=conn.url,
            api_key=conn.api_key,
            verify_ssl=conn.verify_ssl,
            timeout=30.0,
        )
        _fake_instances.append(client)
        apply_override(db, client, asset_id, new_tag, marker_tags=markers)
    except ImmichError as exc:
        return _templates(request).TemplateResponse(
            request,
            "assets/_override_result.html",
            {"ok": False, "message": t("assets.override.fail", lang) % exc, "asset_id": asset_id},
        )
    state = get_state(db, asset_id)
    if state is not None:
        state["current_markers_list"] = json.loads(state["current_markers"] or "[]")
    return _templates(request).TemplateResponse(
        request,
        "assets/_override_result.html",
        {"ok": True, "asset_id": asset_id, "state": state,
         "apply_max_attempts": SettingsStore(db).get("apply_max_attempts")},
    )


@router.post("/{asset_id}/override/clear", response_class=HTMLResponse)
def asset_override_clear(request: Request, asset_id: str):
    """取消改判：仅恢复自动处理资格，不回滚标签。"""
    clear_override(_db(request), asset_id)
    return _review_result(request, asset_id)


@router.post("/{asset_id}/reclassify")
def asset_reclassify(request: Request, asset_id: str):
    """重新分类：建 reclassify job（统一进度/取消），303 跳任务详情。"""
    db = _db(request)
    job_id = create_job(db, "reclassify", params={"asset_ids": [asset_id]})
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{asset_id}/sync", response_class=HTMLResponse)
def asset_sync(request: Request, asset_id: str):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    state = get_state(db, asset_id)
    if state is None:
        return _templates(request).TemplateResponse(
            request,
            "assets/_sync_result.html",
            {"ok": False, "message": t("assets.sync.no_record", lang)},
        )
    conn = ConnectionStore(db).get_immich()
    if not conn.url:
        return _templates(request).TemplateResponse(
            request,
            "assets/_sync_result.html",
            {"ok": False, "message": t("common.immich_not_configured", lang)},
        )
    # 显式 timeout（CLAUDE.md 规矩）；连接配置暂无超时字段，固定 30s
    try:
        client = ImmichClient(
            base_url=conn.url,
            api_key=conn.api_key,
            verify_ssl=conn.verify_ssl,
            timeout=30.0,
        )
        _fake_instances.append(client)
        asset = client.get_asset(asset_id)
    except ImmichError as exc:
        return _templates(request).TemplateResponse(
            request,
            "assets/_sync_result.html",
            {"ok": False, "message": t("assets.sync.fail", lang) % exc},
        )

    # 标签口径：只认本地 tags 表登记的 primary / marker 名字
    asset_tag_names = [t.name for t in (asset.tags or [])]
    tag_defs = load_tags(db)
    primary_names = {t.name for t in tag_defs if t.kind == "primary"}
    marker_names = {t.name for t in tag_defs if t.kind == "marker"}
    current_tag = next((n for n in asset_tag_names if n in primary_names), None)
    current_markers = [n for n in asset_tag_names if n in marker_names]
    visibility = str(asset.visibility.value) if hasattr(
        asset.visibility, "value"
    ) else str(asset.visibility)

    with db.connect() as sqlconn:
        sqlconn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "visibility, updated_at) VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "current_tag = excluded.current_tag, "
            "current_markers = excluded.current_markers, "
            "visibility = excluded.visibility, updated_at = datetime('now')",
            (asset_id, current_tag, json.dumps(current_markers), visibility),
        )
    return _templates(request).TemplateResponse(
        request,
        "assets/_sync_result.html",
        {
            "ok": True,
            "message": t("assets.sync.done", lang),
            "state": get_state(db, asset_id),
            "asset_id": asset_id,
            "apply_max_attempts": SettingsStore(db).get("apply_max_attempts"),
        },
    )
