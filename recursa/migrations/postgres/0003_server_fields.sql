-- Recursa Server: receive times, API keys, and the database roles that enforce
-- append-only storage and the sealed boundary at the database, not only in code.
ALTER TABLE recursa.runs ADD COLUMN received_at double precision;
ALTER TABLE recursa_sealed.runs ADD COLUMN received_at double precision;

CREATE TABLE recursa.api_keys (
  id text PRIMARY KEY, name text NOT NULL, scopes_json jsonb NOT NULL, secret_hash text NOT NULL,
  created_at double precision NOT NULL, expires_at double precision, revoked_at double precision,
  last_used_at double precision);

-- Group roles (NOLOGIN). The operator creates login roles and grants membership, e.g.
--   CREATE ROLE recursa_server LOGIN PASSWORD '...' IN ROLE recursa_app;
--   CREATE ROLE recursa_server_gate LOGIN PASSWORD '...' IN ROLE recursa_gate;
-- The role running migrations owns the schema and needs CREATEROLE only if these
-- roles do not exist yet.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'recursa_app') THEN CREATE ROLE recursa_app NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'recursa_gate') THEN CREATE ROLE recursa_gate NOLOGIN; END IF;
END $$;

REVOKE ALL ON SCHEMA recursa, recursa_sealed FROM PUBLIC;

-- recursa_app: the server's records connection. Read and append; update only mutable
-- workflow state; never touch the sealed schema.
GRANT USAGE ON SCHEMA recursa TO recursa_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA recursa TO recursa_app;
REVOKE INSERT ON recursa.schema_migrations, recursa.api_keys FROM recursa_app;
GRANT UPDATE ON recursa.candidates, recursa.cycles TO recursa_app;
GRANT UPDATE (last_used_at) ON recursa.api_keys TO recursa_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA recursa TO recursa_app;
GRANT EXECUTE ON FUNCTION recursa.reject_change() TO recursa_app, recursa_gate;

-- recursa_gate: the server's sealed connection. It may read the schema version and
-- API keys (to start and authenticate) but no other records.
GRANT USAGE ON SCHEMA recursa TO recursa_gate;
GRANT SELECT ON recursa.schema_migrations, recursa.api_keys TO recursa_gate;
GRANT USAGE ON SCHEMA recursa_sealed TO recursa_gate;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA recursa_sealed TO recursa_gate;
GRANT UPDATE ON recursa_sealed.cycles TO recursa_gate;
