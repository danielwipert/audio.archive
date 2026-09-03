-- Audio Archive Cloud v0.1 - per-job choice of downloadable formats.
--
-- A job used to carry one profile, which fixed the single derivative the worker made.
-- The user now chooses any combination of the Ableton 32-bit float WAV, a 24-bit PCM
-- WAV, an MP3 listening copy and the archive package. The verified source master is
-- always published, so it is not part of the chosen set. The profile column stays as
-- the coarse preset recorded in provenance; requested_outputs is what the worker acts on.

BEGIN;

ALTER TABLE jobs
    ADD COLUMN requested_outputs TEXT[] NOT NULL DEFAULT '{}',
    ADD CONSTRAINT jobs_requested_outputs_check CHECK (
        requested_outputs <@ ARRAY['ableton', 'wav24', 'listen', 'package']::TEXT[]
    );

UPDATE jobs
SET requested_outputs = CASE profile
    WHEN 'ableton' THEN ARRAY['ableton']::TEXT[]
    WHEN 'package' THEN ARRAY['ableton', 'package']::TEXT[]
    ELSE ARRAY[]::TEXT[]
END;

ALTER TABLE outputs
    DROP CONSTRAINT outputs_role_check,
    ADD CONSTRAINT outputs_role_check CHECK (
        role IN ('source', 'ableton', 'wav24', 'listen', 'package')
    );

INSERT INTO schema_migrations (version) VALUES (3);

COMMIT;
