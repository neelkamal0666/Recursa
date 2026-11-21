# Changelog

All notable changes to Recursa. Versions follow semantic versioning; before 1.0, minor versions may break compatibility and say how to upgrade.

## Unreleased

### Added: Recursa Server (preview; plan phase 2)

- `recursa[server]` extra and `recursa server run | migrate | status | keys create/list/revoke`. The server owns Postgres and serves the store protocols over an HTTP API (`/v1`, OpenAPI at `/v1/openapi.json`), with `/healthz` and `/readyz`. It refuses to start unless the schema is at this release's version.
- Postgres migrations (`recursa/migrations/postgres`), version-aligned with SQLite: `recursa` and `recursa_sealed` schemas, append-only triggers (including `TRUNCATE`), and `recursa_app` / `recursa_gate` group roles whose grants forbid updating or deleting records and keep the records role out of the sealed schema.
- API keys with scopes (`ingest`, `optimizer`, `gate`, `reviewer`, `admin`), stored as SHA-256 hashes and shown once. Every route declares its scopes; tests check each route against every scope.
- Client backend `HttpRecordStore` / `HttpSealedStore` (standard library only, keep-alive connections, retries for idempotent calls), selected by `store.url` and `store.key_env` (and optional `store.sealed_key_env`, `store.timeout`).
- Write-ahead log shipping: `Learner.flush()` ships runs in idempotent batches and keeps unshipped files for the next attempt; `Learner.start_shipping()` does it in the background with backoff. Serving continues on the last-known live version if the store is unreachable.
- `put_runs` batch method on the record store; sealed stores also hold runs, scores and the eval cache of gate evaluations (`EvalRunStore`).
- Dockerfile and `docker-compose.yml` for trying the server; CI builds the image and checks the compose quick start, and runs the Postgres and server tests against a Postgres service.

### Changed

- Migration `0003_server_fields` (both backends): `runs.received_at` and an `api_keys` table.
- Imported rows are re-redacted by the importing store.
- `store.url` is accepted (it was rejected in phase 1); API keys in the config file are rejected in favour of environment variables.

## Unreleased, continued: storage interface (plan phase 1)

### Added

- Storage interface: `RecordStore` and `SealedStore` protocols in `recursa/store/base.py`, with `open_store`, `open_sealed_store` and `open_scratch_store` factories. The learner, worker, evals and CLI use only these, so other backends (Recursa Server) can be added without touching them.
- Contract test suite (`tests/test_store_contract.py`) that every backend must pass.
- Pagination: `list_runs(before=...)`, `list_candidates(limit=..., after=...)`, `audit_log(after=..., limit=...)`.

### Changed

- The SQLite store is now `recursa.store.LocalStore`. `recursa.store.Store` remains as an alias.
- Writes are idempotent (first write wins): putting a run or candidate whose id exists is a no-op instead of an error, so clients can retry safely. `ledger_add` returns whether it charged the gate budget instead of raising on a repeat.
- `store.url` and `store.key_env` are rejected with a clear message until Recursa Server exists; unknown `store` settings are errors.

## 0.1.1 — 2026-11-21

### Added

- `rc.clients.Anthropic` and `rc.clients.AsyncAnthropic`: drop-in wrappers that record each model call's tokens, cost (including prompt-cache reads and writes) and latency from the API response and Recursa's own clock, priced at the model that actually served the request. Covers `messages.create`, `create(stream=True)` and `messages.stream(...)`, plus the `beta.messages` equivalents; streamed calls also record time to first event.
- Runs are labelled `measured` or `self_reported`. The gate fails the cost and latency guardrails when any run is self-reported, unless `gate.allow_self_reported_telemetry: true`. Review packets show which applies.
- Versioned, forward-only schema migrations with a checksummed `schema_migrations` ledger; `recursa db status` and `recursa db migrate`; `store.auto_migrate` setting.
- Gate verdict `insufficient_bootstrap` when too few bootstrap resamples are configured to reach significance, instead of silently rejecting.
- Optional live test (`tests/test_live.py`, needs `RECURSA_LIVE=1` and an API key).

### Changed

- Optimizer worker results are imported through the store (`Store.export_records` / `import_records`); no module outside the store and migrations uses SQL.

### Upgrading from 0.1.0

- Existing stores are upgraded automatically on first open (the `telemetry` column is added and the migration ledger created). With `store.auto_migrate: false`, run `recursa db migrate` first.
- Projects whose agents report usage through `rc.trace.model(...)` now fail the cost and latency guardrails. Switch model calls to `rc.clients`, or set `gate.allow_self_reported_telemetry: true` to keep the old behaviour.

## 0.1.0 — 2026-11-20

First release: eval-gated, human-approved system-prompt improvement for one agent, with sealed gate and confirmation sets, an isolated optimizer, authenticated approval and one-command rollback.
