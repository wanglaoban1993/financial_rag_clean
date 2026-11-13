CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.system_events (
    id bigserial PRIMARY KEY,
    event_type text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO app.system_events (event_type, detail)
VALUES ('stage1_postgres_initialized', '{"component":"postgres"}'::jsonb);
