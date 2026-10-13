"""配置页：标签 Tags 的增删改。

表单空串统一转 None 落库（album / min_confidence 可为 NULL，不存空字符串）；
kind 白名单与唯一名冲突走 crud 的 ValidationError。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.store import crud
from app.store.errors import Problem, ValidationError
from app.store.tags import load_tags
from app.web.i18n import lang_from_cookie, render_problems, t

router = APIRouter(prefix="/config")

_KINDS = ("primary", "marker")


def _db(request: Request):
    return request.app.state.db


def _templates(request: Request):
    return request.app.state.templates


def _optional_str(form, name: str) -> str | None:
    raw = str(form.get(name, "")).strip()
    return raw or None


def _optional_float(form, name: str) -> float | None:
    raw = str(form.get(name, "")).strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValidationError([Problem("config.tags.name_is_number", (name, repr(raw)))]) from None


def _render_tags(request: Request, *, error: str | None = None,
                 saved: bool = False, saved_msg: str | None = None):
    return _templates(request).TemplateResponse(
        request,
        "config/tags.html",
        {
            "tags": load_tags(_db(request)),
            "kinds": _KINDS,
            "error": error,
            "saved": saved,
            "saved_msg": saved_msg,
            "form": {},
        },
    )


# ---------------------------------------------------------------------- tags

@router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    return _render_tags(request)


@router.post("/tags", response_class=HTMLResponse)
async def tag_create(request: Request):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    form = await request.form()
    try:
        crud.insert_tag(
            db,
            name=str(form.get("name", "")).strip(),
            kind=str(form.get("kind", "")),
            description=str(form.get("description", "")),
            archive=form.get("archive") is not None,
            album=_optional_str(form, "album"),
            min_confidence=_optional_float(form, "min_confidence"),
            skip_immich=form.get("skip_immich") is not None,
        )
    except ValidationError as exc:
        return _render_tags(request, error=render_problems(exc.problems, lang))
    return _render_tags(request, saved=True, saved_msg=t("config.tags.created", lang))


@router.post("/tags/{tag_id}", response_class=HTMLResponse)
async def tag_update(request: Request, tag_id: int):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    form = await request.form()
    try:
        crud.update_tag(
            db,
            tag_id,
            name=str(form.get("name", "")).strip(),
            kind=str(form.get("kind", "")),
            description=str(form.get("description", "")),
            archive=form.get("archive") is not None,
            album=_optional_str(form, "album"),
            min_confidence=_optional_float(form, "min_confidence"),
            enabled=form.get("enabled") is not None,
            skip_immich=form.get("skip_immich") is not None,
        )
    except ValidationError as exc:
        return _render_tags(request, error=render_problems(exc.problems, lang))
    return _render_tags(request, saved=True)


@router.post("/tags/{tag_id}/toggle", response_class=HTMLResponse)
def tag_toggle(request: Request, tag_id: int):
    db = _db(request)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
    if row is None:
        return _render_tags(request, error=t("config.tags.not_found", lang_from_cookie(request.cookies)))
    crud.set_tag_enabled(db, tag_id, not bool(row["enabled"]))
    return _render_tags(request, saved=True)


@router.post("/tags/{tag_id}/delete", response_class=HTMLResponse)
def tag_delete(request: Request, tag_id: int):
    lang = lang_from_cookie(request.cookies)
    try:
        crud.delete_tag(_db(request), tag_id)
    except ValidationError as exc:
        return _render_tags(request, error=render_problems(exc.problems, lang))
    return _render_tags(request, saved=True)
