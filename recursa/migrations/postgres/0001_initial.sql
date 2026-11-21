-- Initial Postgres schema: the same logical schema as sqlite/0001.
-- Two schemas with different owners of trust:
--   recursa         records: runs, versions, candidates, approvals, audit, ...
--   recursa_sealed  the gate side: runs on sealed examples, gate ledger, packets.
-- Times are epoch seconds (double precision) so they round-trip exactly through the
-- store interface. Hash-committed content (audit details, packets) is stored as text
-- so its bytes never change; other JSON is jsonb.
CREATE SCHEMA IF NOT EXISTS recursa;
CREATE SCHEMA IF NOT EXISTS recursa_sealed;
CREATE TABLE recursa.runs (
  id text PRIMARY KEY, agent text, agent_version text, mode text, example_id text, sample integer,
  status text, input_json jsonb, output_json jsonb, steps_json jsonb, cost_usd double precision,
  latency_ms double precision, started_at double precision, context_json jsonb, error text, redaction text);
CREATE TABLE recursa.scores (
  run_id text, evaluator text, evaluator_version text, value double precision, rationale text, tier text,
  PRIMARY KEY (run_id, evaluator, evaluator_version));
CREATE TABLE recursa.feedback (
  id text PRIMARY KEY, run_id text, kind text, value double precision, correction text, author text,
  idempotency_key text UNIQUE, created_at double precision);
CREATE TABLE recursa.eval_cache (
  key text PRIMARY KEY, run_id text);
CREATE TABLE recursa.versions (
  id text PRIMARY KEY, seq integer UNIQUE, parent text, config_json jsonb, config_hash text,
  candidate_id text, approvers_json jsonb, created_at double precision);
CREATE TABLE recursa.deployments (
  seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, version_id text, action text, actor text,
  reason text, at double precision);
CREATE TABLE recursa.candidates (
  id text PRIMARY KEY, surface text, baseline_version text, proposed_json jsonb, insight_ids_json jsonb,
  status text, created_at double precision, source text, parent_candidate text, dev_report_json jsonb, rationale text);
CREATE TABLE recursa.insights (
  id text PRIMARY KEY, cycle_id text, json jsonb, created_at double precision);
CREATE TABLE recursa.approvals (
  id text PRIMARY KEY, candidate_id text, reviewer text, decision text, reason text, channel text,
  packet_hash text, at double precision);
CREATE TABLE recursa.audit (
  seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, at double precision, actor text, action text,
  subject text, details_json text, prev_hash text, hash text);
CREATE TABLE recursa.cycles (
  id text PRIMARY KEY, state_json jsonb, updated_at double precision);
CREATE TABLE recursa_sealed.runs (
  id text PRIMARY KEY, agent text, agent_version text, mode text, example_id text, sample integer,
  status text, input_json jsonb, output_json jsonb, steps_json jsonb, cost_usd double precision,
  latency_ms double precision, started_at double precision, context_json jsonb, error text, redaction text);
CREATE TABLE recursa_sealed.scores (
  run_id text, evaluator text, evaluator_version text, value double precision, rationale text, tier text,
  PRIMARY KEY (run_id, evaluator, evaluator_version));
CREATE TABLE recursa_sealed.eval_cache (
  key text PRIMARY KEY, run_id text);
CREATE TABLE recursa_sealed.cycles (
  id text PRIMARY KEY, state_json jsonb, updated_at double precision);
CREATE TABLE recursa_sealed.gate_ledger (
  gate_version text, candidate_id text, kind text, at double precision,
  PRIMARY KEY (gate_version, candidate_id, kind));
CREATE TABLE recursa_sealed.packets (
  candidate_id text, hash text PRIMARY KEY, json text, created_at double precision);
CREATE INDEX ON recursa.runs (agent_version, started_at);
CREATE INDEX ON recursa.runs (mode, started_at);
CREATE INDEX ON recursa.runs (example_id);
CREATE INDEX ON recursa.scores (run_id);
CREATE INDEX ON recursa.feedback (run_id);
CREATE INDEX ON recursa.approvals (candidate_id);
CREATE INDEX ON recursa.candidates (status, created_at);
CREATE INDEX ON recursa_sealed.packets (candidate_id, created_at);

-- Append-only: records are evidence, so they can never be changed or removed.
CREATE TRIGGER runs_append_only BEFORE UPDATE OR DELETE ON recursa.runs FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER runs_no_truncate BEFORE TRUNCATE ON recursa.runs FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER scores_append_only BEFORE UPDATE OR DELETE ON recursa.scores FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER scores_no_truncate BEFORE TRUNCATE ON recursa.scores FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER feedback_append_only BEFORE UPDATE OR DELETE ON recursa.feedback FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER feedback_no_truncate BEFORE TRUNCATE ON recursa.feedback FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER eval_cache_append_only BEFORE UPDATE OR DELETE ON recursa.eval_cache FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER eval_cache_no_truncate BEFORE TRUNCATE ON recursa.eval_cache FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER versions_append_only BEFORE UPDATE OR DELETE ON recursa.versions FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER versions_no_truncate BEFORE TRUNCATE ON recursa.versions FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER deployments_append_only BEFORE UPDATE OR DELETE ON recursa.deployments FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER deployments_no_truncate BEFORE TRUNCATE ON recursa.deployments FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER approvals_append_only BEFORE UPDATE OR DELETE ON recursa.approvals FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER approvals_no_truncate BEFORE TRUNCATE ON recursa.approvals FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON recursa.audit FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON recursa.audit FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER insights_append_only BEFORE UPDATE OR DELETE ON recursa.insights FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER insights_no_truncate BEFORE TRUNCATE ON recursa.insights FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER runs_append_only BEFORE UPDATE OR DELETE ON recursa_sealed.runs FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER runs_no_truncate BEFORE TRUNCATE ON recursa_sealed.runs FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER scores_append_only BEFORE UPDATE OR DELETE ON recursa_sealed.scores FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER scores_no_truncate BEFORE TRUNCATE ON recursa_sealed.scores FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER eval_cache_append_only BEFORE UPDATE OR DELETE ON recursa_sealed.eval_cache FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER eval_cache_no_truncate BEFORE TRUNCATE ON recursa_sealed.eval_cache FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER gate_ledger_append_only BEFORE UPDATE OR DELETE ON recursa_sealed.gate_ledger FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER gate_ledger_no_truncate BEFORE TRUNCATE ON recursa_sealed.gate_ledger FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER packets_append_only BEFORE UPDATE OR DELETE ON recursa_sealed.packets FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
CREATE TRIGGER packets_no_truncate BEFORE TRUNCATE ON recursa_sealed.packets FOR EACH STATEMENT EXECUTE FUNCTION recursa.reject_change();
