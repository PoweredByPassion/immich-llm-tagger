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
    # 是否把评分 / 描述写进 Immich（v12）。按标签控制，取代原来的
    # enabled_effects 全局开关；默认 True = 升级后行为不变。
    write_rating: bool = True
    write_description: bool = True


def load_tags(db: Database) -> list[TagDef]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, description, kind, enabled, archive, album, "
            "       min_confidence, skip_immich, write_rating, write_description "
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
            write_rating=bool(row["write_rating"]),
            write_description=bool(row["write_description"]),
        )
        for row in rows
    ]
