-- v6（PERF-2）：webhook 在途去重查询的表达式 partial index。
-- hooks.py 的 _has_active_single 按 (type='single', status IN queued/running,
-- json_extract(params,'$.asset_id')) 查询；无索引时对 jobs 全表 SCAN 并
-- 逐行解析 JSON（大积压 + 高频 webhook 下 CPU 飙升，且该查询在事件循环
-- 线程执行）。实测 10k jobs 积压：miss 查询 3.7ms → 0.01ms（100×）。
-- 注意：partial index 的 WHERE 必须与查询谓词匹配（SQLite 匹配规则），
-- 改动查询条件时需同步改本索引。

CREATE INDEX IF NOT EXISTS idx_jobs_active_single_asset
    ON jobs (json_extract(params, '$.asset_id'))
    WHERE type = 'single' AND status IN ('queued', 'running');
