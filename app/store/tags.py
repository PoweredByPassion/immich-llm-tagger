"""Tag 配置投影与加载。

字段与 tags 表对应；load_tags 返回 id 升序的快照列表。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.db.connection import Database


@dataclass(frozen=True)
class TagDef:
    id: int
    name: str
    description: str
    kind: str  # "primary" | "marker"
    enabled: bool
    archive: bool
    album: str | None
    min_confidence: float | None
    skip_immich: bool = False  # 识别但不在 Immich 打标/进相册/归档


def load_tags(db: Database) -> list[TagDef]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, description, kind, enabled, archive, album, "
            "       min_confidence, skip_immich "
            "  FROM tags ORDER BY id"
        ).fetchall()
    return [
        TagDef(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            kind=row["kind"],
            enabled=bool(row["enabled"]),
            archive=bool(row["archive"]),
            album=row["album"],
            min_confidence=row["min_confidence"],
            skip_immich=bool(row["skip_immich"]),
        )
        for row in rows
    ]
