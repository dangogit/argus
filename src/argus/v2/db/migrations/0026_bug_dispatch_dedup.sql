-- Duplicate-work guard: the same bug report id produced two GitHub PRs in two
-- different repos (tadam PR #324, tadam-agents PR #302, 2026-07-01) because
-- nothing checked for an existing open dispatch on the same bug id across
-- teams. Store the extracted bug identifier on the request row (nullable, most
-- requests have no bug reference) so dispatch time can look it up company-wide
-- with a plain indexed query instead of scanning event payloads or task text.
ALTER TABLE requests ADD COLUMN bug_ref text;

-- Only non-terminal requests (open, awaiting_approval) can block a new
-- dispatch, so a partial index on those statuses keeps the dedup lookup cheap
-- and matches the existing requests_active_fingerprint index style.
CREATE INDEX requests_active_bug_ref
  ON requests (bug_ref)
  WHERE bug_ref IS NOT NULL AND status IN ('open','awaiting_approval');
