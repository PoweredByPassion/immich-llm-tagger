"""配置页：处理范围（scope_rules）+ 排除规则（exclusions）合并页。

两者语义相反但同属"筛选"：scope 决定**要处理什么**，exclusions 决定
**不处理什么**。原先分两页，用户需要在两页之间来回对照，故合并到
`config/scope.html` 一页上下两半。

scope 匹配语义（app/classify/scope.py，文案以此为准）：
- 只有启用的规则参与匹配；多条规则任一命中即在范围内（OR）。
- library_id 精确相等才命中，空串不匹配任何资产（通配靠 path_prefix 段匹配）。
- path_prefix 是路径段匹配：/foo 匹配 /foo 与 /foo/bar，不匹配 /foobar。
- 无任何启用规则 → 所有资产视为在范围内（不拦截），页面必须明示此 fallback。

排除规则本身的 CRUD 仍在 `config_exclusions.py`，但渲染复用本模块的
`render_page`，保证两半永远在同一页渲染。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clients.immich import ImmichClient, ImmichError
from app.store.connections import ConnectionStore
from app.store.crud import delete_scope_rule, insert_scope_rule, set_scope_rule_enabled
from app.store.jobs import create_job, has_active_job_of_type
from app.store.tags import load_tags
from app.web.i18n import lang_from_cookie, t

router = APIRouter(prefix="/config")

# 拉库列表属于页面装饰性调用，绝不能让配置页卡住太久；显式超时（CLAUDE.md 规矩）。
LIBRARIES_TIMEOUT = 30.0

# 排除规则的 type → i18n key（渲染徽章用）。
_EXCLUSION_BADGE_KEYS = {
    "album": "config.exclusions.badge.album",
    "path": "config.exclusions.badge.path",
    "tag": "config.exclusions.badge.tag",
    "reviewed": "common.reviewed",
}


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


def _exclusions(db):
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, type, value, enabled FROM exclusions ORDER BY id"
        ).fetchall()


def _validate(library_id: str, path_prefix: str, lang: str) -> list[str]:
    problems: list[str] = []
    if not library_id and not path_prefix:
        problems.append(t("config.scope.rule_required", lang))
    return problems


def render_page(request: Request, *, error: str | None = None, saved: bool = False):
    """渲染合并页。scope 与 exclusions 两套路由共用，保证永远同页。"""
    db = _db(request)
    lang = lang_from_cookie(request.cookies)
    libraries = _libraries(db)
    # 聚合去重所有库的 import_paths，供 path_prefix 的 datalist 提示
    # （未选库时展示全部；选中库后前端联动只展示该库的）。
    import_paths = sorted({p for lib in libraries for p in lib["paths"] if p})
    lib_paths = {lib["id"]: sorted(p for p in lib["paths"] if p) for lib in libraries}
    return request.app.state.templates.TemplateResponse(
        request,
        "config/scope.html",
        {
            "rules": _rules(db),
            "exclusions": _exclusions(db),
            "tags": load_tags(db),
            "badges": {k: t(v, lang) for k, v in _EXCLUSION_BADGE_KEYS.items()},
            "error": error,
            "saved": saved,
            "libraries": libraries,
            "import_paths": import_paths,
            "lib_paths": lib_paths,
            "backfill_active": has_active_job_of_type(db, "backfill"),
        },
    )


@router.get("/scope", response_class=HTMLResponse)
def scope_page(request: Request):
    return render_page(request)


# 排除规则页已合并进本页：旧 URL 保留重定向，老书签/文档链接不致死。
@router.get("/exclusions", response_class=RedirectResponse, include_in_schema=False)
def exclusions_legacy_redirect():
    return RedirectResponse(url="/config/scope", status_code=302)


@router.post("/scope", response_class=HTMLResponse)
async def scope_create(request: Request):
    lang = lang_from_cookie(request.cookies)
    form = await request.form()
    library_id = str(form.get("library_id", "")).strip()
    path_prefix = str(form.get("path_prefix", "")).strip()
    problems = _validate(library_id, path_prefix, lang)
    if problems:
        return render_page(request, error=t("common.semi_sep", lang).join(problems))
    insert_scope_rule(_db(request), library_id=library_id, path_prefix=path_prefix)
    return render_page(request, saved=True)


@router.post("/scope/{rule_id}/toggle", response_class=HTMLResponse)
def scope_toggle(request: Request, rule_id: int):
    with _db(request).connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM scope_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    if row is not None:
        set_scope_rule_enabled(_db(request), rule_id, not bool(row["enabled"]))
    return render_page(request)


@router.post("/scope/{rule_id}/delete", response_class=HTMLResponse)
def scope_delete(request: Request, rule_id: int):
    delete_scope_rule(_db(request), rule_id)
    return render_page(request)


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
