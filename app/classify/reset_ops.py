"""Revert confirmed system-owned changes in the dimensions selected by the user."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.classify.commit import reconcile_failed_effects
from app.classify.inverse import (
    pending_inverse_assets,
    reconcile_pending_inverse,
    reset_progress,
    run_inverse,
)
from app.classify.ownership import (
    ALBUM,
    METADATA,
    TAG,
    VISIBILITY,
    Change,
    effective_changes,
    matching_resource,
)
from app.db.connection import Database

_RESET_DIMENSIONS = frozenset({"tag", "classification", "rating", "description"})
_DIMENSION_ACTIONS = {
    "tag": TAG,
    "classification": (*VISIBILITY, *ALBUM),
    "rating": (METADATA[0],),
    "description": (METADATA[1],),
}
_RESET_ACTIONS = {
    "archive": "timeline",
    "tag_add": "tag_remove",
    "album_add": "album_remove",
    "rating_set": "rating_set",
    "description_set": "description_set",
}
_ACTION_PRIORITY = {action: i for i, action in enumerate(_RESET_ACTIONS)}


def _reset_plan(db: Database, dimensions: frozenset[str]) -> list[Change]:
    actions = tuple(action for dim in sorted(dimensions) for action in _DIMENSION_ACTIONS[dim])
    if not actions:
        return []
    changes: list[Change] = []
    for chain in effective_changes(db, actions=actions):
        last = chain[-1]
        if last.action not in _RESET_ACTIONS or last.payload.get("protected"):
            continue
        if last.kind in ("tag", "album") and (last.resource_id or last.name):
            changes.append(last)
        elif last.action in METADATA and {"old", "new"} <= last.payload.keys():
            original = chain[0].before
            if original != last.after:
                changes.append(replace(last, payload={**last.payload, "old": original}))
        elif last.action == "archive" and last.after == "archive":
            original = chain[0].before or "timeline"
            if original != last.after:
                changes.append(replace(last, payload={**last.payload,
                                                      "from_visibility": original}))
    return sorted(changes, key=lambda change: (_ACTION_PRIORITY[change.action], change.asset_id))


def reset_preview(db: Database, client) -> tuple[int, list[str]]:
    changes = _reset_plan(db, frozenset({"tag", "classification"}))
    archives = sum(change.action == "archive" for change in changes)
    tags = client.list_tags()
    names = {target[1] for change in changes if change.kind == "tag"
             if (target := matching_resource(change, tags))}
    return archives, sorted(names)


def run_reset_steps(
    db: Database,
    client,
    *,
    job_id: int | None,
    is_canceled: Callable[[], bool],
    dimensions: set[str] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    dims = _RESET_DIMENSIONS if dimensions is None else _RESET_DIMENSIONS & dimensions
    result = {"unarchived": 0, "untagged_tags": 0, "deleted_tags": 0,
              "removed_from_album": 0, "restored_metadata": 0, "skipped_conflicts": 0}
    reconcile_failed_effects(db, client)
    for asset_id in pending_inverse_assets(db, except_job_id=job_id):
        if is_canceled():
            return {**result, "canceled": True}
        reconcile_pending_inverse(db, client, asset_id, except_job_id=job_id)
    recorded = reset_progress(db, job_id) if job_id is not None else []
    last_status = {row["id"]: row["status"] for row in recorded}
    targets = {change.id: change for change in _reset_plan(db, dims)}
    # Preserve the exact before/after values if the prior attempt already started.
    targets.update({row["id"]: Change.from_row(row) for row in recorded})
    removed_tags: set[str] = set()
    removed_albums: set[str] = set()

    def count_applied(change: Change) -> None:
        if change.kind == "tag":
            removed_tags.add(change.resource_id or change.name)
            result["untagged_tags"] = len(removed_tags)
        elif change.kind == "album":
            removed_albums.add(change.resource_id or change.name)
            result["removed_from_album"] = len(removed_albums)
        else:
            field = "unarchived" if change.kind == "visibility" else "restored_metadata"
            result[field] += 1

    finished = 0
    for change in targets.values():
        status = last_status.get(change.id)
        if status == "applied":
            count_applied(change)
        elif status == "skipped":
            result["skipped_conflicts"] += 1
        else:
            continue
        finished += 1
    if on_progress:
        on_progress(finished, len(targets), result["skipped_conflicts"])

    for change in targets.values():
        if last_status.get(change.id) in ("applied", "skipped"):
            continue
        if is_canceled():
            result["canceled"] = True
            break
        change = replace(change, payload={**change.payload, "reset_step": True})
        direction = "apply" if change.kind in ("tag", "album") else "undo"
        if run_inverse(db, client, job_id or 0, change, direction):
            count_applied(change)
        else:
            result["skipped_conflicts"] += 1
        finished += 1
        if on_progress:
            on_progress(finished, len(targets), result["skipped_conflicts"])
    return result


def reset_preview_counts(db: Database) -> dict[str, int]:
    changes = _reset_plan(db, _RESET_DIMENSIONS)
    return {
        dim: len({change.asset_id for change in changes if change.action in actions})
        for dim, actions in _DIMENSION_ACTIONS.items()
    }
