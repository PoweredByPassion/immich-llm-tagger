"""配置页：处理范围（scope_rules）CRUD。

scope 匹配语义（app/classify/scope.py，文案以此为准）：
- 只有启用的规则参与匹配；多条规则任一命中即在范围内（OR）。
- library_id 精确相等才命中，空串不匹配任何资产（通配靠 path_prefix 段匹配）。
- path_prefix 是路径段匹配：/foo 匹配 /foo 与 /foo/bar，不匹配 /foobar。
- 无任何启用规则 → 所有资产视为在范围内（不拦截），页面必须明示此 fallback。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clients.immich import ImmichClient, ImmichError
from app.store.connections import ConnectionStore
from app.store.crud import delete_scope_rule, insert_scope_rule, set_scope_rule_enabled
from app.store.jobs import create_job, has_active_job_of_type
from app.web.i18n import lang_from_cookie, t
from app.web.routes.config_conn import _templates

router = APIRouter(prefix="/config")

# 拉库列表属于页面装饰性调用，绝不能让配置页卡住太久；显式超时（CLAUDE.md 规矩）。
LIBRARIES_TIMEOUT = 30.0


def _db(request: Request):
    return request.app.state.db


def _libraries(db) -> list[dict]:
    """拉取 Immich 外部库用于下拉提示；未配置 / 无权限 / 任何失败一律降级为空列表。"""
    conn = ConnectionStore(db).get_immich()
    if not conn.url:
        return []
    try:
        client = ImmichClient(
            base_url=conn.url,
            api_key=conn.api_key,
            verify_ssl=conn.verify_ssl,
            timeout=LIBRARIES_TIMEOUT,
        )
        return [
            {"id": str(lib.id), "name": lib.name, "paths": list(lib.import_paths)}
            for lib in client.list_libraries()
        ]
    except ImmichError as exc:
        logging.warning("拉取 Immich 库列表失败（%s），scope 页回落到手动输入", exc)
        return []
    except Exception:  # 未预期的失败也不能冒泡成 500
        logging.warning("拉取 Immich 库列表异常，scope 页回落到手动输入", exc_info=True)
        return []


def _rules(db):
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, library_id, path_prefix, enabled FROM scope_rules ORDER BY id"
        ).fetchall()


def _validate(library_id: str, path_prefix: str, lang: str) -> list[str]:
    problems: list[str] = []
    if not library_id and not path_prefix:
        problems.append(t("config.scope.rule_required", lang))
    return problems


def _page(request: Request, *, error: str | None = None, saved: bool = False,
          libraries: list[dict] | None = None):
    libraries = libraries or []
    # 聚合去重所有库的 import_paths，供 path_prefix 的 datalist 提示
    # （未选库时展示全部；选中库后前端联动只展示该库的）。
    import_paths = sorted({p for lib in libraries for p in lib["paths"] if p})
    lib_paths = {lib["id"]: sorted(p for p in lib["paths"] if p) for lib in libraries}
    return _templates(request).TemplateResponse(
        request,
        "config/scope.html",
        {"rules": _rules(_db(request)), "error": error, "saved": saved,
         "libraries": libraries, "import_paths": import_paths,
         "lib_paths": lib_paths,
         "backfill_active": has_active_job_of_type(_db(request), "backfill")},
    )


@router.get("/scope", response_class=HTMLResponse)
def scope_page(request: Request):
    return _page(request, libraries=_libraries(_db(request)))


@router.post("/scope", response_class=HTMLResponse)
async def scope_create(request: Request):
    lang = lang_from_cookie(request.cookies)
    form = await request.form()
    library_id = str(form.get("library_id", "")).strip()
    path_prefix = str(form.get("path_prefix", "")).strip()
    problems = _validate(library_id, path_prefix, lang)
    if problems:
        return _page(request, error=t("common.semi_sep", lang).join(problems),
                     libraries=_libraries(_db(request)))
    insert_scope_rule(_db(request), library_id=library_id, path_prefix=path_prefix)
    return _page(request, saved=True, libraries=_libraries(_db(request)))


@router.post("/scope/{rule_id}/toggle", response_class=HTMLResponse)
def scope_toggle(request: Request, rule_id: int):
    with _db(request).connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM scope_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    if row is not None:
        set_scope_rule_enabled(_db(request), rule_id, not bool(row["enabled"]))
    return _page(request, libraries=_libraries(_db(request)))


@router.post("/scope/{rule_id}/delete", response_class=HTMLResponse)
def scope_delete(request: Request, rule_id: int):
    delete_scope_rule(_db(request), rule_id)
    return _page(request, libraries=_libraries(_db(request)))


@router.post("/scope/backfill")
def scope_backfill(request: Request):
    """全量回填：为范围内未处理图片打队列标签。

    已有活动 backfill 时不重复建任务（并发由 has_active_job_of_type 兜底），
    直接跳到现有任务详情。"""
    db = _db(request)
    active = has_active_job_of_type(db, "backfill")
    job_id = create_job(db, "backfill", params={}) if not active else None
    target = f"/jobs/{job_id}" if job_id is not None else "/jobs"
    return RedirectResponse(target, status_code=303)
