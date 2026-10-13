"""任务回滚（undo）核心逻辑。

读取目标 job 的 change_log（direction='apply'，只撤销正向应用），
按 id 逆序逐条撤销，并把撤销动作写回 change_log（direction='undo'）。
每条成功执行的步骤都写记录；404（对应关系已被用户手工删除）记 WARN 跳过。

一个 asset 的所有行撤销完后回写 asset_state：
current_tag / current_markers / visibility 按撤销后的实际状态更新；
manual_override / reviewed 等人工标记位不动。

逆操作映射：tag_add↔tag_remove, queue_remove↔queue_restore,
archive↔timeline, album_add↔album_remove。
"""
from __future__ import annotations

import json
import logging

from app.db.connection import Database

logger = logging.getLogger(__name__)

# 可见性类 action 撤销后 asset_state.visibility 的新值 = from_visibility
_VIS_ACTIONS = ("archive", "timeline")


class UndoSession:
    """单次 undo job 的会话：load_rows → 逐条 undo_row → flush_state。"""

    def __init__(self, db: Database, client, *, undo_job_id: int) -> None:
        self.db = db
        self.client = client
        self.undo_job_id = undo_job_id
        # 按 asset 聚合的撤销后状态缓冲，flush_state 时统一落库
        self._tag: dict[str, str | None] = {}
        self._markers: dict[str, list[str]] = {}
        self._visibility: dict[str, str | None] = {}

    # ------------------------------------------------------------ 读取

    def load_rows(self, target_job_id: int) -> list[dict]:
        """目标 job 的 apply change_log，id 逆序（后做的先撤）。"""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, asset_id, action, payload FROM change_log "
                "WHERE job_id = ? AND direction = 'apply' ORDER BY id DESC",
                (target_job_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                d["payload"] = {}
            out.append(d)
        return out

    def total(self, target_job_id: int) -> int:
        """进度 total = apply change_log 条数。"""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM change_log "
                "WHERE job_id = ? AND direction = 'apply'",
                (target_job_id,),
            ).fetchone()
        return row["n"]

    # ------------------------------------------------------------ 撤销

    def undo_row(self, row: dict) -> bool:
        """撤销一行。成功（含写 undo 记录）返回 True；404 跳过返回 False。"""
        action, asset_id, payload = row["action"], row["asset_id"], row["payload"]
        done = self._do_undo(action, asset_id, payload)
        if not done:
            return False
        self._record(row["id"], action, asset_id, payload)
        self._update_state_buffers(action, asset_id, payload)
        return True

    def _do_undo(self, action: str, asset_id: str, payload: dict) -> bool:
        """执行 Immich 侧逆操作。404/查无此名 → WARN 跳过，返回 False。"""
        if action in ("tag_add", "tag_remove"):
            name = payload.get("tag_name")
            tag_id = self._tag_id_by_name(name)
            if tag_id is None:
                logger.warning(
                    "undo: tag %r not found in Immich, skip (asset %s)", name, asset_id)
                return False
            # tag_add 的逆操作是摘；tag_remove 的逆操作是打
            if action == "tag_add":
                self.client.untag_assets(tag_id, [asset_id])
            else:
                self.client.tag_assets([tag_id], [asset_id])
            return True

        if action in _VIS_ACTIONS:
            # from_visibility 是变更前的值，恢复用它
            target = payload.get("from_visibility")
            if not target:
                logger.warning(
                    "undo: %s row missing from_visibility, skip (asset %s)",
                    action, asset_id)
                return False
            self.client.set_visibility([asset_id], target)
            return True

        if action in ("album_add", "album_remove"):
            name = payload.get("album_name")
            album_id = self.client.ensure_album(name)
            if action == "album_add":
                self.client.remove_from_album(album_id, [asset_id])
            else:
                self.client.add_to_album(album_id, [asset_id])
            return True

        if action in ("queue_remove", "queue_restore"):
            name = payload.get("queue_tag_name")
            tag_id = self.client.ensure_tags([name])[name]
            if action == "queue_remove":
                # 重新打队列标签
                self.client.tag_assets([tag_id], [asset_id])
            else:
                self.client.untag_assets(tag_id, [asset_id])
            return True

        raise ValueError(f"unknown change_log action: {action!r}")

    def _tag_id_by_name(self, name: str | None) -> str | None:
        if not name:
            return None
        for tag in self.client.list_tags():
            if tag.name == name:
                return str(tag.id)
        return None

    # ------------------------------------------------------------ 记录 / 状态

    def _record(self, source_row_id: int, action: str, asset_id: str,
                payload: dict) -> None:
        inverse = {
            "tag_add": "tag_remove", "tag_remove": "tag_add",
            "queue_remove": "queue_restore", "queue_restore": "queue_remove",
            "archive": "timeline", "timeline": "archive",
            "album_add": "album_remove", "album_remove": "album_add",
        }[action]
        undo_payload = dict(payload)
        if action in _VIS_ACTIONS:
            # 本步把可见性改成了 action 对应的值；撤销恢复为 from_visibility，
            # 因此 undo 记录（逆操作）的"变更前值" = apply 动作的目标值
            undo_payload = {"from_visibility": action}
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (?, ?, ?, 'undo', ?)",
                (self.undo_job_id, asset_id, inverse,
                 json.dumps(undo_payload, ensure_ascii=False)),
            )

    def _update_state_buffers(self, action: str, asset_id: str,
                              payload: dict) -> None:
        """撤销后状态缓冲：首次遇到该 asset 时从 asset_state 读当前值作基线。"""
        if asset_id not in self._markers:
            with self.db.connect() as conn:
                r = conn.execute(
                    "SELECT current_tag, current_markers, visibility "
                    "FROM asset_state WHERE asset_id = ?", (asset_id,),
                ).fetchone()
            self._tag[asset_id] = r["current_tag"] if r else None
            try:
                self._markers[asset_id] = (
                    json.loads(r["current_markers"]) if r else []
                )
            except (TypeError, ValueError):
                self._markers[asset_id] = []
            self._visibility[asset_id] = r["visibility"] if r else None

        if action in ("tag_add", "tag_remove"):
            name = payload.get("tag_name")
            if action == "tag_add":
                # 摘除：primary 与 marker 都可能在——主标签清 current_tag，
                # marker 从列表去掉
                if self._tag[asset_id] == name:
                    self._tag[asset_id] = None
                if name in self._markers[asset_id]:
                    self._markers[asset_id].remove(name)
            else:  # tag_remove 的撤销 = 重新打上
                if name in self._markers[asset_id]:
                    self._markers[asset_id].append(name)
                elif self._tag[asset_id] is None:
                    self._tag[asset_id] = name

        elif action in _VIS_ACTIONS:
            self._visibility[asset_id] = payload.get("from_visibility")

        # album_add/album_remove/queue_* 不进 asset_state

    def flush_state(self) -> None:
        """把缓冲的撤销后状态写回 asset_state（upsert；无 state 行则新建）。
        不触碰 manual_override / reviewed 等"人工痕迹"位。"""
        with self.db.connect() as conn:
            for asset_id, vis in self._visibility.items():
                conn.execute(
                    "INSERT INTO asset_state (asset_id, current_tag, "
                    "current_markers, visibility, last_job_id) "
                    "VALUES (:aid, :tag, :markers, :vis, :job_id) "
                    "ON CONFLICT(asset_id) DO UPDATE SET "
                    "  current_tag = excluded.current_tag, "
                    "  current_markers = excluded.current_markers, "
                    "  visibility = excluded.visibility, "
                    "  last_job_id = excluded.last_job_id, "
                    "  updated_at = datetime('now')",
                    {"aid": asset_id, "tag": self._tag[asset_id],
                     "markers": json.dumps(self._markers[asset_id]),
                     "vis": vis, "job_id": self.undo_job_id},
                )
