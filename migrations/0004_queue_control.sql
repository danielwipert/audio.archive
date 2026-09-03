-- Audio Archive Cloud v0.1 - queue pause control.
--
-- CLOUD_SPEC section 10.2 requires "pause after current job" and "resume". Pausing is a
-- property of the queue rather than of any job, so it lives in one row the worker reads
-- before it claims. A paused queue never interrupts work already in progress: the
-- running job finishes and no further job is claimed.

BEGIN;

CREATE TABLE queue_control (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    paused_at_utc TIMESTAMPTZ,
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO queue_control (id, paused) VALUES (TRUE, FALSE);

INSERT INTO schema_migrations (version) VALUES (4);

COMMIT;
