CREATE TABLE inverse_steps (
    job_id          INTEGER NOT NULL,
    source_change   INTEGER NOT NULL REFERENCES change_log(id),
    asset_id        TEXT NOT NULL,
    source_payload  TEXT NOT NULL,
    inverse_payload TEXT,
    inverse_direction TEXT NOT NULL CHECK (inverse_direction IN ('apply', 'undo')),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'skipped')),
    PRIMARY KEY (job_id, source_change)
);

CREATE INDEX idx_inverse_steps_pending_asset ON inverse_steps(asset_id) WHERE status = 'pending';
