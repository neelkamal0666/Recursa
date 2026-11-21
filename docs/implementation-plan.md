# Recursa implementation plan

This plan takes Recursa from the shipped v1 (embedded library, SQLite, local review UI) to the full design in [the design document](../Self-Learning%20Agent%20Framework%20—%20Design%20Document.md): Recursa Server, admin console, MCP, memory, golden-set growth and ecosystem integrations.

Each phase lists its goal, the work, how it is tested, the exit criteria that must hold before the next dependent phase starts, and the main risks. Durations are rough single-engineer estimates for planning only; re-estimate at the start of each phase.

## Where we are (Nov 2026)

Done and on `main`:

- **v1 loop:** adapter and tracing, append-only SQLite run store with a hash-chained audit log, deterministic primary metric, dev/gate/confirmation splits with sealed (encrypted) gate and confirmation sets, system-prompt optimizer in an isolated subprocess, statistical gate with guardrails, authenticated approval (CLI and local UI), promote and rollback.
- **Measured telemetry:** `rc.clients` wrappers measure model cost and latency; the gate distrusts self-reported numbers unless a project opts in.
- **Versioned migrations:** numbered SQLite migrations, `recursa db status` and `recursa db migrate`.
- **Release:** 0.1.0 on PyPI via trusted publishing. Telemetry and migrations are not released yet.

Known gaps carried into this plan:

- ~~`rc.clients` has only been tested against a fake SDK client, and does not measure `stream=True` calls.~~ Streaming done and tested against the real SDK classes in phase 0; one live run against the API is still to do.
- ~~The learner writes worker results with raw SQL, bypassing the store's methods.~~ Fixed in phase 0.
- All trust boundaries are enforced in one process (library code plus an audit-hook sandbox).

## Phase overview

| Phase | Name | Depends on | Rough size | Release |
| --- | --- | --- | --- | --- |
| 0 | Stabilise and release 0.1.1 | none | 1 week | 0.1.1 |
| 1 | Store interface | 0 | 1.5 weeks | 0.2.0 |
| 2 | Recursa Server core | 1 | 4 weeks | 0.3.0 |
| 3 | Server-side governance and gate worker | 2 | 3 weeks | 0.4.0 |
| 4 | Admin console: reviews and operations | 3 | 4 weeks | 0.5.0 |
| 5 | Admin console: settings and administration | 4 | 3 weeks | 0.6.0 |
| 6 | MCP interface | 3 | 1.5 weeks | 0.7.0 |
| 7 | Model proxy (server-measured telemetry) | 3 | 2 weeks | 0.8.0 |
| 8 | Memory and context | 2 (server) or 1 (embedded only) | 6 weeks | 0.9.0 |
| 9 | Golden-set growth | 3 | 4 weeks | 0.10.0 |
| 10 | Ecosystem and more candidate surfaces | 3 | ongoing | 1.0 when 0–9 are done |

Phases 6, 7 and 9 can run in parallel with 4–5 once phase 3 is done.

```
0 ─ 1 ─ 2 ─ 3 ─┬─ 4 ─ 5
               ├─ 6
               ├─ 7
               ├─ 9
               └─ 10
        2 ─ 8 (memory; can start after 1 for embedded-only)
```

Cross-cutting rules for every phase:

- Every phase ends with a tagged release through the existing publish workflow, and `examples/triage/demo.sh` must pass in CI.
- Every schema change is a new numbered migration for **every** backend that exists at the time.
- Every trust-boundary claim gets a test that tries to break it (the pattern of `tests/test_isolation.py`).
- Docs (README and design doc) are updated in the same change as the code.

---

## Phase 0: Stabilise and release 0.1.1

**Goal:** ship what is on `main` and close the gaps that would undermine later phases.

**Work**

1. **Real-API check of `rc.clients`.** Run the triage agent with `TRIAGE_BACKEND=claude` on a small sample (about 20 examples, needs an API key and a small budget). Confirm tokens, cost, served model after fallback, and refusal handling are recorded correctly. Add a test marked `live` that is skipped in CI unless a key is present.
2. **Streaming support in `rc.clients`.** Wrap `messages.stream(...)` and `create(stream=True)`: time to first token and total latency from Recursa's clock; usage from the final message. Remove the `NotImplementedError`.
3. **Remove the raw-SQL leak.** Replace `recursa/learner.py:306-310` with store methods (`put_run_row`/`put_score_rows`, or reuse `put_run`/`put_scores`) so phase 1 has a clean boundary.
4. **Release 0.1.1:** bump version, changelog (new `CHANGELOG.md`), GitHub release, verify on PyPI.

**Tests:** streaming wrapper tests with a fake stream; existing 34 tests; live test run once manually.

**Exit criteria**

- 0.1.1 installable from PyPI; `recursa db status` works on a fresh install and on a 0.1.0 database.
- A real Claude run produces measured telemetry that matches the Console usage figures within rounding.
- No module outside `recursa/store.py` and `recursa/migrations/` touches SQL.

**Risks:** pricing table drift (`recursa/llm.py` `PRICES`). Mitigation: unpriced models already mark runs as self-reported; add a test that every model id used in examples is priced.

---

## Phase 1: Store interface

**Goal:** the learner, CLI, UI and worker depend on an interface, not on SQLite, so a remote store can be swapped in without touching them.

**Work**

1. **Define `recursa/store/` package:**
   - `base.py`: a `Store` `Protocol` with the 33 public methods the current store exposes, grouped as runs and scores, eval cache, feedback, versions and deployments, candidates, insights, approvals, audit, cycles, gate ledger, packets. Typed with the existing dataclasses from `recursa/types.py`.
   - `local.py`: today's SQLite code, renamed `LocalStore`.
   - `__init__.py`: `open_store(config)` factory: no `store.url` gives `LocalStore`; `store.url` gives `HttpStore` (phase 2).
   - Keep `from recursa.store import Store` working for existing users.
2. **Split the sealed side.** Today the sealed database is a second `Store`. Introduce a narrower `SealedStore` protocol (gate ledger, sealed examples, gate results) so phase 3 can give it its own credentials.
3. **Contract test suite.** `tests/store_contract.py`: one parametrised suite every backend must pass, covering append-only behaviour, idempotent feedback, audit-chain verification, version sequencing, ledger counting and redaction failing closed. Run it against `LocalStore` now.
4. **Config:** `store:` section accepts `auto_migrate`, and reserves `url` and `key_env` (rejected with a clear message until phase 2).

**Tests:** contract suite; all existing tests unchanged.

**Exit criteria**

- `Learner`, `cli.py`, `ui.py` and `worker.py` import only the protocol and the factory.
- Contract suite passes on `LocalStore`.
- No behaviour change visible to users (demo output identical apart from ids and times).

**Risks:** the protocol freezes method shapes that do not suit HTTP (for example methods that return iterators over every run). Mitigation: add pagination (`cursor`, `limit`) to list methods now.

---

## Phase 2: Recursa Server core

**Goal:** a deployable server that owns Postgres, and an SDK client that uses it, with no change to agent code beyond configuration.

**Packaging and stack**

- New optional extra `recursa[server]`, so the base library stays light: an ASGI framework (Starlette or FastAPI), `uvicorn`, `psycopg` 3 with its connection pool. Decide between Starlette and FastAPI at the start of the phase; the plan assumes FastAPI for its generated OpenAPI schema.
- Entry point `recursa server --config server.yaml`.
- Docker image (`ghcr.io/<org>/recursa-server`) built in CI on release; a `docker-compose.yml` with Postgres for local trials.

**Work**

1. **Postgres migrations** in `recursa/migrations/postgres/`, numbered to match SQLite (`0001_initial.sql`, `0002_run_telemetry.sql`), with `jsonb`, `timestamptz`, identity columns and the indexes from the design doc. Extend `recursa/migrations/__init__.py` with a backend driver (SQLite and psycopg) behind the same `migrate`/`status` functions, using an advisory lock instead of `BEGIN IMMEDIATE`.
2. **Roles.** `0003_roles.sql` (both backends where meaningful): `recursa_migrator`, `recursa_app`, `recursa_gate`, with grants: `recursa_app` gets `SELECT, INSERT` on append-only tables and `UPDATE` only on `candidates` and `cycles`. Sealed tables live in a separate `sealed` schema granted only to `recursa_gate`.
3. **`PostgresStore`** implementing the phase 1 protocol, used only inside the server. Must pass the contract suite (run in CI against a Postgres service container).
4. **HTTP API v1** (`/v1/...`): resources mirroring the protocol: runs (`POST /v1/runs:batch`), scores, feedback, versions, deployments, candidates, insights, approvals, audit, cycles, packets. JSON bodies use the existing dataclass shapes; OpenAPI published at `/v1/openapi.json`.
5. **API keys.** `api_keys` table (hashed with a per-key salt; secret shown once), scopes `ingest`, `optimizer`, `gate`, `reviewer`, `admin`. Middleware checks scope per route. CLI: `recursa server keys create --scope ingest --name prod-agent`.
6. **`HttpStore` client** implementing the protocol over HTTP with retries, timeouts and idempotency keys. Selected by `store: {url: ..., key_env: RECURSA_API_KEY}`.
7. **Ingest from the WAL.** The SDK appends runs to the existing local write-ahead log; a background shipper sends batches to `POST /v1/runs:batch`, idempotent by run id; backoff when the server is down; a cap on WAL size with a loud warning (never silently drop). Server stamps `received_at`, redacts PII (fails closed) and appends the audit entry.
8. **Health:** `/healthz` (process up), `/readyz` (database reachable and schema up to date; server refuses to serve writes on a schema mismatch).

**Tests**

- Contract suite against `PostgresStore` and against `HttpStore` (client talking to an in-process server).
- Scope tests: every route rejects every scope it should not accept (generated table-driven test).
- Grant tests: connect as `recursa_app` and confirm `UPDATE`/`DELETE` on append-only tables fail at the database, not only in the server.
- WAL shipping: server down, agent keeps running, runs arrive once after recovery.
- Demo runs end to end in server mode in CI (`demo.sh --server`).

**Exit criteria**

- The triage demo produces the same verdicts in embedded and server modes for the same seed.
- An `ingest` key cannot read anything; an `optimizer` key cannot reach any sealed endpoint (tested).
- `docker compose up` gives a working server and `recursa db status` against it reports up to date.

**Risks**

- Scope creep in the API surface. Mitigation: the API mirrors the store protocol 1:1 in this phase; nicer resource design waits for console needs in phase 4.
- Performance of large step traces in `jsonb`. Mitigation: measure with 100k runs; if needed move `steps` to object storage with a content hash in the row (design doc already allows this).

---

## Phase 3: Server-side governance and gate worker

**Goal:** the rules that decide promotion are enforced by the server, so local code cannot bypass them.

**Work**

1. **Gate worker protocol.**
   - `recursa gate-worker --key-env RECURSA_GATE_KEY` runs in the operator's environment with the agent's code.
   - It claims a gate job (`POST /v1/gate/jobs:claim`), pulls sealed examples for that job only, runs baseline and candidate, and posts per-example, per-sample results.
   - The server computes statistics and the verdict using the existing `recursa/stats.py` (moved into a module both sides import), charges the gate budget ledger in the same transaction, and records the gate settings version used.
2. **Confirmation set flow** moved server-side the same way, including retiring the confirmation set after one decision.
3. **Approvals server-side.** Reviewer passphrase login moves to the server (scrypt hashes in the database, not files); approval tokens are server sessions. The server checks the surface, packet hash, quorum, staleness (baseline changed or 7 days) and that the approver did not propose or edit the candidate.
4. **Audit appends server-side only.** Clients can no longer write audit entries; the server writes them for every state change. `recursa audit --verify` works against the server.
5. **Optimizer runs with an `optimizer` key.** `recursa cycle` in server mode fetches train/dev runs through the API, runs the existing subprocess-isolated optimizer, and posts candidates. Keep the audit-hook sandbox as defence in depth.
6. **Promote and rollback** become server operations; the SDK's agent loads the live version from the server (cached, with a configurable refresh interval).

**Tests**

- Port `tests/test_acceptance.py` to run in server mode: full cycle, gate, confirmation, approval, promote, rollback verified by eval.
- Adversarial tests: a client with an `optimizer` key tries to read sealed examples, post gate results, approve, or write audit entries: all rejected and audited.
- Budget: concurrent gate jobs cannot overspend (two workers racing for the last budget unit).
- Tampering: a gate worker posting results for examples not in its job is rejected.

**Exit criteria**

- Every v1 acceptance criterion holds in server mode with the guarantees enforced by the server.
- No client-side code path can change promotion state without the server's checks (reviewed against the route table).

**Risks:** a malicious gate worker could post fabricated results. Mitigation for now: gate keys are operator-held and audited; record the worker identity and code version on every result; later option (open question in the design doc) to run pure-configuration agents inside the server.

---

## Phase 4: Admin console — reviews and operations

**Goal:** replace the local review UI with a console served by Recursa Server that covers everything reviewers and operators do day to day.

**Stack**

- Single-page app in `console/` (TypeScript, React, Vite), built in CI and shipped as static files inside the server wheel and image. It calls only the public `/v1` API.
- Typed API client generated from the server's OpenAPI schema, so the console and API cannot drift.

**Work**

1. **Sign-in:** OIDC (authorisation code with PKCE) against the organisation's identity provider; local passphrase accounts for small installs. HttpOnly SameSite cookies, CSRF tokens, session timeout, strict CSP.
2. **Roles:** viewer, reviewer, operator, admin, mapped to API scopes; reviewer surfaces and quorum from the server.
3. **Pages:** overview; review queue and review packet (diff, gate and confirmation results with confidence bounds, guardrails, telemetry source, example cases side by side; approve, reject, request changes with an editor that re-gates the edit); versions and deployments with diff and rollback; cycles and candidates; runs and eval reports (redacted); audit log with "verify chain"; server and database status (read-only in this phase).
4. **Step-up re-authentication** for approve and rollback (MFA via the identity provider where supported).
5. **Retire `recursa ui`** in server mode; keep it for embedded mode.

**Tests**

- Component tests for the packet view (the side-by-side bug fixed in v1 must have a test here too).
- End-to-end browser tests (Playwright) for: login, approve with step-up, request changes, rollback, audit verify.
- Security tests: CSRF rejected, cookies flags set, CSP present, no sealed data reachable from any page.

**Exit criteria**

- A reviewer can complete the full approve flow from the console with no CLI use.
- Accessibility: keyboard-only approve flow and WCAG AA contrast.

**Risks:** front-end scope grows. Mitigation: build the v1 UI's features first; anything else goes to phase 5 or later.

---

## Phase 5: Admin console — settings and administration

**Goal:** administer the server from the console safely.

**Work**

1. **Users and roles:** invite, deactivate, assign roles and reviewer surfaces; separation-of-duties checks (admins and operators cannot approve unless also reviewers for that surface).
2. **API keys:** create with scope and expiry, rotate, revoke; secret shown once; last-used time.
3. **Governed settings:** gate settings, quorum rules and reviewer assignments are edited as a proposed diff, approved by a second admin, and take effect as a new settings version recorded in the audit log. Gate decisions already record the settings version (phase 3).
4. **Datasets:** seal and rotate gate and confirmation sets (upload), view split sizes and gate budget; sealed contents never displayed.
5. **Database operations:** connection status (no secrets), schema version, pending migrations, run migrations (uses migrator credentials; operator role plus step-up), table sizes, ingest backlog, support bundle download. Connection details stay in environment variables or a secret manager.
6. **Setup mode:** a fresh server with no admin accepts a one-time token printed to its log, checks the database, applies migrations, creates the first admin and then disables setup.
7. **Retention and PII settings** (governed like gate settings).

**Tests:** two-admin approval cannot be completed by one person with two sessions; setup mode cannot be re-entered once an admin exists; key secrets never appear in logs or API responses after creation.

**Exit criteria:** an operator can run a production install (keys, users, datasets, migrations) without shell access to the server apart from the initial deploy.

**Risks:** settings sprawl. Mitigation: every setting needs a documented owner and a default; no setting without a test.

---

## Phase 6: MCP interface

**Goal:** agents and assistants can use Recursa through MCP, with the same keys and audit as the API.

**Work**

1. MCP endpoint on the server (streamable HTTP transport) using the official Python MCP SDK.
2. Tools for agents: `feedback.submit`; memory tools are added in phase 8.
3. Tools for reviewers working through an assistant: `review.list`, `review.show`, `versions.diff`, `audit.verify`. No approve, reject or rollback tools: those require the reviewer's own interactive login.
4. Every MCP call authorised by API key scope and audited like an API call.

**Tests:** scope enforcement per tool; tool outputs redacted; an MCP client cannot reach any write path apart from feedback.

**Exit criteria:** Claude (or any MCP client) configured with a reviewer key can list and explain pending candidates; with an ingest key can submit feedback; nothing else.

---

## Phase 7: Model proxy (server-measured telemetry)

**Goal:** cost and latency are measured by the server, so agent code cannot misreport its own usage.

**Work**

1. Optional proxy endpoint compatible with the Anthropic Messages API (`/v1/proxy/anthropic/v1/messages`), forwarding with the operator's provider key held by the server.
2. The SDK's `rc.clients` gains `via_server=True`, pointing the Anthropic client's base URL at the proxy and tagging requests with the run id.
3. The server attaches measured steps to the run; runs are labelled `measured` by the server, and the gate trusts only server-measured telemetry when `gate.require_server_telemetry` is set.
4. Streaming pass-through with usage capture.

**Tests:** proxy faithfully forwards requests and responses (including fallbacks and refusals); a client reporting different usage than the proxy measured is flagged; latency overhead measured and documented.

**Exit criteria:** with the proxy on, cost and latency guardrails depend only on server-side measurements.

**Risks:** the proxy becomes a latency and availability dependency. Mitigation: it is opt-in, horizontally scalable, and the SDK can fall back to direct calls (marked self-reported) if allowed by settings.

---

## Phase 8: Memory and context

Corresponds to design-doc phase 3. Can start after phase 1 for embedded mode; the server parts need phase 2.

**Goal:** agents improve between prompt versions through governed memory, with a measured gain.

**Work**

1. **Memory types** from the design doc (working, episodic, semantic, procedural), stored as `MemoryItem` with scope (global, tenant, user, session), provenance, authority, confidence, usefulness counters and TTL. Write policy with salience scoring; procedural memory changes only through a candidate and human approval; untrusted content is stored as quoted data, never as instructions.
2. **Vector index:** sqlite-vec in embedded mode; pgvector in server mode, behind a `VectorIndex` interface with a contract test suite.
3. **Context engine:** retrieval with a token budget per source, deduplication, recency and usefulness weighting.
4. **Consolidation and forgetting jobs:** merge duplicates, resolve conflicts by authority then recency, flag persistent conflicts for review, decay unused items (using dev ablation where available), honour deletion requests (cascade to derived items).
5. **Memory as a candidate surface:** memory snapshots are part of an agent version; promotions that change procedural memory go through the gate like prompt changes.
6. **MCP tools:** `memory.search`, `memory.record` (phase 6 endpoint).

**Tests:** retrieval never crosses tenants or users; deletion cascades; tool output or user text cannot become procedural memory; gate cannot see memory derived from gate examples (no leakage from sealed sets into memory).

**Exit criteria:** measurable gain from memory on a multi-session benchmark, passing the same gate rules as prompt changes.

**Risks:** memory leaks sealed data into training. Mitigation: provenance on every item; items derived from gate or confirmation runs are rejected at write time (tested).

---

## Phase 9: Golden-set growth

Corresponds to design-doc phase 4.

**Goal:** the evaluation data grows from production without contaminating sealed sets.

**Work**

1. **Failure mining:** cluster failed and low-feedback production runs into candidate examples.
2. **Synthetic variants** of mined examples (Claude-generated, labelled as synthetic, never placed in sealed sets without human labelling).
3. **Labelling queue** in the console: accept, correct, reject; duplicate detection.
4. **Coverage reports:** tags and failure modes covered per split.
5. **Holdout refresh:** periodic sealing of fresh gate and confirmation sets from labelled examples, using the existing rotate flow; old sets retire into dev.

**Tests:** an example can never be in more than one split (group-hash check); synthetic examples are excluded from sealed sets unless human-labelled.

**Exit criteria:** dataset grows from production with under 5% duplicates on review (design-doc target).

---

## Phase 10: Ecosystem and more candidate surfaces

Corresponds to design-doc phase 5 (remaining items). Ongoing; pick by user demand.

- **Framework adapters:** LangGraph, Claude Agent SDK, OpenAI-compatible agent frameworks, each mapping the framework's events to Recursa trace steps.
- **OpenTelemetry export** of traces and gate decisions.
- **More candidate surfaces:** few-shot examples, tool descriptions, model and effort settings; each with its own reviewer surface and gate rules.
- **Canary rollout:** serve a promoted version to a share of traffic and auto-roll-back on live guardrail breaches (rollback still audited; promotion still requires approval).
- **Skill extraction:** recurring successful procedures proposed as reusable skills, reviewed like any candidate.
- **Trainer plugin** (later): export preference pairs for fine-tuning.

---

## Release and versioning policy

- Semantic versioning; pre-1.0 minor versions may include breaking changes, always listed in `CHANGELOG.md` with an upgrade note.
- Migrations are forward-only and run before the new server version serves traffic. A release never requires editing an old migration.
- The HTTP API is versioned (`/v1`); breaking changes go to `/v2` with at least one minor release of overlap.
- Embedded mode stays supported and tested in CI for every release.

## Decisions needed before or during the phases

| When | Decision |
| --- | --- |
| Phase 2 start | FastAPI or Starlette; container registry for the server image |
| Phase 2 start | Whether `PostgresStore` may also be used directly in embedded mode (design doc currently says server-only) |
| Phase 3 | Whether pure-configuration agents can be gated inside the server (removes the operator-run gate worker for them) |
| Phase 4 start | Console framework (plan assumes React and Vite) and which identity providers to test against |
| Phase 7 | Whether to support providers other than Anthropic in the proxy |
| Phase 8 start | Default reflection model (agent's own or a stronger "teacher"), an open question in the design doc |
