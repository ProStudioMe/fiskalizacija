-- Pokreni na već postojećem volume-u (init-roles.sql se ne izvršava ponovo):
--   docker compose exec db psql -U sepko -d sepko -f /tmp/harden.sql
-- ili: psql postgresql://sepko:sepko@localhost:5433/sepko -f deploy/postgres/harden.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sepko_app') THEN
        CREATE USER sepko_app WITH PASSWORD 'sepko_app'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT LOGIN;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE sepko TO sepko_app;
GRANT USAGE ON SCHEMA public TO sepko_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sepko_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sepko_app;
ALTER DEFAULT PRIVILEGES FOR USER sepko IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sepko_app;
ALTER DEFAULT PRIVILEGES FOR USER sepko IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sepko_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM sepko_app;
-- Nema GRANT za DROP / CREATE TABLE / TRUNCATE schema
