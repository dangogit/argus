ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS advanced_at timestamptz;

CREATE INDEX IF NOT EXISTS jobs_pipeline_unadvanced
  ON jobs (updated_at)
  WHERE kind='pipeline'
    AND status IN ('done','dead')
    AND advanced_at IS NULL;
