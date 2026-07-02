-- A double-claimed job (reclaimed lease while the original worker still ran)
-- could write two run rows for the same (job_id, attempt), a phantom audit
-- row. Dedupe existing duplicates first (keep the earliest row per
-- job_id/attempt, by started_at then id as a stable tiebreak) so this
-- migration is safe on a live database, then enforce uniqueness going
-- forward.

DELETE FROM runs r
USING runs keep
WHERE r.job_id = keep.job_id
  AND r.attempt = keep.attempt
  AND (r.started_at, r.id) > (keep.started_at, keep.id);

ALTER TABLE runs
  ADD CONSTRAINT runs_job_id_attempt_key UNIQUE (job_id, attempt);
