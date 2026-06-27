-- Argus core schema. One durable store for events, queue, outbox.
CREATE TABLE conversations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id       text NOT NULL,
  channel_ref   text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_activity_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid REFERENCES conversations(id),
  team_id         text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('message','signal')),
  source          text NOT NULL,
  payload         jsonb NOT NULL DEFAULT '{}',
  dedup_key       text NOT NULL,
  status          text NOT NULL DEFAULT 'received'
                    CHECK (status IN ('received','processing','processed')),
  received_at     timestamptz NOT NULL DEFAULT now(),
  processed_at    timestamptz,
  UNIQUE (source, dedup_key)
);
CREATE INDEX events_unprocessed ON events (status) WHERE status <> 'processed';

CREATE TABLE media (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id    uuid NOT NULL REFERENCES events(id),
  kind        text NOT NULL CHECK (kind IN ('audio','image','file')),
  path        text NOT NULL,
  mime        text,
  bytes       bigint,
  checksum    text,
  transcript  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE requests (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id        uuid NOT NULL REFERENCES events(id),
  team_id         text NOT NULL,
  conversation_id uuid REFERENCES conversations(id),
  status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','awaiting_approval','done','failed','cancelled')),
  current_stage   int NOT NULL DEFAULT 0,
  branch_counters jsonb NOT NULL DEFAULT '{}',
  fingerprint     text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
-- One active request per (team, fingerprint): dedup holds across open AND awaiting_approval.
CREATE UNIQUE INDEX requests_active_fingerprint
  ON requests (team_id, fingerprint)
  WHERE fingerprint IS NOT NULL AND status IN ('open','awaiting_approval');

CREATE TABLE jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id      uuid REFERENCES requests(id),
  event_id        uuid REFERENCES events(id),
  conversation_id uuid REFERENCES conversations(id),
  team_id         text NOT NULL,
  role            text NOT NULL,
  stage           int NOT NULL DEFAULT 0,
  kind            text NOT NULL CHECK (kind IN ('front','pipeline')),
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','claimed','running','awaiting_approval','done','failed','dead')),
  attempts        int NOT NULL DEFAULT 0,
  max_attempts    int NOT NULL DEFAULT 3,
  claim_token     uuid,
  claimed_by      text,
  claimed_at      timestamptz,
  lease_expires_at timestamptz,
  heartbeat_at    timestamptz,
  run_after       timestamptz NOT NULL DEFAULT now(),
  idempotency_key text NOT NULL UNIQUE,
  exec_snapshot   jsonb NOT NULL DEFAULT '{}',
  payload         jsonb NOT NULL DEFAULT '{}',
  result          jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claimable ON jobs (run_after) WHERE status='pending';
CREATE INDEX jobs_leased ON jobs (lease_expires_at) WHERE status IN ('claimed','running');

CREATE TABLE runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id      uuid NOT NULL REFERENCES jobs(id),
  attempt     int NOT NULL,
  claim_token uuid NOT NULL,
  role        text NOT NULL,
  engine      text NOT NULL,
  model       text,
  prompt      text,
  output      text,
  cost_source text,
  cost_usd    text,
  status      text NOT NULL,
  started_at  timestamptz NOT NULL DEFAULT now(),
  ended_at    timestamptz
);

CREATE TABLE actions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id      uuid REFERENCES requests(id),
  job_id          uuid REFERENCES jobs(id),
  team_id         text NOT NULL,
  type            text NOT NULL,
  risk            text NOT NULL CHECK (risk IN ('reversible_internal','irreversible_outward')),
  destination_ref text,
  status          text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','awaiting_approval','approved','executing','done','failed','rejected')),
  idempotency_key text NOT NULL UNIQUE,
  provider_ref    text,
  payload         jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX actions_pending ON actions (status) WHERE status IN ('proposed','approved');

CREATE TABLE approvals (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_id    uuid NOT NULL REFERENCES actions(id),
  request_id   uuid REFERENCES requests(id),
  status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','expired')),
  approver_ref text,
  nonce        text NOT NULL UNIQUE,
  expires_at   timestamptz NOT NULL,
  consumed_at  timestamptz
);
