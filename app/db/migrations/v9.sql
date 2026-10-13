-- v9：jobs.type CHECK 增加 classify_item（scan 拆分的逐图子任务）。
-- SQLite 不能原地改 CHECK，走表重建：建新表 → 拷贝 → drop → rename。
-- 必须保留：
--   1. cancel_requested 列（v2）。
--   2. AUTOINCREMENT 计数器（sqlite_sequence）：DROP TABLE 会删掉它，重建后
--      新表 seq = 拷贝行的 MAX(id)。若升级时 jobs 表恰好为空（record_retention_days
--      已把终态 job 清光），seq 归零 → 下一个 job 复用 id 1 → change_log 里旧
--      job 的资产会被新 job 的 undo 误伤。v9 之前 AUTOINCREMENT 杜绝了这种复用，
--      这里必须捕获旧值再恢复。
--   3. v6 的 partial index（随 DROP TABLE 消失，重建）。
-- 另新增 idx_jobs_pending_children：pending_children 每 0.5s 轮询一次，按
-- (type='classify_item', status IN queued/running, parent_job_id) 查询。

CREATE TEMP TABLE _jobs_seq AS
    SELECT seq FROM sqlite_sequence WHERE name = 'jobs';

CREATE TABLE jobs_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT    NOT NULL CHECK (type IN
                        ('scan','backfill','single','reclassify','dryrun',
                         'reset','undo','classify_item')),
    status            TEXT    NOT NULL CHECK (status IN
                        ('queued','running','done','failed','canceled')),
    total             INTEGER NOT NULL DEFAULT 0,
    done              INTEGER NOT NULL DEFAULT 0,
    failed            INTEGER NOT NULL DEFAULT 0,
    params            TEXT    NOT NULL DEFAULT '{}',
    prompt_version_id INTEGER,
    started_at        TEXT,
    finished_at       TEXT,
    error             TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0
);

INSERT INTO jobs_new SELECT
    id, type, status, total, done, failed, params, prompt_version_id,
    started_at, finished_at, error, cancel_requested
FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

-- 恢复计数器：绝不降低（新表 seq = 拷贝行 MAX(id) ≤ 旧 seq）。
-- 注意：sqlite_sequence 的 name 无唯一约束，INSERT OR REPLACE 只会**追加重复行**
-- 而不是替换，反而让 AUTOINCREMENT 取到 0；必须用 UPDATE。
UPDATE sqlite_sequence SET seq = (SELECT seq FROM _jobs_seq)
    WHERE name = 'jobs'
      AND (SELECT seq FROM _jobs_seq) > sqlite_sequence.seq;
-- 兜底：rename 后若不存在 jobs 行（不同 SQLite 版本差异）则补插。
INSERT INTO sqlite_sequence(name, seq)
    SELECT 'jobs', (SELECT seq FROM _jobs_seq)
    WHERE EXISTS (SELECT 1 FROM _jobs_seq)
      AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'jobs');
DROP TABLE _jobs_seq;

CREATE INDEX IF NOT EXISTS idx_jobs_active_single_asset
    ON jobs (json_extract(params, '$.asset_id'))
    WHERE type = 'single' AND status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_jobs_pending_children
    ON jobs (json_extract(params, '$.parent_job_id'))
    WHERE type = 'classify_item' AND status IN ('queued', 'running');
