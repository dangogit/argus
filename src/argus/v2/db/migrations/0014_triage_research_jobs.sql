-- Allow PM-triage and researcher jobs in the jobs table.
-- Signals now route through the team manager (kind='triage') which may invoke a
-- read-only researcher (kind='research') before dispatching the dev pipeline.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
  CHECK (kind IN ('front', 'pipeline', 'converse', 'triage', 'research'));
