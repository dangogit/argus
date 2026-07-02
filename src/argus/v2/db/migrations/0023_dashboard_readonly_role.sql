-- The dashboard currently reads Postgres with the full-privilege operator
-- DSN: a compromised dashboard is full read/write DB access. Give it a
-- dedicated NOLOGIN role scoped to SELECT only, so the operator can create a
-- login user in that role and point ARGUS_DB_DSN at it instead.
--
-- This migration only creates the role and grants; it does not create a
-- login user (passwords do not belong in migrations). To provision the
-- actual dashboard DB user, run once as a superuser:
--
--   CREATE ROLE dashboard_login LOGIN PASSWORD '<generated-secret>' IN ROLE argus_dashboard;
--
-- then set ARGUS_DB_DSN for the dashboard to that user's connection string.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'argus_dashboard') THEN
    CREATE ROLE argus_dashboard NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO argus_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO argus_dashboard;

-- Cover tables added by later migrations without a follow-up grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO argus_dashboard;
