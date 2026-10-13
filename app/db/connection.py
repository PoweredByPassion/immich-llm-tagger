"""SQLite 连接工厂。

两条 PRAGMA 的作用域不同，不能混为一谈：
- journal_mode=WAL 是**数据库级**属性，写进数据库文件，设一次即持久。
- busy_timeout 是**连接级**属性，只在当前连接生效；16 个 worker 各有独立连接，
  所以必须在每个新连接上设置，否则其余连接仍是 0 ms 超时。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BUSY_TIMEOUT_MS = 10_000


class Database:
    """按需新建连接。SQLite 连接开销很小，每次新建换来的是「PRAGMA 一定生效」。

    并发前提：**首次并发 connect() 之前，数据库必须已经是 WAL 模式**。
    在 fresh（delete 模式）库上，多个连接同时执行 `PRAGMA journal_mode = WAL`
    会间歇性抛 `database is locked`，且 `busy_timeout` 对此**无效**
    （SQLite 切换 journal mode 时不走 busy handler）。
    本项目由启动时的 `migrate()` 单线程打开并完成切换，因此多个并发 worker
    进入时库已是 WAL，天然满足该前提。**不要在没有这个前提的地方并发首连。**
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
