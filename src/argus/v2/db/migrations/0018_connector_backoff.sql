-- Connector failure backoff + error visibility. A failing source (expired key,
-- provider down) used to be retried every tick with no record; now we count
-- consecutive failures, store the last error, and gate the next poll so a dead
-- connector backs off instead of hammering the provider.
ALTER TABLE connector_state ADD COLUMN error_count   integer NOT NULL DEFAULT 0;
ALTER TABLE connector_state ADD COLUMN last_error    text;
ALTER TABLE connector_state ADD COLUMN last_error_at timestamptz;
ALTER TABLE connector_state ADD COLUMN poll_after    timestamptz;
