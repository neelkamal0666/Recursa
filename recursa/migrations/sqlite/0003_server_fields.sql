-- Fields introduced with Recursa Server, kept here so version 3 means the same
-- logical schema on every backend. Unused by the local store.
ALTER TABLE runs ADD COLUMN received_at REAL;  -- when the server accepted the run
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, scopes_json TEXT NOT NULL, secret_hash TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL, revoked_at REAL, last_used_at REAL);
