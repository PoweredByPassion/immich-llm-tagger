ALTER TABLE asset_state ADD COLUMN process_status TEXT
    CHECK (process_status IN ('pending', 'applied', 'failed'));
ALTER TABLE asset_state ADD COLUMN retry_after TEXT;
ALTER TABLE asset_records ADD COLUMN apply_status TEXT
    CHECK (apply_status IN ('pending', 'applied', 'failed'));
ALTER TABLE asset_records ADD COLUMN analysis_record_id INTEGER;
ALTER TABLE asset_records ADD COLUMN apply_error TEXT;

INSERT INTO asset_state (asset_id, process_status)
SELECT asset_id, CASE WHEN applied_at IS NOT NULL THEN 'applied' ELSE 'pending' END
FROM (
    SELECT asset_id, status, applied_at,
           ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY created_at DESC, id DESC) AS position
    FROM asset_records WHERE dry_run = 0
)
WHERE position = 1 AND (applied_at IS NOT NULL OR status = 'ok')
ON CONFLICT(asset_id) DO UPDATE SET process_status = excluded.process_status;

UPDATE asset_records SET apply_status = 'applied' WHERE applied_at IS NOT NULL;

CREATE TABLE pending_effects (
    asset_id TEXT PRIMARY KEY,
    job_id INTEGER,
    analysis_record_id INTEGER,
    batches TEXT NOT NULL,
    in_flight INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_pending_effects_analysis ON pending_effects(analysis_record_id);
CREATE INDEX idx_pending_effects_job ON pending_effects(job_id);

ALTER TABLE jobs ADD COLUMN discovery_complete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN scan_page INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN scan_cursor TEXT;
ALTER TABLE jobs ADD COLUMN rule_snapshot TEXT;
ALTER TABLE jobs ADD COLUMN available_at TEXT;
CREATE TABLE active_asset_mutations (id INTEGER PRIMARY KEY AUTOINCREMENT);

CREATE INDEX idx_asset_state_processing ON asset_state(process_status, retry_after);
CREATE INDEX idx_jobs_status_type ON jobs(status, type);
CREATE INDEX idx_asset_records_created_production ON asset_records(created_at) WHERE dry_run = 0;
CREATE INDEX idx_asset_records_applied_production ON asset_records(applied_at)
    WHERE dry_run = 0 AND applied_at IS NOT NULL;
CREATE INDEX idx_change_log_action_direction ON change_log(action, direction);
CREATE INDEX idx_jobs_active_auto_asset ON jobs(json_extract(params, '$.asset_id'))
    WHERE type IN ('single','classify_item') AND status IN ('queued','running');
CREATE INDEX idx_jobs_children_asset ON jobs(json_extract(params, '$.parent_job_id'), json_extract(params, '$.asset_id'))
    WHERE type = 'classify_item';
