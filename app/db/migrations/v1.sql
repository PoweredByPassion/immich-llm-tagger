-- 初始 schema。
-- 注意：SQLite 不支持 ALTER TABLE ... ADD COLUMN IF NOT EXISTS，
-- 迁移靠 schema_version 保证只跑一次，脚本本身无需幂等。

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- 配置

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS immich_conn (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    url        TEXT    NOT NULL DEFAULT '',
    api_key    TEXT    NOT NULL DEFAULT '',
    verify_ssl INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS llm_conn (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    base_url    TEXT    NOT NULL DEFAULT '',
    api_key     TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    concurrency INTEGER NOT NULL DEFAULT 16,
    timeout     INTEGER NOT NULL DEFAULT 180,
    max_tokens  INTEGER NOT NULL DEFAULT 128000
);

CREATE TABLE IF NOT EXISTS scope_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id  TEXT    NOT NULL DEFAULT '',
    path_prefix TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tags (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,
    description    TEXT    NOT NULL DEFAULT '',
    kind           TEXT    NOT NULL CHECK (kind IN ('primary', 'marker')),
    archive        INTEGER NOT NULL DEFAULT 0,
    album          TEXT,
    min_confidence REAL,
    enabled        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE RESTRICT,
    description TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    body       TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS exclusions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    type    TEXT    NOT NULL CHECK (type IN ('album', 'path', 'tag', 'reviewed')),
    value   TEXT    NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS marker_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    condition     TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------- 运行
-- job_id / last_job_id 一律不加外键：jobs 会被 record_retention_days 清理，
-- 加 FK 会让清理永久失败。

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT    NOT NULL CHECK (type IN
                        ('scan','backfill','single','reclassify','dryrun','reset','undo')),
    status            TEXT    NOT NULL CHECK (status IN
                        ('queued','running','done','failed','canceled')),
    total             INTEGER NOT NULL DEFAULT 0,
    done              INTEGER NOT NULL DEFAULT 0,
    failed            INTEGER NOT NULL DEFAULT 0,
    params            TEXT    NOT NULL DEFAULT '{}',
    prompt_version_id INTEGER,
    started_at        TEXT,
    finished_at       TEXT,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS asset_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id          TEXT    NOT NULL,
    job_id            INTEGER,
    prompt_version_id INTEGER,
    source            TEXT    NOT NULL CHECK (source IN ('webhook','poll','manual')),
    predicted_tag     TEXT,
    confidence        REAL,
    markers           TEXT    NOT NULL DEFAULT '[]',
    model             TEXT,
    usage             TEXT,
    duration_ms       INTEGER,
    status            TEXT    NOT NULL CHECK (status IN ('ok','refused','error')),
    raw_response      TEXT,
    dry_run           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_asset_records_asset
    ON asset_records (asset_id, created_at DESC);

-- 任务详情页的失败清单：WHERE job_id = ? [AND status = 'error']
CREATE INDEX IF NOT EXISTS idx_asset_records_job
    ON asset_records (job_id);

CREATE TABLE IF NOT EXISTS asset_state (
    asset_id          TEXT PRIMARY KEY,
    current_tag       TEXT,
    current_markers   TEXT NOT NULL DEFAULT '[]',
    visibility        TEXT,
    manual_override   INTEGER NOT NULL DEFAULT 0,
    reviewed          INTEGER NOT NULL DEFAULT 0,
    last_job_id       INTEGER,
    lease_owner       INTEGER,
    -- UTC，格式必须是 'YYYY-MM-DD HH:MM:SS'（即 SQLite datetime('now') 的格式）。
    -- 该列与 datetime('now') 做**字符串**比较来判断租约是否过期，所以格式必须
    -- 定宽且以空格分隔。用 isoformat() 产生的 'T' 分隔符虽然表示同一时刻，但字典序
    -- 会排到后面 → 租约永不过期 → worker 崩溃后卡住的图片永不回收（静默故障）。
    -- 这里用 CHECK 把契约变成硬约束：写错格式直接报错，而不是静默失效。
    lease_expires_at  TEXT CHECK (
        lease_expires_at IS NULL
        OR lease_expires_at GLOB
           '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
    ),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_asset_state_lease
    ON asset_state (lease_expires_at);

CREATE TABLE IF NOT EXISTS change_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    asset_id   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN
                 ('tag_add','tag_remove','queue_remove','queue_restore',
                  'archive','timeline','album_add','album_remove')),
    direction  TEXT NOT NULL CHECK (direction IN ('apply','undo')),
    payload    TEXT NOT NULL DEFAULT '{}',
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_change_log_job ON change_log (job_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    source     TEXT NOT NULL CHECK (source IN ('webhook','poll','system')),
    event_type TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
