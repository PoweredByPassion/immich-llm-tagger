"""Liveness and local task-queue readiness probes."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request, Response

from app.db.connection import Database

router = APIRouter(prefix="/api")


@router.get("/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health")
def health(request: Request, response: Response) -> dict:
    db: Database = request.app.state.db
    schema_version = 0
    db_writable = False
    try:
        with db.connect() as conn:
            conn.execute("PRAGMA busy_timeout = 1000")
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            schema_version = row["v"] or 0
            if schema_version:
                # Acquire a write transaction without adding or changing any data.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE schema_version SET version = version WHERE 0")
                conn.rollback()
                db_writable = True
    except (sqlite3.Error, OSError):
        pass
    if not db_writable:
        response.status_code = 503
    return {
        "status": "ok" if db_writable else "unavailable",
        "schema_version": schema_version,
        "db_writable": db_writable,
    }
