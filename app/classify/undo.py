"""Undo individual Immich changes while preserving later edits."""
from __future__ import annotations

from app.classify.commit import retire_pending_effects
from app.classify.inverse import (
    inverse_progress,
    inverse_status,
    reconcile_pending_inverse,
    run_inverse,
    skip_inverse,
)
from app.classify.ownership import (
    INVERSE_ACTIONS,
    Change,
    effective_changes,
    mutation_lease,
)
from app.db.connection import Database


class UndoSession:
    def __init__(self, db: Database, client, *, undo_job_id: int) -> None:
        self.db = db
        self.client = client
        self.undo_job_id = undo_job_id

    @property
    def conflicts(self) -> int:
        return self.progress()[1]

    def progress(self) -> tuple[int, int]:
        return inverse_progress(self.db, self.undo_job_id)

    def prepare(self, target_job_id: int) -> None:
        """Capture any successful outstanding writes before loading the journal."""
        with self.db.connect() as conn:
            assets = [row["asset_id"] for row in conn.execute(
                "SELECT asset_id FROM pending_effects WHERE job_id = ?", (target_job_id,),
            )]
        for asset_id in assets:
            with mutation_lease(self.db, asset_id) as acquired:
                if acquired:
                    retire_pending_effects(self.db, self.client, asset_id, job_id=target_job_id)

    def load_rows(self, target_job_id: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT c.id, c.job_id, c.asset_id, c.action, c.direction, c.payload FROM change_log c "
                "LEFT JOIN inverse_steps s ON s.source_change=c.id AND s.job_id=? "
                "WHERE c.job_id=? AND c.direction='apply' "
                "AND (s.status IS NULL OR s.status='pending') ORDER BY c.id DESC",
                (self.undo_job_id, target_job_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def total(self, target_job_id: int) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM change_log "
                "WHERE job_id = ? AND direction = 'apply'",
                (target_job_id,),
            ).fetchone()
        return row["n"]

    def undo_row(self, row: dict) -> bool:
        change = Change.from_row(row)
        with mutation_lease(self.db, change.asset_id) as acquired:
            status = inverse_status(self.db, self.undo_job_id, change.id)
            if not acquired:
                if status != "pending":
                    return skip_inverse(self.db, self.undo_job_id, change, "undo", "asset_busy")
                return False
            if status is None:
                reconcile_pending_inverse(self.db, self.client, change.asset_id)
                latest = effective_changes(
                    self.db, change.asset_id, (INVERSE_ACTIONS[change.action], change.action),
                )
                if not any(chain[-1].id == change.id for chain in latest):
                    return skip_inverse(self.db, self.undo_job_id, change, "undo", "superseded")
            return run_inverse(self.db, self.client, self.undo_job_id, change, "undo")
