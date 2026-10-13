"""用量统计页（GET /usage）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db.connection import Database
from app.store import usage

router = APIRouter()


def _existing_job_ids(db: Database, by_job_rows: list[dict]) -> set[int]:
    """by_job 里真实存在于 jobs 表的 job_id 集合（retention 清理后软引用过期）。"""
    ids = {r["job_id"] for r in by_job_rows if r["job_id"] is not None}
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM jobs WHERE id IN ({placeholders})", tuple(ids)
        ).fetchall()
    return {r["id"] for r in rows}


@router.get("/usage", response_class=HTMLResponse, include_in_schema=False)
def usage_page(request: Request):
    db: Database = request.app.state.db
    payload = {
        "totals": usage.totals(db),
        "by_day": usage.by_day(db),
        "by_model": usage.by_model(db),
        "by_job": usage.by_job(db),
        "existing_job_ids": _existing_job_ids(db, usage.by_job(db)),
    }
    return request.app.state.templates.TemplateResponse(
        request, "usage.html", payload
    )
