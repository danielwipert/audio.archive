-- Audio Archive Cloud v0.1 - keep the acquisition log of a failed attempt.
--
-- A successful job publishes ingest.log as a downloadable sidecar, but a failed one used
-- to keep only the last kilobyte of yt-dlp's error output. That is too little to tell a
-- proxy that changed address mid-download from a token YouTube rejected. The log is
-- stored per attempt, so an automatic retry that clears the job's failure detail does not
-- also discard the evidence of why it was needed.

BEGIN;

ALTER TABLE processing_attempts ADD COLUMN failure_log TEXT;

INSERT INTO schema_migrations (version) VALUES (5);

COMMIT;
