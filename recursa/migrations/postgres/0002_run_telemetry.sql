-- Whether a run's cost and latency were measured by Recursa or reported by the agent.
ALTER TABLE recursa.runs ADD COLUMN telemetry text;
ALTER TABLE recursa_sealed.runs ADD COLUMN telemetry text;
