"""GET /api/health —— 进程存活 + DB 状态探测。

db_writable 为 False 时仍返回 200：健康检查的职责是「如实上报」，
DB 故障应能在响应体里看到，而不是让探针收到 500。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.db.connection import Database

router = APIRouter(prefix="/api")


@router.get("/health")
def health(request: Request) -> dict:
    db: Database = request.app.state.db
    schema_version = 0
    db_writable = False
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if row is not None and row["v"] is not None:
                schema_version = row["v"]
            # trivial write-capable probe
            conn.execute("CREATE TABLE IF NOT EXISTS health_probe (id INTEGER PRIMARY KEY)")
            conn.execute("DELETE FROM health_probe")
            conn.execute("INSERT INTO health_probe (id) VALUES (1)")
            db_writable = True
    except Exception:
        pass
    return {"status": "ok", "schema_version": schema_version, "db_writable": db_writable}
