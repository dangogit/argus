-- Per-day rollup of a team's conversation + activity: durable agent memory.
CREATE TABLE conversation_summaries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id         text NOT NULL,
  conversation_id uuid REFERENCES conversations(id),
  day             date NOT NULL,
  summary         text NOT NULL,
  message_count   int NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (team_id, conversation_id, day)
);
CREATE INDEX conversation_summaries_lookup ON conversation_summaries (team_id, day DESC);
