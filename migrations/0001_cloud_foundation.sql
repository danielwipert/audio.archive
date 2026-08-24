BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE csv_imports (
    id BIGSERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    imported_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_rows INTEGER NOT NULL CHECK (accepted_rows >= 0),
    rejected_rows INTEGER NOT NULL CHECK (rejected_rows >= 0),
    duplicate_rows INTEGER NOT NULL CHECK (duplicate_rows >= 0)
);

CREATE TABLE jobs (
    id BIGSERIAL PRIMARY KEY,
    processing_state TEXT NOT NULL,
    delivery_state TEXT NOT NULL DEFAULT 'not_published',
    origin TEXT NOT NULL,
    requested_artist TEXT,
    requested_title TEXT,
    requested_version TEXT,
    requested_url TEXT,
    profile TEXT NOT NULL,
    import_id BIGINT REFERENCES csv_imports(id),
    import_row INTEGER,
    source_extractor TEXT,
    source_id TEXT,
    source_url TEXT,
    source_title TEXT,
    source_creator TEXT,
    resolution_method TEXT,
    selected_score INTEGER,
    runner_up_score INTEGER,
    progress_percent DOUBLE PRECISION,
    quality_status TEXT,
    warning_summary TEXT,
    error_stage TEXT,
    error_class TEXT,
    error_summary TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    published_at_utc TIMESTAMPTZ,
    expires_at_utc TIMESTAMPTZ,
    deletion_requested_at_utc TIMESTAMPTZ,
    deleted_at_utc TIMESTAMPTZ,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at_utc TIMESTAMPTZ,
    completed_at_utc TIMESTAMPTZ,
    CONSTRAINT jobs_processing_state_check CHECK (
        processing_state IN (
            'pending', 'resolving', 'needs_review', 'ready', 'downloading',
            'verifying_master', 'converting', 'verifying_output', 'packaging',
            'publishing', 'completed', 'completed_with_warnings', 'failed',
            'interrupted', 'skipped_duplicate', 'not_found', 'cancelled'
        )
    ),
    CONSTRAINT jobs_delivery_state_check CHECK (
        delivery_state IN (
            'not_published', 'available', 'deletion_pending', 'expired', 'deleted'
        )
    ),
    CONSTRAINT jobs_profile_check CHECK (profile IN ('ableton', 'source', 'package')),
    CONSTRAINT jobs_origin_check CHECK (origin IN ('manual', 'url', 'csv', 'cli')),
    CONSTRAINT jobs_request_check CHECK (
        requested_url IS NOT NULL
        OR (requested_artist IS NOT NULL AND requested_title IS NOT NULL)
    ),
    CONSTRAINT jobs_expiry_check CHECK (
        expires_at_utc IS NULL OR published_at_utc IS NOT NULL
    )
);

CREATE INDEX jobs_processing_state_idx ON jobs(processing_state, id);
CREATE INDEX jobs_delivery_state_idx ON jobs(delivery_state, expires_at_utc);
CREATE INDEX jobs_source_idx ON jobs(source_extractor, source_id);
CREATE INDEX jobs_created_idx ON jobs(created_at_utc DESC);

CREATE TABLE job_events (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    occurred_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    from_processing_state TEXT,
    to_processing_state TEXT,
    from_delivery_state TEXT,
    to_delivery_state TEXT,
    event_type TEXT NOT NULL,
    message TEXT,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX job_events_job_idx ON job_events(job_id, id);

CREATE TABLE candidates (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    video_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    channel TEXT,
    duration_seconds DOUBLE PRECISION,
    thumbnail_url TEXT,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    disqualified BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE(job_id, video_id)
);

CREATE TABLE processing_attempts (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    worker_network_class TEXT NOT NULL DEFAULT 'unknown',
    started_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at_utc TIMESTAMPTZ,
    result TEXT,
    error_class TEXT,
    error_summary TEXT,
    tool_versions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT processing_attempt_network_class_check CHECK (
        worker_network_class IN ('cloud_datacenter', 'residential', 'unknown')
    )
);

CREATE INDEX processing_attempts_job_idx ON processing_attempts(job_id, id);

CREATE TABLE worker_claims (
    job_id BIGINT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    claim_token UUID NOT NULL UNIQUE,
    claimed_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at_utc TIMESTAMPTZ NOT NULL
);

CREATE INDEX worker_claims_lease_idx ON worker_claims(lease_expires_at_utc);

CREATE TABLE outputs (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    object_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    media_properties_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_at_utc TIMESTAMPTZ NOT NULL,
    expires_at_utc TIMESTAMPTZ NOT NULL,
    deleted_at_utc TIMESTAMPTZ,
    UNIQUE(job_id, role, object_key),
    CONSTRAINT outputs_role_check CHECK (role IN ('source', 'ableton', 'package')),
    CONSTRAINT outputs_expiry_check CHECK (expires_at_utc > published_at_utc)
);

CREATE INDEX outputs_job_idx ON outputs(job_id, id);
CREATE INDEX outputs_expiry_idx ON outputs(expires_at_utc) WHERE deleted_at_utc IS NULL;

INSERT INTO schema_migrations (version) VALUES (1);

COMMIT;
