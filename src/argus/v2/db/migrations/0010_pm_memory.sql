CREATE TABLE pm_lessons (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id     text NOT NULL,
  fingerprint text NOT NULL,
  finding     text NOT NULL,
  outcome     text NOT NULL CHECK (outcome IN ('proposed','qa-pass','qa-fail','no-change')),
  note        text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX pm_lessons_team_created
  ON pm_lessons (team_id, created_at DESC);

CREATE TABLE pm_lesson_attributions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id            text NOT NULL,
  request_id         uuid NOT NULL REFERENCES requests(id),
  lesson_fingerprint text NOT NULL,
  outcome            text NOT NULL CHECK (outcome IN ('qa-pass','qa-fail')),
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (team_id, request_id, lesson_fingerprint)
);

CREATE INDEX pm_lesson_attributions_team_fingerprint
  ON pm_lesson_attributions (team_id, lesson_fingerprint);

CREATE TABLE retro_records (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id    text NOT NULL,
  retro_day  date NOT NULL DEFAULT current_date,
  payload    jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX retro_records_day_team
  ON retro_records (retro_day, team_id);

CREATE TABLE retro_backlog (
  id         text PRIMARY KEY,
  team_id    text NOT NULL,
  type       text NOT NULL CHECK (type IN ('lesson','skill','prompt-edit','process-edit','infra-flag')),
  status     text NOT NULL CHECK (status IN ('gated','quarantined','infra-notice')),
  priority   int NOT NULL DEFAULT 1,
  statement  text NOT NULL,
  trigger    text NOT NULL DEFAULT '',
  payload    jsonb NOT NULL DEFAULT '{}',
  bridged_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX retro_backlog_status
  ON retro_backlog (status, type, priority DESC);
