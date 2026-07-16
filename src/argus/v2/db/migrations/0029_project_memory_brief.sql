ALTER TABLE conversation_summaries
  ADD COLUMN details jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN source_fingerprint text NOT NULL DEFAULT '',
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
