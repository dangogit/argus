CREATE TABLE alerts (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts          timestamptz NOT NULL DEFAULT clock_timestamp(),
  severity    text NOT NULL CHECK (severity IN ('info','warn','error','critical')),
  project     text NOT NULL,
  fingerprint text NOT NULL,
  message     text NOT NULL,
  channel     text NOT NULL CHECK (channel IN ('log','whatsapp')),
  payload     jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX alerts_ts_desc
  ON alerts (ts DESC);

CREATE INDEX alerts_project_ts
  ON alerts (project, ts DESC);

CREATE INDEX alerts_severity_ts
  ON alerts (severity, ts DESC);
