"""配置导入导出页。

- GET  /config/transfer         页面（导出按钮 + 导入上传表单 + 二次确认）
- GET  /config/transfer/export  下载 YAML（attachment，无任何凭证）
- POST /config/transfer/import  上传 YAML 覆盖配置（单事务，失败整体回滚）
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import UploadFile

from app.store.errors import ValidationError
from app.store.transfer import (
    export_config,
    import_config,
    parse_yaml,
    render_yaml,
)
from app.web.i18n import lang_from_cookie, render_problems, t
from app.web.routes.config_conn import _templates

router = APIRouter(prefix="/config")

# 结果页提示文案（不导出的字段导入后保留 DB 现值，需手动配置）
_SKIPPED_SENSITIVE_KEYS = [
    "config.transfer.sensitive.immich_key",
    "config.transfer.sensitive.llm_key",
    "config.transfer.sensitive.webhook_token",
    "config.transfer.sensitive.active_prompt",
]


def _db(request: Request):
    return request.app.state.db


def _page(request: Request, *, error: str | None = None,
          imported: bool = False):
    lang = lang_from_cookie(request.cookies)
    return _templates(request).TemplateResponse(
        request,
        "config/transfer.html",
        {"error": error, "imported": imported,
         "skipped": t("common.list_sep", lang).join(
             t(k, lang) for k in _SKIPPED_SENSITIVE_KEYS)},
    )


@router.get("/transfer", response_class=HTMLResponse)
def transfer_page(request: Request):
    return _page(request)


@router.get("/transfer/export", response_class=Response)
def transfer_export(request: Request):
    yaml_text = render_yaml(export_config(_db(request)))
    # 文件名带导出时刻，方便区分不同日期时间的配置快照
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=yaml_text,
        media_type="text/yaml",
        headers={
            "Content-Disposition":
                f'attachment; filename="autotag-config-{stamp}.yaml"'
        },
    )


@router.post("/transfer/import", response_class=HTMLResponse)
async def transfer_import(request: Request):
    lang = lang_from_cookie(request.cookies)
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _page(request, error=t("config.transfer.import_no_file", lang))
    raw = await upload.read()
    try:
        data = parse_yaml(raw.decode("utf-8"))
        import_config(_db(request), data)
    except ValidationError as e:
        return _page(request, error=render_problems(e.problems, lang))
    except ValueError as e:
        # utf-8 解码失败等
        return _page(request, error=t("config.transfer.import_read_fail", lang) % e)
    return _page(request, imported=True)
