-- Prompt versioning: correlate output regressions with prompt/rules/skills
-- edits. Nullable since historical rows predate the hash.

ALTER TABLE runs
  ADD COLUMN prompt_hash text;
