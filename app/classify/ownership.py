"""Effective Immich changes derived from the ordered change journal."""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass

from app.classify.lease import claim, release
from app.classify.snapshot import _val, _vis_str
from app.db.connection import Database

TAG = ("tag_add", "tag_remove")
ALBUM = ("album_add", "album_remove")
QUEUE = ("queue_restore", "queue_remove")
VISIBILITY = ("archive", "timeline")
INVERSE_ACTIONS = {
    "tag_add": "tag_remove", "tag_remove": "tag_add",
    "queue_remove": "queue_restore", "queue_restore": "queue_remove",
    "archive": "timeline", "timeline": "archive",
    "album_add": "album_remove", "album_remove": "album_add",
    "rating_set": "rating_set", "description_set": "description_set",
}
METADATA = ("rating_set", "description_set")
MUTATION_LEASE_SECONDS = 15 * 60

_MEMBERSHIP = {
    **{action: ("tag", "tag_name", "tag_id") for action in TAG},
    **{action: ("album", "album_name", "album_id") for action in ALBUM},
    **{action: ("queue", "queue_tag_name", "tag_id") for action in QUEUE},
}


@dataclass(frozen=True)
class Change:
    id: int
    asset_id: str
    action: str
    direction: str
    payload: dict

    @classmethod
    def from_row(cls, row) -> Change:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        return cls(row["id"], row["asset_id"], row["action"], row["direction"],
                   payload if isinstance(payload, dict) else {})

    @property
    def kind(self) -> str:
        if self.action in _MEMBERSHIP:
            return _MEMBERSHIP[self.action][0]
        return "visibility" if self.action in VISIBILITY else self.action

    @property
    def name(self) -> str | None:
        return self.payload.get(_MEMBERSHIP[self.action][1]) if self.action in _MEMBERSHIP else None

    @property
    def resource_id(self) -> str | None:
        return self.payload.get(_MEMBERSHIP[self.action][2]) if self.action in _MEMBERSHIP else None

    @property
    def before(self):
        if self.action in VISIBILITY:
            return self.payload.get("from_visibility")
        if self.action in METADATA:
            return self.payload.get("old")
        return not self.after

    @property
    def after(self):
        if self.action in VISIBILITY:
            return self.payload.get("to_visibility", self.action)
        if self.action in METADATA:
            return self.payload.get("new")
        return self.action in ("tag_add", "album_add", "queue_restore")

    def concerns_same_field(self, other: Change) -> bool:
        if self.kind != other.kind:
            return False
        if self.kind not in ("tag", "album", "queue"):
            return True
        if self.resource_id and other.resource_id:
            return self.resource_id == other.resource_id
        return bool(self.name) and self.name == other.name


def effective_changes(db: Database, asset_id: str | None = None,
                      actions: tuple[str, ...] = ()) -> list[list[Change]]:
    """Return surviving chains of edits. An inverse cancels its source edit."""
    sql = "SELECT id, asset_id, action, direction, payload FROM change_log WHERE 1"
    params: list[str] = []
    if asset_id is not None:
        sql += " AND asset_id = ?"
        params.append(asset_id)
    if actions:
        sql += f" AND action IN ({','.join('?' for _ in actions)})"
        params.extend(actions)
    groups: dict[tuple[str, str], list[list[Change]]] = {}
    with db.connect() as conn:
        for row in conn.execute(sql + " ORDER BY id", params):
            change = Change.from_row(row)
            chains = groups.setdefault((change.asset_id, change.kind), [])
            matching = [chain for chain in chains
                        if chain and chain[-1].concerns_same_field(change)]
            # Legacy rows identify memberships by name. When the name is ambiguous,
            # keep the histories unrelated and let callers skip that legacy record.
            if len(matching) != 1:
                if change.direction == "apply":
                    chains.append([change])
                continue
            chain = matching[0]
            last = chain[-1]
            undo_of = change.payload.get("undo_of")
            if change.payload.get("reset"):
                chain.clear()
                if change.direction == "apply":
                    chain.append(change)
            elif change.direction == "undo":
                if (undo_of == last.id or (undo_of is None
                                          and change.before == last.after)):
                    chain.pop()
                else:
                    chain.clear()
            else:
                if change.before != last.after:
                    chain.clear()
                chain.append(change)
            if not chain:
                chains.remove(chain)
    return [chain for chains in groups.values() for chain in chains if chain]


def matching_resource(change: Change, candidates) -> tuple[str, str] | None:
    """Use identity when recorded. Legacy names are safe only if unambiguous."""
    name_key = "album_name" if change.kind == "album" else "name"
    matches = [(str(item.id), getattr(item, name_key)) for item in candidates
               if (str(item.id) == change.resource_id if change.resource_id
                   else getattr(item, name_key) == change.name)]
    return matches[0] if len(matches) == 1 else None


def current_value(asset, change: Change):
    if change.kind == "visibility":
        return _vis_str(_val(getattr(asset, "visibility", None)))
    field = change.action.removesuffix("_set")
    value = _val(getattr(getattr(asset, "exif_info", None), field, None))
    return (value or "") if field == "description" else value


def is_current(asset, change: Change) -> bool:
    expected = (change.after or "") if change.action == "description_set" else change.after
    return current_value(asset, change) == expected


def owned_memberships(db: Database, asset_id: str, actions: tuple[str, ...]) -> list[Change]:
    return [chain[-1] for chain in effective_changes(db, asset_id, actions)
            if chain[-1].after and not chain[-1].payload.get("protected")]


@contextmanager
def mutation_lease(db: Database, asset_id: str):
    """Coordinate short manual writes and undo with the automated classifier."""
    owner = secrets.randbits(63)
    acquired = claim(db, asset_id, owner, MUTATION_LEASE_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            release(db, asset_id, owner)


def _current_memberships(client, change: Change):
    if change.kind == "album":
        return client.list_asset_albums(change.asset_id)
    return client.get_asset(change.asset_id).tags or []


def _normalized_value(change: Change, value):
    return (value or "") if change.action == "description_set" else value


def prepare_inverse(client, change: Change) -> dict | None:
    """Confirm the current value and resolve the original target's identity."""
    payload = dict(change.payload)
    if change.kind in ("tag", "queue", "album"):
        album = change.kind == "album"
        member = matching_resource(change, _current_memberships(client, change))
        if (member is not None) != change.after:
            return None
        target = member or matching_resource(
            change, client.list_albums() if album else client.list_tags(),
        )
        if target is None:
            return None
        payload["album_id" if album else "tag_id"] = target[0]
        return payload

    if change.kind == "visibility" and not change.before:
        return None
    if change.action in METADATA and ("new" not in payload or "old" not in payload):
        return None
    asset = client.get_asset(change.asset_id)
    if not is_current(asset, change):
        return None
    if change.kind == "visibility":
        return {"from_visibility": change.after, "to_visibility": change.before}
    return {"old": change.after, "new": change.before}


def observe_inverse(client, change: Change, payload: dict) -> str:
    """Determine whether the durable intent still needs a write."""
    if change.kind in ("tag", "queue", "album"):
        resolved = Change(change.id, change.asset_id, change.action, change.direction, payload)
        present = matching_resource(resolved, _current_memberships(client, change)) is not None
        return "original" if present == change.after else "reverted"
    current = current_value(client.get_asset(change.asset_id), change)
    if current == _normalized_value(change, change.before):
        return "reverted"
    if current == _normalized_value(change, change.after):
        return "original"
    return "conflict"


def apply_inverse(client, change: Change, payload: dict) -> None:
    asset_ids = [change.asset_id]
    if change.kind == "album":
        operation = client.remove_from_album if change.after else client.add_to_album
        operation(payload["album_id"], asset_ids)
    elif change.kind in ("tag", "queue"):
        if change.after:
            client.untag_assets(payload["tag_id"], asset_ids)
        else:
            client.tag_assets([payload["tag_id"]], asset_ids)
    elif change.kind == "visibility":
        client.set_visibility(asset_ids, payload["to_visibility"])
    else:
        field = change.action.removesuffix("_set")
        client.set_asset_properties(asset_ids, **{field: _normalized_value(change, payload["new"])})


def update_after_inverse(conn, change: Change, job_id: int | None) -> None:
    if change.kind in ("album", "queue"):
        return
    fields: dict = {"last_job_id": job_id} if job_id is not None else {}
    if change.kind == "tag":
        row = conn.execute(
            "SELECT current_tag, current_markers FROM asset_state WHERE asset_id = ?",
            (change.asset_id,),
        ).fetchone()
        tag = row["current_tag"] if row else None
        markers = json.loads(row["current_markers"]) if row else []
        name = change.name
        if change.after:
            if tag == name:
                fields["current_tag"] = None
            if name in markers:
                fields["current_markers"] = json.dumps([m for m in markers if m != name])
        elif name:
            configured = conn.execute("SELECT kind FROM tags WHERE name = ?", (name,)).fetchone()
            kind = change.payload.get("tag_kind") or (configured["kind"] if configured else None)
            if kind == "marker" and name not in markers:
                fields["current_markers"] = json.dumps([*markers, name])
            elif kind == "primary" or (kind is None and tag is None):
                fields["current_tag"] = name
    elif change.kind == "visibility":
        fields["visibility"] = change.before
    else:
        fields[change.action.removesuffix("_set")] = change.before
    if fields:
        conn.execute(
            f"UPDATE asset_state SET {', '.join(key + ' = ?' for key in fields)}, "
            "updated_at = datetime('now') WHERE asset_id = ?",
            (*fields.values(), change.asset_id),
        )
