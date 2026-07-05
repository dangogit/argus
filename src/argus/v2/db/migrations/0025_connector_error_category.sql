-- Distinguish a permanent auth failure (bad/expired key) from a transient one
-- (timeout, 5xx, network) so the orchestrator sweep can alert on auth failures
-- once instead of treating every connector error the same.
ALTER TABLE connector_state ADD COLUMN error_category text;
