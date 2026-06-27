-- Add a middle risk class for owner-personal actions. Irreversible system
-- actions stay approval-gated by config loader policy.
ALTER TABLE actions DROP CONSTRAINT actions_risk_check;
ALTER TABLE actions ADD CONSTRAINT actions_risk_check
  CHECK (risk IN ('reversible_internal', 'personal_outward', 'irreversible_outward'));
