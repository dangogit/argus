-- Allow converse jobs (kind='converse') in the jobs table.
-- The existing check constraint is dropped and replaced to include the new value.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
  CHECK (kind IN ('front', 'pipeline', 'converse'));
