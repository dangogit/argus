-- Circuit breaker for engine outages (usage limit, auth failure, missing
-- binary). One row per engine. open_until in the future = workers must not
-- call this engine; they release jobs until then instead. First successful
-- run deletes the row (breaker closes).
CREATE TABLE IF NOT EXISTS engine_breaker (
    engine     text PRIMARY KEY,
    open_until timestamptz NOT NULL,
    reason     text NOT NULL DEFAULT '',
    trip_count int NOT NULL DEFAULT 1,
    opened_at  timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
