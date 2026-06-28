-- Defer an event's next routing attempt. When a team is over its budget cap,
-- route_events parks its event with a defer_until so the claim loop skips it
-- until then, instead of re-claiming and re-deferring it every poll tick.
ALTER TABLE events ADD COLUMN defer_until timestamptz;
