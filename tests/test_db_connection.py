import sqlite3
from pathlib import Path

import pytest

from app.db.connection import Database


def test_connection_sets_wal_mode(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connection_sets_busy_timeout(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 10000


def test_every_new_connection_gets_busy_timeout(tmp_path: Path) -> None:
    """busy_timeout 是连接级 PRAGMA：第二个连接也必须带上。"""
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        conn.execute("SELECT 1")
    with db.connect() as conn2:
        assert conn2.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_rows_are_mapping_like(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        row = conn.execute("SELECT a, b FROM t").fetchone()
    assert row["a"] == 1
    assert row["b"] == "x"


def test_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "t.db"
    db = Database(target)
    with db.connect() as conn:
        conn.execute("SELECT 1")
    assert target.exists()


def test_connect_closes_on_exit(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_rollback_on_exception(tmp_path: Path) -> None:
    """异常时回滚，不留半写状态。Task 3 之后的所有写路径都依赖这条。"""
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 0


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    """Task 3 的外键约束测试依赖此 PRAGMA。"""
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_accepts_str_path(tmp_path: Path) -> None:
    target = tmp_path / "t.db"
    db = Database(str(target))
    with db.connect() as conn:
        conn.execute("SELECT 1")
    assert target.exists()


def test_commit_on_clean_exit_persists(tmp_path: Path) -> None:
    """干净退出必须提交。删掉 connect() 里的 commit()，这条会失败。

    **必须用 DML 验证**：DDL（CREATE TABLE）在 Python sqlite3 里会被隐式提交，
    用它做验证会掩盖 commit() 缺失。Tasks 4-7 的写入全是 DML，都依赖这条。
    """
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    with db.connect() as conn:
        conn.execute("INSERT INTO t VALUES (1)")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 1


def test_concurrent_connections(tmp_path: Path) -> None:
    """16 线程各自开连接写入，验证并发下无错误（M3 的真实负载）。"""
    import threading

    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            with db.connect() as conn:
                conn.execute("INSERT INTO t VALUES (?)", (n,))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 16
