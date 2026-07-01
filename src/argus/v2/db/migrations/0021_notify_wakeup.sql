-- Wake the orchestrator loop on write instead of relying only on the 2s poll.
-- The loop LISTENs on argus_jobs and argus_actions (see orchestrator/loop.py);
-- events feed the sweep too (route_events), so it also gets a channel. Payload
-- is empty: the loop only needs a wakeup signal, sweep_once re-reads state.

CREATE OR REPLACE FUNCTION argus_notify_jobs() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('argus_jobs', '');
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION argus_notify_actions() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('argus_actions', '');
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION argus_notify_events() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('argus_events', '');
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_notify_insert
  AFTER INSERT ON jobs
  FOR EACH ROW EXECUTE FUNCTION argus_notify_jobs();

CREATE TRIGGER jobs_notify_status_update
  AFTER UPDATE OF status ON jobs
  FOR EACH ROW EXECUTE FUNCTION argus_notify_jobs();

CREATE TRIGGER actions_notify_insert
  AFTER INSERT ON actions
  FOR EACH ROW EXECUTE FUNCTION argus_notify_actions();

CREATE TRIGGER actions_notify_status_update
  AFTER UPDATE OF status ON actions
  FOR EACH ROW EXECUTE FUNCTION argus_notify_actions();

CREATE TRIGGER events_notify_insert
  AFTER INSERT ON events
  FOR EACH ROW EXECUTE FUNCTION argus_notify_events();
