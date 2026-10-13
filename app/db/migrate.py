"""轻量迁移执行器。

迁移脚本按文件名 v{N}.sql 排序，只执行 version > 当前最大值的那些。
因此脚本本身**不需要幂等**：`schema_version` 已经保证只跑一次。
注意 SQLite 不支持 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。

版本记录由执行器统一插入（单一插入点，所有版本一致）。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.db.connection import Database

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILENAME_RE = re.compile(r"^v(\d+)\.sql$")


def _discover() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in MIGRATIONS_DIR.iterdir():
        m = _FILENAME_RE.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    return sorted(found)


def current_version(db: Database) -> int:
    with db.connect() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if not exists:
            return 0
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return row["v"] or 0


def _ensure_version_table(db: Database) -> None:
    """runner 自己保证 schema_version 存在。

    不依赖「第一个迁移脚本会创建它」—— 那是隐含契约，一旦 v1 被重写或拆分，
    migrate() 会在 INSERT 时以 `no such table: schema_version` 失败（难以定位）。
    """
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version    INTEGER PRIMARY KEY,"
            "    applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )


def migrate(db: Database) -> int:
    """执行所有未应用的迁移，返回执行后的版本号。

    每个迁移脚本与其版本记录在**同一个事务**里提交。这点很关键：
    `executescript` 自身会逐条隐式提交，若不显式包事务，脚本中途失败会留下
    「DDL 应用了一半、schema_version 没记录」的分裂状态 —— 下次启动重跑时
    先报 `table ... already exists`，把真正的语法错误掩盖掉，库卡死到手工修。
    """
    _ensure_version_table(db)
    version = current_version(db)
    for number, path in _discover():
        if number <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        with db.connect() as conn:
            # number 来自文件名的正则捕获组，是 int，拼接无注入风险
            conn.executescript(
                f"BEGIN;\n{sql}\n"
                f"INSERT OR IGNORE INTO schema_version (version) VALUES ({number});\n"
                f"COMMIT;"
            )
        version = number
    return version
