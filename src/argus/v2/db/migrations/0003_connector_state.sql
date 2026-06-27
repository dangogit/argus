-- Durable per-source poll cursor so only NEW items ingest across restarts.
CREATE TABLE connector_state (
  source_name    text PRIMARY KEY,
  cursor         jsonb NOT NULL DEFAULT '{}',
  last_polled_at timestamptz,
  updated_at     timestamptz NOT NULL DEFAULT now()
);
