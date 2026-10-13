"""危险区页：重置预览 + 执行。

破坏性操作需输入确认词 "RESET"（危险等级高于普通二次确认）；
任一活动 job（任意类型）存在时禁止重置（重置与一切 job 互斥）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.classify.reset_ops import reset_preview
from app.clients.immich import ImmichClient, ImmichError
from app.store.connections import ConnectionStore
from app.store.jobs import create_job, has_active_job
from app.web.i18n import lang_from_cookie, t
from app.web.routes.config_conn import _templates

router = APIRouter()


def _db(request: Request):
    return request.app.state.db


def _immich_client(db):
    conn = ConnectionStore(db).get_immich()
    if not conn.url:
        return None
    return ImmichClient(base_url=conn.url, api_key=conn.api_key,
                        verify_ssl=conn.verify_ssl, timeout=30.0)


def _page(request: Request, *, error: str | None = None):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    unarchive_count, tag_names = 0, []
    conn = _immich_client(db)
    error = error or (None if conn else t("danger.immich_not_configured", lang))
    if conn and error is None:
        try:
            unarchive_count, tag_names = reset_preview(db, conn)
        except ImmichError as exc:
            error = t("danger.preview_fail", lang) % exc
    return _templates(request).TemplateResponse(
        request,
        "danger.html",
        {"unarchive_count": unarchive_count, "tag_names": tag_names,
         "error": error, "has_active": has_active_job(db)},
    )


@router.get("/danger", response_class=HTMLResponse)
def danger_page(request: Request):
    return _page(request)


@router.post("/danger/reset", status_code=303)
async def danger_reset(request: Request):
    lang = lang_from_cookie(request.cookies)
    db = _db(request)
    form = await request.form()
    if str(form.get("confirm") or "").strip() != "RESET":
        return _error_422(request, t("danger.confirm_word_wrong", lang))
    if has_active_job(db):
        return _error_422(request, t("danger.job_active", lang))
    job_id = create_job(db, "reset", params={})
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _error_422(request: Request, message: str) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        "danger.html",
        {"unarchive_count": 0, "tag_names": [], "error": message,
         "has_active": has_active_job(_db(request))},
        status_code=422,
    )
