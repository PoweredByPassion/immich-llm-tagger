"""Persist the desired changes before issuing the first write to Immich."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.classify.effectors.base import (
    BUCKET_ALBUM,
    BUCKET_ASSET_PROPS,
    BUCKET_QUEUE,
    BUCKET_TAGS,
    Effect,
    Gates,
)
from app.classify.snapshot import AssetSnapshot
from app.clients.immich import ImmichError
from app.db.connection import Database
from app.store.events import log_event


@dataclass
class CommitResult:
    ok: bool
    failed_bucket: str | None = None
    error: str | None = None
    committed: list[Effect] = field(default_factory=list)
    conflict: bool = False


def _bucket(effect: Effect) -> str:
    return BUCKET_QUEUE if effect.action == "queue_remove" else effect.bucket


def _batches(effects: list[Effect]) -> list[list[Effect]]:
    props = [item for item in effects if item.bucket == BUCKET_ASSET_PROPS]
    result = [props] if props else []
    for bucket in (BUCKET_TAGS, BUCKET_ALBUM):
        result.extend([item] for item in effects if item.bucket == bucket)
    return result


def _pack(batches: list[list[Effect]]) -> str:
    return json.dumps([[(item.action, item.payload) for item in group] for group in batches],
                      ensure_ascii=False)


def _load(db, asset_id):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT job_id, batches, in_flight FROM pending_effects WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
    if row is None:
        return None
    batches = [[Effect(action, asset_id, payload) for action, payload in group]
               for group in json.loads(row["batches"])]
    return row["job_id"], batches, bool(row["in_flight"])


def _store(db, asset_id, job_id, batches, *, in_flight=False, analysis_record_id=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pending_effects(asset_id, job_id, batches, in_flight, analysis_record_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(asset_id) DO UPDATE SET job_id=excluded.job_id, "
            "batches=excluded.batches, in_flight=excluded.in_flight, "
            "analysis_record_id=COALESCE(excluded.analysis_record_id, pending_effects.analysis_record_id)",
            (asset_id, job_id, _pack(batches), int(in_flight), analysis_record_id),
        )


def pending_effects_record(db, asset_id):
    with db.connect() as conn:
        return conn.execute("SELECT job_id, analysis_record_id FROM pending_effects WHERE asset_id = ?",
                            (asset_id,)).fetchone()


def retire_pending_effects(db, client, asset_id, *, job_id=None):
    """Preserve confirmed actions from a superseded attempt and discard unfinished work."""
    pending = _load(db, asset_id)
    if pending is None:
        return
    original_job, batches, in_flight = pending
    if job_id is not None and original_job != job_id:
        return
    if batches and in_flight:
        try:
            applied, _, _ = _current_items(client, asset_id, batches[0])
        except ImmichError as exc:
            if exc.status_code != 404:
                raise
            applied = []
        _confirm(db, asset_id, original_job, applied, [])
    with db.connect() as conn:
        conn.execute("DELETE FROM pending_effects WHERE asset_id=?", (asset_id,))
        conn.execute("UPDATE asset_state SET process_status='failed', retry_after=NULL WHERE asset_id=?",
                     (asset_id,))
        conn.execute(
            "UPDATE asset_records SET apply_status='failed', apply_error='Pending changes superseded' "
            "WHERE asset_id=? AND apply_status='pending'", (asset_id,),
        )


def reconcile_failed_effects(db, client):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.asset_id FROM pending_effects p JOIN asset_state s USING(asset_id) "
            "WHERE s.process_status='failed' OR (p.job_id IS NULL AND p.analysis_record_id IS NULL)",
        ).fetchall()
    for row in rows:
        retire_pending_effects(db, client, row["asset_id"])


def _confirm(db, asset_id, job_id, applied, remaining):
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (?, ?, ?, 'apply', ?)",
            [(job_id, asset_id, item.action, json.dumps(item.payload, ensure_ascii=False))
             for item in applied],
        )
        conn.execute(
            "UPDATE pending_effects SET batches = ?, in_flight = 0 WHERE asset_id = ?",
            (_pack(remaining), asset_id),
        )


def _current_items(client, asset_id, items):
    """Read the outstanding batch after a lost response or an interrupted local write."""
    bucket = _bucket(items[0])
    if bucket == BUCKET_ALBUM:
        lookup = getattr(client, "list_asset_albums", None)
        if lookup is None:
            return [], items, False
        albums = {str(album.id) for album in lookup(asset_id)}
        applied = [item for item in items
                   if (item.payload["album_id"] in albums) == (item.action == "album_add")]
        return applied, [item for item in items if item not in applied], False

    lookup = getattr(client, "get_asset", None)
    if lookup is None:
        return [], items, False
    dto = lookup(asset_id)
    snapshot = AssetSnapshot.from_dto(dto)
    if bucket in (BUCKET_TAGS, BUCKET_QUEUE):
        applied = []
        tag_ids = {str(tag.id) for tag in (getattr(dto, "tags", None) or [])
                   if getattr(tag, "id", None) is not None}
        for item in items:
            name = item.payload.get("tag_name") or item.payload.get("queue_tag_name")
            present = item.payload["tag_id"] in tag_ids if tag_ids else name in snapshot.tags
            if present == (item.action == "tag_add"):
                applied.append(item)
        return applied, [item for item in items if item not in applied], False

    applied, retry = [], []
    conflict = False
    for item in items:
        payload = item.payload
        if item.action in ("archive", "timeline"):
            current, before, after = snapshot.visibility, payload["from_visibility"], item.action
        else:
            current = getattr(snapshot, item.action.removesuffix("_set"))
            before, after = payload["old"], payload["new"]
        if current == after:
            applied.append(item)
        elif current == before:
            retry.append(item)
        else:
            conflict = True
    return applied, retry, conflict


def _resolve(client, items, queue_tag_id):
    """Bind remote IDs before the write so recovery and undo refer to the same objects."""
    bucket = _bucket(items[0])
    if bucket == BUCKET_ASSET_PROPS:
        return items
    item = items[0]
    payload = dict(item.payload)
    if bucket == BUCKET_QUEUE:
        if "tag_id" not in payload:
            name = payload["queue_tag_name"]
            payload["tag_id"] = queue_tag_id or client.ensure_tags([name])[name]
    elif bucket == BUCKET_TAGS:
        if "tag_id" not in payload:
            name = payload["tag_name"]
            payload["tag_id"] = client.ensure_tags([name])[name]
    elif "album_id" not in payload:
        payload["album_id"] = client.ensure_album(payload["album_name"])
    return [Effect(item.action, item.asset_id, payload)]


def _apply(client, asset_id, items):
    item = items[0]
    payload = item.payload
    if item.action == "tag_add":
        client.tag_assets([payload["tag_id"]], [asset_id])
    elif item.action in ("tag_remove", "queue_remove"):
        client.untag_assets(payload["tag_id"], [asset_id])
    elif item.action == "album_add":
        client.add_to_album(payload["album_id"], [asset_id])
    elif item.action == "album_remove":
        client.remove_from_album(payload["album_id"], [asset_id])
    else:
        props = {}
        for item in items:
            if item.action in ("archive", "timeline"):
                props["visibility"] = item.action
            else:
                field = item.action.removesuffix("_set")
                props[field] = item.payload["new"]
        if "description" in props:
            props["description"] = props["description"] or ""
        client.set_asset_properties([asset_id], **props)


def _already_in_album(client, item):
    lookup = getattr(client, "list_asset_albums", None)
    return item.action == "album_add" and lookup is not None and any(
        str(album.id) == item.payload["album_id"] for album in lookup(item.asset_id)
    )


def commit_effects(db: Database, client, effects: list[Effect], *,
                   asset_id: str, job_id: int | None,
                   queue_tag_id: str | None, queue_tag_name: str,
                   gates: Gates, retain_completed: bool = False,
                   supersede: bool = False, analysis_record_id: int | None = None) -> CommitResult:
    if gates.dry_run:
        return CommitResult(ok=False, error="dry_run")

    pending = _load(db, asset_id)
    committed = []
    bucket = None
    try:
        if pending is not None:
            previous_job_id, batches, in_flight = pending
            if batches and in_flight:
                bucket = _bucket(batches[0][0])
                applied, retry, conflict = _current_items(client, asset_id, batches[0])
                batches = ([retry] if retry else []) + batches[1:]
                _confirm(db, asset_id, previous_job_id, applied, batches)
                committed.extend(applied)
                if conflict and not supersede:
                    return CommitResult(
                        ok=False, failed_bucket=bucket,
                        error="Immich changed after the attempted write. Review the current values before retrying.",
                        committed=committed, conflict=True,
                    )
        if pending is None or supersede:
            batches = _batches(effects)
            if queue_tag_id is not None:
                batches.append([Effect("queue_remove", asset_id,
                                       {"queue_tag_name": queue_tag_name, "tag_id": queue_tag_id})])
            _store(db, asset_id, job_id, batches, analysis_record_id=analysis_record_id)

        while batches:
            items, remaining = batches[0], batches[1:]
            bucket = _bucket(items[0])
            items = _resolve(client, items, queue_tag_id)
            if bucket == BUCKET_ALBUM and _already_in_album(client, items[0]):
                _confirm(db, asset_id, job_id, [], remaining)
            else:
                _store(db, asset_id, job_id, [items, *remaining], in_flight=True)
                _apply(client, asset_id, items)
                _confirm(db, asset_id, job_id, items, remaining)
                committed.extend(items)
            batches = remaining
        if not retain_completed:
            with db.connect() as conn:
                conn.execute("DELETE FROM pending_effects WHERE asset_id = ?", (asset_id,))
        return CommitResult(ok=True, committed=committed)
    except Exception as exc:
        log_event(db, "system", "apply_failed", {
            "asset_id": asset_id, "bucket": bucket, "error": str(exc),
        })
        return CommitResult(ok=False, failed_bucket=bucket, error=str(exc), committed=committed)
