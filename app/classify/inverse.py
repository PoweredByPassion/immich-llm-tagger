"""Persist and reconcile the intent of each undo and reset write."""
from __future__ import annotations

import json

from app.classify.ownership import (
    INVERSE_ACTIONS,
    Change,
    apply_inverse,
    observe_inverse,
    prepare_inverse,
    update_after_inverse,
)
from app.clients.immich import ImmichError
from app.db.connection import Database

_STEP_SELECT = (
    "SELECT s.job_id, s.source_change AS id, s.asset_id, s.source_payload AS payload, "
    "s.inverse_payload, s.inverse_direction, s.status, c.action, c.direction "
    "FROM inverse_steps s JOIN change_log c ON c.id = s.source_change"
)


def _step(db: Database, job_id: int, change_id: int):
    with db.connect() as conn:
        return conn.execute(
            _STEP_SELECT + " WHERE s.job_id = ? AND s.source_change = ?",
            (job_id, change_id),
        ).fetchone()


def inverse_status(db: Database, job_id: int, change_id: int) -> str | None:
    row = _step(db, job_id, change_id)
    return row["status"] if row is not None else None


def inverse_progress(db: Database, job_id: int) -> tuple[int, int]:
    """Return the number of finished and skipped steps, including prior runs."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(CASE WHEN status='skipped' THEN 1 END) AS skipped "
            "FROM inverse_steps WHERE job_id=? AND status!='pending'", (job_id,),
        ).fetchone()
    return row["n"], row["skipped"]


def reset_progress(db: Database, job_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute(
            _STEP_SELECT + " WHERE s.job_id = ? ORDER BY s.source_change", (job_id,),
        )]


def pending_inverse_assets(db: Database, *, except_job_id: int | None) -> list[str]:
    with db.connect() as conn:
        return [row["asset_id"] for row in conn.execute(
            "SELECT DISTINCT asset_id FROM inverse_steps "
            "WHERE status='pending' AND job_id IS NOT ?", (except_job_id,),
        )]


def _store_pending(db: Database, job_id: int, change: Change, payload: dict,
                   direction: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO inverse_steps VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (job_id, change.id, change.asset_id, json.dumps(change.payload, ensure_ascii=False),
             json.dumps(payload, ensure_ascii=False), direction),
        )


def _record_terminal(db: Database, job_id: int, change: Change,
                     direction: str, *, payload: dict | None = None,
                     reason: str | None = None) -> None:
    with db.connect() as conn:
        if job_id:
            updated = conn.execute(
                "INSERT INTO inverse_steps VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (job_id, source_change) DO UPDATE SET status=excluded.status "
                "WHERE inverse_steps.status='pending'",
                (job_id, change.id, change.asset_id, json.dumps(change.payload, ensure_ascii=False),
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                 direction, "applied" if payload is not None else "skipped"),
            )
            if not updated.rowcount:
                return
        if payload is not None:
            _insert_change(conn, job_id, change, payload, direction)
        elif reason:
            event = "reset_change_skipped" if change.payload.get("reset_step") else "undo_change_skipped"
            conn.execute(
                "INSERT INTO events (source,event_type,payload) VALUES ('system', ?, ?)",
                (event, json.dumps({
                    "job_id": job_id or None, "asset_id": change.asset_id, "change_id": change.id,
                    "action": change.action, "reason": reason,
                }, ensure_ascii=False)),
            )


def _insert_change(conn, job_id, change, payload, direction) -> None:
    payload = dict(payload)
    if change.payload.get("reset_step"):
        if direction == "undo":
            payload["reset"] = True
    else:
        payload["undo_of"] = change.id
    payload.pop("reset_step", None)
    conn.execute(
        "INSERT INTO change_log (job_id, asset_id, action, direction, payload) VALUES (?, ?, ?, ?, ?)",
        (job_id or None, change.asset_id, INVERSE_ACTIONS[change.action],
         direction, json.dumps(payload, ensure_ascii=False)),
    )
    update_after_inverse(conn, change, job_id or None)


def skip_inverse(db: Database, job_id: int, change: Change, direction: str, reason: str) -> bool:
    _record_terminal(db, job_id, change, direction, reason=reason)
    return False


def run_inverse(
    db: Database, client, job_id: int, change: Change, direction: str,
) -> bool:
    step = _step(db, job_id, change.id) if job_id else None
    if step is not None:
        if step["status"] != "pending":
            return step["status"] == "applied"
        return _resume_inverse(db, client, step, replay=True)
    try:
        payload = prepare_inverse(client, change)
    except ImmichError as exc:
        if exc.status_code != 404:
            raise
        return skip_inverse(db, job_id, change, direction, "deleted")
    if payload is None:
        return skip_inverse(db, job_id, change, direction, "changed_or_missing")
    if job_id == 0:
        # Legacy direct invocation with no job has no stable retry identity.
        apply_inverse(client, change, payload)
        _record_terminal(db, job_id, change, direction, payload=payload)
        return True
    _store_pending(db, job_id, change, payload, direction)
    return _resume_inverse(db, client, _step(db, job_id, change.id), replay=False)


def _resume_inverse(db: Database, client, row, *, replay: bool, allow_write: bool = True) -> bool:
    job_id, direction = row["job_id"], row["inverse_direction"]
    change = Change.from_row(row)
    payload = json.loads(row["inverse_payload"])
    try:
        observed = observe_inverse(client, change, payload) if replay else "original"
        if observed == "conflict" or (observed == "original" and not allow_write):
            return skip_inverse(db, job_id, change, direction, "changed_or_missing")
        if observed == "original":
            apply_inverse(client, change, payload)
    except ImmichError as exc:
        if exc.status_code != 404:
            raise
        return skip_inverse(db, job_id, change, direction, "deleted")
    _record_terminal(db, job_id, change, direction, payload=payload)
    return True


def reconcile_pending_inverse(db: Database, client, asset_id: str,
                              *, except_job_id: int | None = None) -> None:
    """Confirm interrupted inverse writes before a newer task edits the asset."""
    with db.connect() as conn:
        steps = conn.execute(
            _STEP_SELECT + " WHERE s.asset_id = ? AND s.status = 'pending' ORDER BY s.source_change",
            (asset_id,),
        ).fetchall()
    for step in steps:
        if step["job_id"] != except_job_id:
            _resume_inverse(db, client, step, replay=True, allow_write=False)
