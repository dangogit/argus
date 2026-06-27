CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE knowledge (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope      text NOT NULL CHECK (scope IN ('company','team')),
  team_id    text,
  title      text NOT NULL,
  content    text NOT NULL,
  embedding  vector,
  source     text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX knowledge_scope ON knowledge (scope, team_id);
CREATE INDEX knowledge_fts ON knowledge USING gin (to_tsvector('english', content));
