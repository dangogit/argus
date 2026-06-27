CREATE TABLE conversation_contexts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id      text NOT NULL,
  channel_ref  text NOT NULL,
  context_type text NOT NULL CHECK (context_type IN ('support_case')),
  context_ref  text NOT NULL,
  status       text NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'learned', 'resolved', 'expired')),
  summary      text NOT NULL DEFAULT '',
  payload      jsonb NOT NULL DEFAULT '{}',
  expires_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (team_id, channel_ref, context_type, context_ref)
);

CREATE INDEX conversation_contexts_active
  ON conversation_contexts (team_id, channel_ref, updated_at DESC)
  WHERE status='active';
