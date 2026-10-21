"""任务页：列表 / 详情 / 取消 / 全局暂停开关 / SSE 单 job 进度。

SSE 方案：持续推终态帧（简单方案）——每 1s 重查 get_job 推一帧，浏览器端
htmx sse-swap 局部刷新数字；终态后帧内容不变，客户端断开即结束。
不做"终态后主动关闭"（需要 generator 侧额外状态，收益小）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db.connection import Database
from app.store import jobs
from app.store import assets
from app.store.assets import query_records
from app.store.settings import SettingsStore
from app.web.sse import sse_response

router = APIRouter(prefix="/jobs")
# SSE 端点在根路径 /api/sse/job/{id}（与 overview 的 /api/sse/overview 对齐），
# 单独 router：FastAPI prefix 不能以 "/" 结尾区分，用无前缀 router 挂根路径。
sse_router = APIRouter()

_LIST_PAGE_SIZE = 10

_TYPE_BADGE = {
    "scan": "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    "single": "border-sky-500/40 bg-sky-500/10 text-sky-300",
    "dryrun": "border-amber-500/40 bg-amber-500/10 text-amber-300",
}
_STATUS_BADGE = {
    "queued": "border-zinc-600 bg-zinc-800 text-zinc-300",
    "running": "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    "done": "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    "failed": "border-red-500/40 bg-red-500/10 text-red-300",
    "canceled": "border-zinc-600 bg-zinc-800 text-zinc-400",
}


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    # SQLite datetime('now') 是 "YYYY-MM-DD HH:MM:SS"（UTC）
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _duration(job: jobs.JobRow) -> str:
    started = _ts(job.started_at)
    if started is None:
        return "-"
    end = _ts(job.finished_at) or datetime.utcnow()
    secs = max((end - started).total_seconds(), 0.0)
    return f"{secs:.0f}s"


def _rate(done: int, duration_secs: float) -> str:
    """速率 done/秒；耗时 <1s 或除零保护 → '-'。"""
    if done <= 0 or duration_secs < 1.0:
        return "-"
    return f"{done / duration_secs:.1f}/s"


def _eta(remaining: int, rate_per_sec: float | None) -> str:
    """remaining/速率；速率无效 → '-'。"""
    if rate_per_sec is None or rate_per_sec <= 0:
        return "-"
    return f"{timedelta(seconds=round(remaining / rate_per_sec))}"


def _job_ctx(job: jobs.JobRow) -> dict[str, Any]:
    """模板上下文：进度条百分比、耗时、速率、ETA、跳过数（scan 类）。"""
    ctx: dict[str, Any] = {"job": job}
    started = _ts(job.started_at)
    duration_secs = 0.0
    if started is not None:
        end = _ts(job.finished_at) or datetime.utcnow()
        duration_secs = max((end - started).total_seconds(), 0.0)
    ctx["duration"] = "-" if started is None else f"{duration_secs:.0f}s"
    ctx["progress_pct"] = (
        round(job.done / job.total * 100) if job.total > 0 else 0
    )
    rate_per_sec = None
    if job.done > 0 and duration_secs >= 1.0:
        rate_per_sec = job.done / duration_secs
    ctx["rate"] = _rate(job.done, duration_secs)
    remaining = max(job.total - job.done - job.failed, 0)
    ctx["eta"] = _eta(remaining, rate_per_sec)
    # 已完成跳过数：scan 里 total - done - failed 的「已处理被跳过」部分
    # （Immich tagIds 搜索索引 ~1h 延迟导致重复发现被跳过）。
    #
    # 只在 **scan 终态(done)** 展示：running 期间 total 是「扫描发现的候选数」，
    # total-done-failed 里还混着尚未拆出/尚未完成的待处理项，标成「已完成跳过」
    # 是错的（backfill 的 total 已剔除已完成，更无此语义）。终态时待处理为 0，
    # 差值才真正等于跳过数。
    ctx["skipped"] = (
        max(job.total - job.done - job.failed, 0)
        if job.type == "scan" and job.status == "done"
        else 0
    )
    return ctx


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def jobs_list(request: Request, status: str | None = None, type_: str | None = None,
              page: int = 1):
    db: Database = request.app.state.db
    total = jobs.count_jobs(db, status=status, type_=type_)
    pages = max(1, (total + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
    page = max(1, min(page, pages))
    rows = jobs.list_jobs(
        db, status=status, type_=type_,
        limit=_LIST_PAGE_SIZE, offset=(page - 1) * _LIST_PAGE_SIZE,
    )
    poll_enabled = bool(SettingsStore(db).get("poll_enabled"))
    return request.app.state.templates.TemplateResponse(
        request, "jobs/list.html",
        {
            "jobs": rows, "poll_enabled": poll_enabled, "filter_status": status,
            "filter_type": type_, "total": total, "page": page, "pages": pages,
            "has_prev": page > 1, "has_next": page < pages,
        },
    )


@router.post("/pause", response_class=HTMLResponse, include_in_schema=False)
async def jobs_pause(request: Request):
    db: Database = request.app.state.db
    form = await request.form()
    # 带 pause=1 → 暂停；空表单 → 恢复
    SettingsStore(db).set("poll_enabled", not bool(form.get("pause")))
    return jobs_list(request)


@router.post("/scan", include_in_schema=False)
def jobs_scan(request: Request):
    """顶栏"触发扫描"：入队 manual scan job（active 已存在则去重），回任务列表。"""
    db: Database = request.app.state.db
    if not jobs.has_active_job_of_type(db, "scan"):
        jobs.create_job(db, "scan", params={"source": "manual"})
    return RedirectResponse("/jobs", status_code=303)


@sse_router.get("/api/sse/job/{job_id}", include_in_schema=False)
def job_sse(request: Request, job_id: int):
    db: Database = request.app.state.db

    def fetch() -> dict:
        job = jobs.get_job(Database(db.path), job_id)
        if job is None:
            return {"id": job_id, "status": "not_found", "done": 0, "total": 0, "failed": 0}
        return {
            "id": job.id, "status": job.status, "done": job.done,
            "total": job.total, "failed": job.failed,
        }

    return sse_response(fetch, interval=1.0)


@router.get("/{job_id}", response_class=HTMLResponse, include_in_schema=False)
def jobs_detail(request: Request, job_id: int):
    db: Database = request.app.state.db
    job = jobs.get_job(db, job_id)
    if job is None:
        return request.app.state.templates.TemplateResponse(
            request, "jobs/notfound.html", {"job_id": job_id}, status_code=200
        )
    failures = query_records(db, job_id=job_id, status="error", limit=100,
                             include_unapplied=True)
    ctx: dict[str, Any] = {**_job_ctx(job), "failures": failures, "params_json": json.dumps(
        job.params, ensure_ascii=False, indent=2)}
    # 回滚：done/canceled 且有 apply change_log 才可回滚；
    # undo job 自身不渲染按钮；已有活动 undo 时置灰提示。
    ctx["can_undo"] = _undoable(db, job)
    if ctx["can_undo"]:
        n, n_assets = assets.count_changes_for_job(db, job_id)
        ctx["undo_changes"] = n
        ctx["undo_assets"] = n_assets
        ctx["undo_busy"] = jobs.has_active_job_of_type(db, "undo")
    if job.type == "undo":
        ctx["undo_log"] = assets.changes_for_job(db, job_id)
        target = job.params.get("target_job_id")
        ctx["undo_target"] = target
        ctx["target_job"] = jobs.get_job(db, target) if target else None
    return request.app.state.templates.TemplateResponse(
        request, "jobs/detail.html", ctx
    )


@router.post("/{job_id}/cancel", response_class=HTMLResponse, include_in_schema=False)
def jobs_cancel(request: Request, job_id: int):
    db: Database = request.app.state.db
    jobs.cancel_job(db, job_id)
    return jobs_detail(request, job_id)


# ------------------------------------------------------------- 回滚

_UNDOABLE_STATUS = ("done", "canceled")


def _undoable(db: Database, job: jobs.JobRow | None) -> bool:
    """可回滚判定（页面 can_undo 与 POST 端点共用单一来源）。"""
    return (
        job is not None
        and job.status in _UNDOABLE_STATUS
        and job.type != "undo"
        and assets.count_changes_for_job(db, job.id)[0] > 0
    )


@router.post("/{job_id}/undo", response_class=HTMLResponse, include_in_schema=False)
def jobs_undo(request: Request, job_id: int):
    db: Database = request.app.state.db
    if not _undoable(db, jobs.get_job(db, job_id)):
        raise HTTPException(status_code=404)
    new_id = jobs.create_job(
        db, "undo", params={"target_job_id": job_id}, prompt_version_id=None
    )
    return RedirectResponse(f"/jobs/{new_id}", status_code=303)
