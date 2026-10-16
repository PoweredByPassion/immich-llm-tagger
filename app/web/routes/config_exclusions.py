"""配置页：排除规则（exclusions）CRUD。

排除语义：被排除的图片根本不进入自动分类候选
（区别于"已完成"计入 total）。type=reviewed 无值，命中"已复核"的图片即排除。

页面已与「处理范围」合并（见 config_scope.render_page）；本模块只保留动作端点，
渲染一律走 render_page，避免两半分页。GET /config/exclusions 的重定向在
config_scope.py 里注册。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.store.crud import (
    delete_exclusion,
    insert_exclusion,
    set_exclusion_enabled,
    update_exclusion,
)
from app.store.errors import ValidationError
from app.web.i18n import lang_from_cookie, render_problems
from app.web.routes.config_scope import render_page

router = APIRouter(prefix="/config")


def _db(request: Request):
    return request.app.state.db


def _create_or_update(request: Request, *, rule_id: int | None, form):
    lang = lang_from_cookie(request.cookies)
    type_ = str(form.get("type", "")).strip()
    value = str(form.get("value", "")).strip()
    try:
        if rule_id is None:
            insert_exclusion(_db(request), type=type_, value=value)
        else:
            update_exclusion(_db(request), rule_id, type=type_, value=value)
    except ValidationError as exc:
        return render_page(request, error=render_problems(exc.problems, lang))
    return render_page(request, saved=True)


@router.post("/exclusions", response_class=HTMLResponse)
async def exclusions_create(request: Request):
    return _create_or_update(request, rule_id=None, form=await request.form())


@router.post("/exclusions/{rule_id}/update", response_class=HTMLResponse)
async def exclusions_update(request: Request, rule_id: int):
    return _create_or_update(request, rule_id=rule_id, form=await request.form())


@router.post("/exclusions/{rule_id}/toggle", response_class=HTMLResponse)
def exclusions_toggle(request: Request, rule_id: int):
    with _db(request).connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM exclusions WHERE id = ?", (rule_id,)
        ).fetchone()
    if row is not None:
        set_exclusion_enabled(_db(request), rule_id, not bool(row["enabled"]))
    return render_page(request)


@router.post("/exclusions/{rule_id}/delete", response_class=HTMLResponse)
def exclusions_delete(request: Request, rule_id: int):
    delete_exclusion(_db(request), rule_id)
    return render_page(request)
