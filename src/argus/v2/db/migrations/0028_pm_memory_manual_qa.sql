ALTER TABLE pm_lessons DROP CONSTRAINT IF EXISTS pm_lessons_outcome_check;

ALTER TABLE pm_lessons
  ADD CONSTRAINT pm_lessons_outcome_check
  CHECK (outcome IN (
    'proposed',
    'qa-pass',
    'qa-fail',
    'no-change',
    'blocked',
    'found-not-fixed',
    'manual-qa'
  ));
