CREATE TABLE advisor_messages (
  seq                bigserial PRIMARY KEY,
  jid                text NOT NULL,
  message_id         text NOT NULL,
  ts                 bigint NOT NULL DEFAULT 0,
  participant        text NOT NULL DEFAULT '',
  participant_jid    text NOT NULL DEFAULT '',
  push_name          text NOT NULL DEFAULT '',
  body               text NOT NULL DEFAULT '',
  mentioned          jsonb NOT NULL DEFAULT '[]',
  quoted_participant text NOT NULL DEFAULT '',
  payload            jsonb NOT NULL DEFAULT '{}',
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (jid, message_id)
);

CREATE INDEX advisor_messages_jid_seq
  ON advisor_messages (jid, seq);

CREATE TABLE advisor_replies (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  jid         text NOT NULL,
  ts          bigint NOT NULL DEFAULT 0,
  participant text NOT NULL DEFAULT '',
  reply_to_id text NOT NULL DEFAULT '',
  parts       int NOT NULL DEFAULT 0,
  skipped     boolean NOT NULL DEFAULT false,
  reason      text NOT NULL DEFAULT '',
  payload     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX advisor_replies_jid_ts
  ON advisor_replies (jid, ts DESC);

CREATE TABLE advisor_cursors (
  jid        text PRIMARY KEY,
  value      bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE advisor_attempts (
  jid        text NOT NULL,
  message_id text NOT NULL,
  attempts   int NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (jid, message_id)
);

CREATE TABLE advisor_digests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  jid           text NOT NULL,
  digest_date   date NOT NULL,
  message_count int NOT NULL DEFAULT 0,
  posted        boolean NOT NULL DEFAULT false,
  seed_topic    text NOT NULL DEFAULT '',
  reason        text NOT NULL DEFAULT '',
  payload       jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (jid, digest_date)
);

CREATE INDEX advisor_digests_jid_date
  ON advisor_digests (jid, digest_date DESC);
