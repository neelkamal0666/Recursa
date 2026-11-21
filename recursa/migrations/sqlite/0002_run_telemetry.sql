-- Whether a run's cost and latency were measured by Recursa or reported by the agent.
ALTER TABLE runs ADD COLUMN telemetry TEXT;
