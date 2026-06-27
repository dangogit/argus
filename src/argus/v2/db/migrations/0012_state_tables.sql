CREATE TABLE context_watermarks (
  job        text PRIMARY KEY,
  source     text NOT NULL DEFAULT '',
  last_id    bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE context_commitments (
  id           text PRIMARY KEY,
  dedup_hash   text NOT NULL UNIQUE,
  who          text NOT NULL DEFAULT '',
  what         text NOT NULL,
  due_at       text NOT NULL DEFAULT '',
  source_ref   text NOT NULL DEFAULT '',
  status       text NOT NULL CHECK (status IN ('open','surfaced','done','dismissed')),
  surfaced_at  text NOT NULL DEFAULT '',
  snooze_until text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX context_commitments_status_due
  ON context_commitments (status, due_at, id);

CREATE TABLE context_reminder_batches (
  fingerprint text PRIMARY KEY,
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE content_drafts (
  id         text PRIMARY KEY,
  project    text NOT NULL,
  platform   text NOT NULL,
  status     text NOT NULL DEFAULT 'ready',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX content_drafts_status
  ON content_drafts (status, created_at DESC);

CREATE TABLE content_queue (
  id         text PRIMARY KEY,
  project    text NOT NULL,
  platform   text NOT NULL,
  request    text NOT NULL,
  status     text NOT NULL CHECK (status IN ('queued','drafted','dead')),
  attempts   int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX content_queue_status_created
  ON content_queue (status, created_at);

CREATE TABLE content_breaker_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project     text NOT NULL,
  breaker_day date NOT NULL DEFAULT current_date,
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX content_breaker_project_day
  ON content_breaker_events (project, breaker_day);

CREATE TABLE support_thread_events (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_key  text UNIQUE,
  project    text NOT NULL,
  thread_id  text NOT NULL,
  action     text NOT NULL,
  sender     text NOT NULL DEFAULT '',
  subject    text NOT NULL DEFAULT '',
  reason     text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX support_thread_events_project_thread
  ON support_thread_events (project, thread_id, created_at);

CREATE TABLE support_drafts (
  id         text PRIMARY KEY,
  project    text NOT NULL,
  thread_id  text NOT NULL,
  sender     text NOT NULL DEFAULT '',
  subject    text NOT NULL DEFAULT '',
  reply      text NOT NULL DEFAULT '',
  transport  text NOT NULL DEFAULT '',
  status     text NOT NULL DEFAULT 'ready',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX support_drafts_project_thread
  ON support_drafts (project, thread_id, updated_at DESC);

CREATE TABLE support_guidance (
  id             text PRIMARY KEY,
  project        text NOT NULL,
  thread_id      text NOT NULL,
  sender         text NOT NULL DEFAULT '',
  subject        text NOT NULL DEFAULT '',
  question       text NOT NULL DEFAULT '',
  proposed_reply text NOT NULL DEFAULT '',
  thread_text    text NOT NULL DEFAULT '',
  status         text NOT NULL DEFAULT 'pending',
  answer         text NOT NULL DEFAULT '',
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX support_guidance_project_status
  ON support_guidance (project, status, updated_at DESC);

CREATE TABLE assistant_history (
  id         bigserial PRIMARY KEY,
  role       text NOT NULL DEFAULT 'unknown',
  text       text NOT NULL DEFAULT '',
  payload    jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE assistant_memory (
  name       text PRIMARY KEY,
  content    text NOT NULL DEFAULT '',
  watermark  bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
