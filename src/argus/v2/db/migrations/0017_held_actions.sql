ALTER TABLE actions DROP CONSTRAINT actions_status_check;
ALTER TABLE actions ADD CONSTRAINT actions_status_check
  CHECK (status IN ('proposed','awaiting_approval','approved','executing','held','done','failed','rejected'));
