CREATE TABLE team_obligations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('code', 'support', 'maintenance')),
  fingerprint text NOT NULL,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN (
    'open', 'working', 'awaiting_pr', 'awaiting_merge', 'awaiting_deploy',
    'verifying', 'awaiting_approval', 'blocked', 'done', 'failed'
  )),
  priority smallint NOT NULL DEFAULT 50,
  request_id uuid REFERENCES requests(id) ON DELETE SET NULL,
  action_id uuid REFERENCES actions(id) ON DELETE SET NULL,
  provider_ref text,
  source_ref text,
  definition_of_done jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  attempts integer NOT NULL DEFAULT 0,
  next_check_at timestamptz NOT NULL DEFAULT now(),
  blocked_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (team_id, fingerprint)
);

CREATE INDEX idx_team_obligations_due
  ON team_obligations (next_check_at, priority DESC)
  WHERE status NOT IN ('done', 'failed');

CREATE TABLE team_obligation_events (
  id bigserial PRIMARY KEY,
  obligation_id uuid NOT NULL REFERENCES team_obligations(id) ON DELETE CASCADE,
  from_status text,
  to_status text NOT NULL,
  reason text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_team_obligation_events_obligation
  ON team_obligation_events (obligation_id, id);
