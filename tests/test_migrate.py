import sqlite3
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import MIGRATIONS_DIR, _discover, current_version, migrate


def test_migrate_creates_schema_version_table(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_migrate_creates_all_config_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    migrate(db)
    expected = {
        "settings", "immich_conn", "llm_conn", "scope_rules", "tags",
        "prompt_versions", "exclusions", "marker_rules",
        "jobs", "asset_records", "asset_state", "change_log", "events",
    }
    with db.connect() as conn:
        found = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # 索引也是 schema 的一部分：性能关键，掉一个不容易被发现
        indexes = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert expected <= found
    assert {
        "idx_asset_records_asset", "idx_asset_records_job",
        "idx_asset_state_lease", "idx_change_log_job", "idx_events_ts",
    } <= indexes


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """重复调用不应重复执行迁移，也不应报错。"""
    db = Database(tmp_path / "t.db")
    migrate(db)
    migrate(db)
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    assert count == 11


def test_current_version_zero_when_fresh(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with db.connect() as conn:
        conn.execute("SELECT 1")
    assert current_version(db) == 0


def test_tags_kind_check_constraint(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    migrate(db)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO tags (name, kind) VALUES ('x', 'bogus')"
            )


def test_v8_adds_skip_immich_column(tmp_path: Path) -> None:
    """v8 给 tags 加 skip_immich（识别但不写 Immich）。"""
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tags)")}
    assert "skip_immich" in cols


def test_v7_drops_categories_table(tmp_path: Path) -> None:
    """v7 删除 categories（死功能，从未接入管线）。新库建后删，旧库直接删。"""
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        found = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "categories" not in found


def test_migrations_dir_contains_v1() -> None:
    assert (MIGRATIONS_DIR / "v1.sql").exists()


def test_migrate_leaves_db_in_wal_mode(tmp_path: Path) -> None:
    """M3 的 16 个 worker 依赖「启动时单线程 migrate() 先把库切到 WAL」。

    fresh 库上并发首连会间歇抛 `database is locked`，且 busy_timeout 对此无效
    （SQLite 切换 journal mode 时不走 busy handler）。所以这个顺序是硬前提，
    必须钉住 —— 否则 M3 的并发安全只建立在注释上。

    边界：本测试只证明「migrate() 跑完之后库是 WAL」，不证明启动顺序。
    """
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_discover_sorts_numerically_and_ignores_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按数值排序（不是字典序），且忽略非 v{N}.sql 文件。

    v10 与 v2 用来区分数值序和字典序：字典序会把 v10 排在 v2 前面。
    排序在只有一个迁移文件时是空操作，所以必须用多文件才钉得住。

    目录遍历顺序是未定义的（Linux readdir 是哈希序，实测同一批文件在不同机器/
    不同名字上顺序不同），所以这里**显式**打乱 iterdir 的产出顺序：否则删掉
    `sorted()` 后可能碰巧仍得到 [1, 2, 10]，测试沦为假绿灯。
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in ("v10.sql", "v2.sql", "v1.sql", "README.md", "notes.txt"):
        (migrations / name).write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr("app.db.migrate.MIGRATIONS_DIR", migrations)

    real_iterdir = Path.iterdir
    scrambled = [
        migrations / "v10.sql",
        migrations / "README.md",
        migrations / "v2.sql",
        migrations / "notes.txt",
        migrations / "v1.sql",
    ]
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda self: iter(scrambled) if self == migrations else real_iterdir(self),
    )

    assert [number for number, _ in _discover()] == [1, 2, 10]


def test_non_idempotent_migration_runs_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """证明「脚本无需幂等，靠 schema_version 保证只跑一次」这条承诺成立。

    v2 故意非幂等（ALTER TABLE ADD COLUMN 不带 IF NOT EXISTS）。
    若 `if number <= version: continue` 守卫失效，第二次 migrate() 会重复执行并抛错
    —— 这正是将来 v2 出现非幂等 DDL 时的真实故障。

    注意：**不能**用 `v1.sql` 做这个验证。它的 DDL 全是 IF NOT EXISTS、INSERT 是
    OR IGNORE，天然幂等，重复执行不报错，会让这条测试变成假绿灯。
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # 显式建 schema_version：让本测试**只**因「跳过守卫失效」而失败，
    # 不与 _ensure_version_table 的回归耦合（那由专门的 bootstrap 测试负责）。
    # v1 保持幂等（IF NOT EXISTS），好让守卫失效时的报错落在 v2 的 ALTER 上。
    (migrations / "v1.sql").write_text(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "    version INTEGER PRIMARY KEY,"
        "    applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ");"
        "CREATE TABLE IF NOT EXISTS a (x INTEGER);",
        encoding="utf-8",
    )
    (migrations / "v2.sql").write_text(
        "ALTER TABLE a ADD COLUMN y INTEGER;", encoding="utf-8"
    )
    monkeypatch.setattr("app.db.migrate.MIGRATIONS_DIR", migrations)

    db = Database(tmp_path / "t.db")
    migrate(db)
    assert current_version(db) == 2

    migrate(db)  # 必须是无操作，而不是重复执行
    assert current_version(db) == 2

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(a)")}
    assert columns == {"x", "y"}


def test_migrate_bootstraps_version_table_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runner 必须自己建 schema_version，不能依赖第一个迁移脚本建它。

    否则一旦 v1 被重写/拆分，migrate() 会在 INSERT 时以
    `no such table: schema_version` 失败 —— 一个难以定位的隐含契约。
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "v1.sql").write_text(
        "CREATE TABLE a (x INTEGER);", encoding="utf-8"
    )
    monkeypatch.setattr("app.db.migrate.MIGRATIONS_DIR", migrations)

    db = Database(tmp_path / "t.db")
    assert migrate(db) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM a").fetchone()["c"] == 0


def test_lease_expires_at_format_is_enforced(tmp_path: Path) -> None:
    """租约时间戳必须是 'YYYY-MM-DD HH:MM:SS'，不能是 isoformat() 的 'T' 形式。

    lease_expires_at 与 datetime('now') 做**字符串**比较。'T' 分隔虽表示同一时刻，
    但字典序会排到后面 → 租约永不过期 → worker 崩溃后卡住的图片永不回收（静默故障）。
    CHECK 把它变成硬约束。
    """
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        conn.execute("INSERT INTO asset_state (asset_id) VALUES ('a')")

    # 正确格式（SQLite datetime 产生的空格分隔形式）：接受
    with db.connect() as conn:
        conn.execute(
            "UPDATE asset_state SET lease_expires_at = datetime('now', '+5 minutes') "
            "WHERE asset_id = 'a'"
        )

    # isoformat() 的 'T' 形式：必须被拒绝
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "UPDATE asset_state SET lease_expires_at = '2026-10-10T12:00:00' "
                "WHERE asset_id = 'a'"
            )


def test_failed_migration_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移脚本中途失败必须**整体回滚**且不记录版本。

    否则会留下「DDL 应用了一半、schema_version 没记录」的分裂状态：
    下次启动重跑时先报 `table ... already exists`，把真正的语法错误掩盖掉，
    库里残留半应用的 schema。这正是执行器把 DDL 与版本插入包进同一事务的原因。
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "v1.sql").write_text(
        "CREATE TABLE good (x INTEGER);\n"
        "THIS IS NOT VALID SQL;\n"
        "CREATE TABLE never (y INTEGER);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.db.migrate.MIGRATIONS_DIR", migrations)

    db = Database(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)

    with db.connect() as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "good" not in tables
    assert "never" not in tables
    assert current_version(db) == 0


def test_v6_partial_index_serves_webhook_dedup_query(tmp_path: Path) -> None:
    """v6 表达式 partial index：webhook 去重查询不再全表 SCAN。"""
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT 1 FROM jobs WHERE type = 'single' "
            "AND status IN ('queued', 'running') "
            "AND json_extract(params, '$.asset_id') = ? LIMIT 1",
            ("x",),
        ).fetchall()
    details = " ".join(r["detail"] for r in plan)
    assert "SCAN jobs" not in details
    assert "USING INDEX" in details


def test_v9_allows_classify_item_type(tmp_path):
    """v9 表重建后 jobs.type 接受 classify_item（scan 拆分的子任务类型）。"""
    db = Database(tmp_path / "t.db")
    version = migrate(db)
    assert version >= 9
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO jobs (type, status, params) "
            "VALUES ('classify_item', 'queued', '{}')"
        )  # v1 CHECK 下会抛 IntegrityError


def _seed_v8_db(tmp_path: Path) -> Database:
    """建一个停在 v8 的库：直接跑 v1..v8 脚本并登记版本号。

    用真实脚本而非手写 DDL，保证 seed 出的库与线上 v8 形态一致；
    migrate() 之后即可单独验证 v9 的表重建（同 test_old_v1_db 的思路）。
    """
    db = Database(tmp_path / "v8.db")
    with db.connect() as conn:
        for n in range(1, 9):
            conn.executescript(
                (MIGRATIONS_DIR / f"v{n}.sql").read_text(encoding="utf-8")
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version    INTEGER PRIMARY KEY,"
            "    applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        for n in range(1, 9):
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (n,)
            )
    assert current_version(db) == 8
    return db


def test_v9_rebuild_preserves_rows_and_columns(tmp_path: Path) -> None:
    """v9 表重建（建新表→拷贝→drop→rename）必须逐列保真。

    空库上 INSERT ... SELECT 拷贝 0 行，列顺序写错也测不出来；这里造一个带
    真实行的 v8 库，断言升级后每个字段（含 v2 的 cancel_requested）原样保留。
    """
    db = _seed_v8_db(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO jobs (type, status, total, done, failed, params, "
            "prompt_version_id, started_at, finished_at, error, cancel_requested) "
            "VALUES ('scan','running',10,3,1,'{\"a\":1}',7,"
            "'2026-01-01 00:00:00',NULL,'boom',1)"
        )
        conn.execute("INSERT INTO jobs (type, status) VALUES ('single','queued')")
        conn.execute("DELETE FROM jobs WHERE id = 2")   # 制造 id 空洞

    assert migrate(db) == 11

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
        assert row["type"] == "scan" and row["status"] == "running"
        assert (row["total"], row["done"], row["failed"]) == (10, 3, 1)
        assert row["params"] == '{"a":1}'
        assert row["prompt_version_id"] == 7
        assert row["started_at"] == "2026-01-01 00:00:00"
        assert row["finished_at"] is None
        assert row["error"] == "boom"
        assert row["cancel_requested"] == 1
        # 计数器不降：下一个 id 续在 2 之后，不复用 2
        conn.execute("INSERT INTO jobs (type, status) VALUES ('scan','queued')")
        assert conn.execute("SELECT MAX(id) AS m FROM jobs").fetchone()["m"] == 3


def test_v9_preserves_autoincrement_counter_when_jobs_empty(tmp_path: Path) -> None:
    """升级时 jobs 表为空（record_retention_days 已清光终态 job）时，
    AUTOINCREMENT 计数器仍须保留，否则新 job 复用 id 1 → change_log 里旧 job
    的资产会被新 job 的 undo 误伤（v9 之前 AUTOINCREMENT 杜绝了这种复用）。"""
    db = _seed_v8_db(tmp_path)
    with db.connect() as conn:
        for _ in range(3):
            conn.execute("INSERT INTO jobs (type, status) VALUES ('scan','done')")
        conn.execute("DELETE FROM jobs")              # 计数器停在 3，表为空

    assert migrate(db) == 11

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
        conn.execute("INSERT INTO jobs (type, status) VALUES ('scan','queued')")
        new_id = conn.execute("SELECT MAX(id) AS m FROM jobs").fetchone()["m"]
    assert new_id == 4                                # 不复用 1


def test_v10_drops_llm_concurrency_column(tmp_path: Path) -> None:
    """v10：删除死配置列 llm_conn.concurrency，其余列原样保留。

    fresh 库会先经 v1 建出该列、再被 v10 DROP，等价于升级已有库的路径。
    """
    db = Database(tmp_path / "t.db")
    assert migrate(db) == 11
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_conn)")}
    assert "concurrency" not in cols
    assert {"base_url", "api_key", "model", "timeout", "max_tokens",
            "multimodal"} <= cols


def _seed_v10_db(tmp_path: Path) -> Database:
    """造一个停在 v10 的库（跑 v1..v10 脚本 + 登记版本号），
    供 v11 的「升级既有库」断言使用。"""
    db = Database(tmp_path / "v10.db")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version    INTEGER PRIMARY KEY,"
            "    applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        for n in range(1, 11):
            conn.executescript(
                (MIGRATIONS_DIR / f"v{n}.sql").read_text(encoding="utf-8")
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (n,)
            )
    assert current_version(db) == 10
    return db


def test_v11_adds_columns_and_widens_change_log(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    assert migrate(db) == 11
    with db.connect() as conn:
        cols_r = {r["name"] for r in conn.execute("PRAGMA table_info(asset_records)")}
        cols_s = {r["name"] for r in conn.execute("PRAGMA table_info(asset_state)")}
        assert {"rating", "description"} <= cols_r
        assert {"rating", "description", "apply_attempts"} <= cols_s
        # 枚举已扩容：写不进去就说明 CHECK 没重建
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (NULL, 'a1', 'rating_set', 'apply', '{\"old\": null, \"new\": 4}')"
        )


def test_v11_backfills_applied_at_only_for_applied_rows(tmp_path: Path) -> None:
    """§6.2：不回填 → 改口径后整库历史资产被判「未完成」→ 全量重跑。"""
    db = _seed_v10_db(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, created_at) "
            "VALUES ('ok-live','poll','ok',0,'2026-01-01 00:00:00'),"
            "       ('ok-dry','poll','ok',1,'2026-01-01 00:00:00'),"
            "       ('err','poll','error',0,'2026-01-01 00:00:00')"
        )

    assert migrate(db) == 11

    with db.connect() as conn:
        got = {r["asset_id"]: r["applied_at"] for r in conn.execute(
            "SELECT asset_id, applied_at FROM asset_records")}
    assert got["ok-live"] == "2026-01-01 00:00:00"   # 回填成 created_at
    assert got["ok-dry"] is None                     # dry_run=1 不算应用过
    assert got["err"] is None                        # status != 'ok'


def test_v11_change_log_rebuild_keeps_autoincrement(tmp_path: Path) -> None:
    """表重建（v9 模式）会连 sqlite_sequence 行一起 DROP 掉。

    再造「有行 → 删光」的空表状态：seq 归零则新行复用旧 id，
    change_log 的 id 是 undo 的定位依据，复用即误伤。"""
    db = _seed_v10_db(tmp_path)
    with db.connect() as conn:
        for _ in range(2):
            conn.execute("INSERT INTO change_log (asset_id, action, direction, payload) "
                         "VALUES ('a1','tag_add','apply','{}')")
        conn.execute("DELETE FROM change_log")        # 表空，seq 停在 2

    assert migrate(db) == 11

    with db.connect() as conn:
        conn.execute("INSERT INTO change_log (asset_id, action, direction, payload) "
                     "VALUES ('a2','rating_set','apply','{}')")
        assert conn.execute(
            "SELECT MAX(id) AS m FROM change_log").fetchone()["m"] == 3


def test_v11_recreates_change_log_job_index(tmp_path: Path) -> None:
    """DROP TABLE 会把索引一起删掉，必须重建（idx_change_log_job）。"""
    db = _seed_v10_db(tmp_path)
    migrate(db)
    with db.connect() as conn:
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_change_log_job" in idx


def test_v11_adds_change_log_asset_action_index(tmp_path: Path) -> None:
    """v11 重建 change_log 时补上 (asset_id, action) 覆盖索引。

    `has_system_archive` 与 `last_written_value`（后者每张图分类调两次）都按
    `asset_id`（+action）过滤，而 change_log 刻意永不清理 —— 没这个索引这三个
    热查询每次都全表 SCAN change_log，随库增长线性劣化。
    """
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_change_log_asset_action" in idx
