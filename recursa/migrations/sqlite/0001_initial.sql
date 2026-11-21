-- Initial schema (Recursa 0.1.0). IF NOT EXISTS lets it complete databases created before
-- versioned migrations, which may be missing some tables.
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, agent TEXT, agent_version TEXT, mode TEXT, example_id TEXT, sample INTEGER,
  status TEXT, input_json TEXT, output_json TEXT, steps_json TEXT, cost_usd REAL, latency_ms REAL,
  started_at REAL, context_json TEXT, error TEXT, redaction TEXT);
CREATE TABLE IF NOT EXISTS scores (
  run_id TEXT, evaluator TEXT, evaluator_version TEXT, value REAL, rationale TEXT, tier TEXT,
  PRIMARY KEY (run_id, evaluator, evaluator_version));
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, value REAL, correction TEXT, author TEXT,
  idempotency_key TEXT UNIQUE, created_at REAL);
CREATE TABLE IF NOT EXISTS eval_cache (key TEXT PRIMARY KEY, run_id TEXT);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE, parent TEXT, config_json TEXT, config_hash TEXT,
  candidate_id TEXT, approvers_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS deployments (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT, action TEXT, actor TEXT, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, surface TEXT, baseline_version TEXT, proposed_json TEXT, insight_ids_json TEXT,
  status TEXT, created_at REAL, source TEXT, parent_candidate TEXT, dev_report_json TEXT, rationale TEXT);
CREATE TABLE IF NOT EXISTS insights (id TEXT PRIMARY KEY, cycle_id TEXT, json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, candidate_id TEXT, reviewer TEXT, decision TEXT, reason TEXT, channel TEXT,
  packet_hash TEXT, at REAL);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, actor TEXT, action TEXT, subject TEXT, details_json TEXT,
  prev_hash TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, state_json TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS gate_ledger (gate_version TEXT, candidate_id TEXT, kind TEXT, at REAL,
  PRIMARY KEY (gate_version, candidate_id, kind));
CREATE TABLE IF NOT EXISTS packets (candidate_id TEXT, hash TEXT PRIMARY KEY, json TEXT, created_at REAL);

-- Append-only: records are evidence, so they can never be changed or removed.
CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS scores_no_update BEFORE UPDATE ON scores BEGIN SELECT RAISE(ABORT, 'scores is append-only'); END;
CREATE TRIGGER IF NOT EXISTS scores_no_delete BEFORE DELETE ON scores BEGIN SELECT RAISE(ABORT, 'scores is append-only'); END;
CREATE TRIGGER IF NOT EXISTS feedback_no_update BEFORE UPDATE ON feedback BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END;
CREATE TRIGGER IF NOT EXISTS feedback_no_delete BEFORE DELETE ON feedback BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END;
CREATE TRIGGER IF NOT EXISTS eval_cache_no_update BEFORE UPDATE ON eval_cache BEGIN SELECT RAISE(ABORT, 'eval_cache is append-only'); END;
CREATE TRIGGER IF NOT EXISTS eval_cache_no_delete BEFORE DELETE ON eval_cache BEGIN SELECT RAISE(ABORT, 'eval_cache is append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT, 'versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT, 'versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS deployments_no_update BEFORE UPDATE ON deployments BEGIN SELECT RAISE(ABORT, 'deployments is append-only'); END;
CREATE TRIGGER IF NOT EXISTS deployments_no_delete BEFORE DELETE ON deployments BEGIN SELECT RAISE(ABORT, 'deployments is append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals BEGIN SELECT RAISE(ABORT, 'approvals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT, 'approvals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS gate_ledger_no_update BEFORE UPDATE ON gate_ledger BEGIN SELECT RAISE(ABORT, 'gate_ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS gate_ledger_no_delete BEFORE DELETE ON gate_ledger BEGIN SELECT RAISE(ABORT, 'gate_ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS packets_no_update BEFORE UPDATE ON packets BEGIN SELECT RAISE(ABORT, 'packets is append-only'); END;
CREATE TRIGGER IF NOT EXISTS packets_no_delete BEFORE DELETE ON packets BEGIN SELECT RAISE(ABORT, 'packets is append-only'); END;
CREATE TRIGGER IF NOT EXISTS insights_no_update BEFORE UPDATE ON insights BEGIN SELECT RAISE(ABORT, 'insights is append-only'); END;
CREATE TRIGGER IF NOT EXISTS insights_no_delete BEFORE DELETE ON insights BEGIN SELECT RAISE(ABORT, 'insights is append-only'); END;
