-- Audio Archive Cloud v0.1 - automatic backoff after a YouTube access failure.
--
-- A rate-limited or challenged acquisition is a transient condition of the worker's
-- network path, not a defect in the job. These columns let the worker requeue such a
-- job for a later attempt instead of leaving it failed for a user to retry by hand.

BEGIN;

ALTER TABLE jobs
    ADD COLUMN access_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (access_retry_count >= 0),
    ADD COLUMN retry_not_before_utc TIMESTAMPTZ;

CREATE INDEX jobs_retry_not_before_idx ON jobs(retry_not_before_utc)
    WHERE retry_not_before_utc IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES (2);

COMMIT;
