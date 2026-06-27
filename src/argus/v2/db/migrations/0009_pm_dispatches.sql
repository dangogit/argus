CREATE TABLE pm_dispatches (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id        text NOT NULL,
  fingerprint    text NOT NULL,
  dispatch_day   date NOT NULL DEFAULT current_date,
  source         text NOT NULL DEFAULT 'pm',
  request_id     uuid REFERENCES requests(id),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX pm_dispatches_day_team
  ON pm_dispatches (team_id, dispatch_day, created_at);
