-- Effector 化重构：评分 / 描述落库 + apply 重试凭据 + change_log 枚举扩容。
-- SQLite 改不了 CHECK，change_log 必须表重建（照抄 v9.sql 的 jobs_new 模式）。
-- 表重建必须保留：
--   1. AUTOINCREMENT 计数器（sqlite_sequence）：DROP TABLE 会删掉它，重建后
--      seq = 拷贝行的 MAX(id)。若升级时 change_log 恰好为空（历史清理过），
--      seq 归零 → 新行复用旧 id → undo 会撤到别的 job 的行。
--   2. idx_change_log_job（随 DROP TABLE 消失，重建）。

CREATE TEMP TABLE _cl_seq AS
    SELECT seq FROM sqlite_sequence WHERE name = 'change_log';

ALTER TABLE asset_records ADD COLUMN rating INTEGER;
ALTER TABLE asset_records ADD COLUMN description TEXT;

ALTER TABLE asset_state ADD COLUMN rating INTEGER;
ALTER TABLE asset_state ADD COLUMN description TEXT;
ALTER TABLE asset_state ADD COLUMN apply_attempts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE change_log_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    asset_id   TEXT    NOT NULL,
    action     TEXT    NOT NULL CHECK (action IN (
                   'tag_add','tag_remove','queue_remove','queue_restore',
                   'archive','timeline','album_add','album_remove',
                   'rating_set','description_set')),
    direction  TEXT    NOT NULL CHECK (direction IN ('apply','undo')),
    payload    TEXT    NOT NULL DEFAULT '{}',
    applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO change_log_new
    (id, job_id, asset_id, action, direction, payload, applied_at)
SELECT id, job_id, asset_id, action, direction, payload, applied_at
  FROM change_log;

DROP TABLE change_log;
ALTER TABLE change_log_new RENAME TO change_log;

-- 恢复计数器：绝不降低（新表 seq = 拷贝行 MAX(id) ≤ 旧 seq）。
-- sqlite_sequence.name 无唯一约束，INSERT OR REPLACE 只会追加重复行，
-- 反而让 AUTOINCREMENT 取到 0 —— 必须用 UPDATE。
UPDATE sqlite_sequence SET seq = (SELECT seq FROM _cl_seq)
    WHERE name = 'change_log'
      AND (SELECT seq FROM _cl_seq) > sqlite_sequence.seq;
-- 兜底：rename 后若不存在 change_log 行（不同 SQLite 版本差异）则补插。
INSERT INTO sqlite_sequence(name, seq)
    SELECT 'change_log', (SELECT seq FROM _cl_seq)
    WHERE EXISTS (SELECT 1 FROM _cl_seq)
      AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'change_log');
DROP TABLE _cl_seq;

CREATE INDEX IF NOT EXISTS idx_change_log_job ON change_log (job_id);

-- 回填：applied_at 历史上从未被写过（§4.4）；不回填则改口径后
-- 「整库历史资产都被判为未完成」→ 全量重跑 + 大规模 LLM 调用。
UPDATE asset_records
   SET applied_at = created_at
 WHERE dry_run = 0 AND status = 'ok' AND applied_at IS NULL;
