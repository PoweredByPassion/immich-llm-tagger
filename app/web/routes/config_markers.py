"""配置页：Marker 规则（marker_rules）CRUD。

condition 支持两种：
  · {"confidence_lt": float} —— 分类置信度低于阈值时打上该 marker 标签（表单一个
    0–1 数值输入，越界中文报错）；
  · {"on_refusal": true}     —— 模型拒答（或输出无法解析）时打上该 marker 标签，
    用于把"识别失败"的图单独标记出来供人工复核。
下拉只列 kind=marker 标签。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.store.crud import (
    delete_marker_rule,
    insert_marker_rule,
    set_marker_rule_enabled,
    update_marker_rule,
)
from app.store.errors import ValidationError
from app.store.tags import load_tags
from app.web.i18n import lang_from_cookie, render_problems, t
from app.web.routes.config_conn import _templates

router = APIRouter(prefix="/config")

# condition 类型（表单 condition_type 的取值，也是模板渲染分支的键）
CONDITION_CONFIDENCE = "confidence_lt"
CONDITION_ON_REFUSAL = "on_refusal"


def _db(request: Request):
    return request.app.state.db


def _marker_tags(db):
    return [t for t in load_tags(db) if t.kind == "marker"]


def _condition_kind(condition: dict) -> str:
    """把 condition 归到两种类型之一供模板渲染。写入时只会写一种，故 on_refusal 优先。"""
    return (CONDITION_ON_REFUSAL if condition.get("on_refusal")
            else CONDITION_CONFIDENCE)


def _parse_condition(raw: str) -> dict:
    try:
        cond = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return cond if isinstance(cond, dict) else {}


def _rules(db):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.id, r.marker_tag_id, r.condition, r.enabled, t.name AS tag_name "
            "  FROM marker_rules r LEFT JOIN tags t ON t.id = r.marker_tag_id "
            " ORDER BY r.id"
        ).fetchall()
    rules = []
    for row in rows:
        condition = _parse_condition(row["condition"])
        try:
            conf = float(condition["confidence_lt"])
        except (KeyError, TypeError, ValueError):
            conf = None
        rules.append({"row": row, "confidence_lt": conf,
                      "kind": _condition_kind(condition)})
    return rules


def _parse_confidence(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _page(request: Request, *, error: str | None = None, saved: bool = False):
    return _templates(request).TemplateResponse(
        request,
        "config/markers.html",
        {
            "rules": _rules(_db(request)),
            "marker_tags": _marker_tags(_db(request)),
            "error": error,
            "saved": saved,
        },
    )


def _create_or_update(request: Request, *, rule_id: int | None, form):
    lang = lang_from_cookie(request.cookies)
    raw_tag = str(form.get("marker_tag_id", "")).strip()
    # 旧表单（不带 condition_type）默认置信度阈值：保持既有行为兼容。
    kind = str(form.get("condition_type", "")).strip() or CONDITION_CONFIDENCE
    try:
        marker_tag_id = int(raw_tag)
    except ValueError:
        return _page(request, error=t("config.markers.select_tag", lang))

    if kind == CONDITION_ON_REFUSAL:
        condition: dict = {CONDITION_ON_REFUSAL: True}
    elif kind == CONDITION_CONFIDENCE:
        conf = _parse_confidence(str(form.get("confidence_lt", "")).strip())
        if conf is None:
            return _page(request, error=t("config.markers.conf_not_number", lang))
        if not (0 <= conf <= 1):
            return _page(request, error=t("config.markers.conf_out_of_range", lang))
        condition = {CONDITION_CONFIDENCE: conf}
    else:
        return _page(request, error=t("config.markers.cond_unknown", lang))

    try:
        if rule_id is None:
            insert_marker_rule(
                _db(request), marker_tag_id=marker_tag_id,
                condition=condition,
            )
        else:
            update_marker_rule(
                _db(request), rule_id, marker_tag_id=marker_tag_id,
                condition=condition,
            )
    except ValidationError as exc:
        return _page(request, error=render_problems(exc.problems, lang))
    return _page(request, saved=True)


@router.get("/markers", response_class=HTMLResponse)
def markers_page(request: Request):
    return _page(request)


@router.post("/markers", response_class=HTMLResponse)
async def markers_create(request: Request):
    return _create_or_update(request, rule_id=None, form=await request.form())


@router.post("/markers/{rule_id}/update", response_class=HTMLResponse)
async def markers_update(request: Request, rule_id: int):
    return _create_or_update(request, rule_id=rule_id, form=await request.form())


@router.post("/markers/{rule_id}/toggle", response_class=HTMLResponse)
def markers_toggle(request: Request, rule_id: int):
    with _db(request).connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM marker_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    if row is not None:
        set_marker_rule_enabled(_db(request), rule_id, not bool(row["enabled"]))
    return _page(request)


@router.post("/markers/{rule_id}/delete", response_class=HTMLResponse)
def markers_delete(request: Request, rule_id: int):
    delete_marker_rule(_db(request), rule_id)
    return _page(request)
