# Recursa v1

*Agents that get better every run.* Recursa wraps an existing LLM agent, records and scores its runs, proposes system-prompt changes from its failures, and promotes a change only when it beats the current version on a sealed evaluation set **and** a named human approves it.

This is the v1 described in [the design document](https://github.com/neelkamal0666/Recursa/blob/main/Self-Learning%20Agent%20Framework%20—%20Design%20Document.md): one adapter, one candidate type (system prompt), deterministic primary metric, sealed gate and confirmation sets, authenticated human approval, whole-version promote and rollback.

## Quickstart

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
bash examples/triage/demo.sh      # full loop on the reference agent, offline, about 10 seconds
pytest -q                         # 22 tests, including one or more per acceptance criterion
```

Step by step (from `examples/triage/`):

```bash
python generate.py pool --n 800 --out pool.jsonl
recursa init --reviewer alice         # creates v1 from the agent's current prompt
recursa data split pool.jsonl         # train/dev open; gate/confirm encrypted; deletes pool.jsonl
recursa auth add-reviewer alice       # sets alice's passphrase (alice must be in reviewers.yaml)
recursa eval --split dev              # benchmark the current version
recursa cycle                         # reflect -> propose -> dev -> gate -> confirm -> review queue
recursa review list
recursa review show <candidate>       # diff, CIs, guardrails, insights, side-by-side cases, packet hash
recursa auth login alice              # 15-minute signed token
recursa approve <candidate> --packet <hash prefix>   # or: reject / request-changes --reason ...
recursa rollback v1 --reason "..."    # one command; re-run `recursa eval` to verify
recursa ui                            # local review dashboard on 127.0.0.1:8765
recursa audit --verify                # hash-chained audit log
```

## Wrapping your own agent

```python
import recursa as rc

@rc.tool(capability="read_only")                          # every tool declares a capability class
async def lookup_order(order_id): ...

@rc.tool(capability="irreversible", mock=lambda amount: {"refunded": amount})
async def refund(amount): ...                              # in eval mode the mock runs, never the real call

client = rc.clients.AsyncAnthropic()                     # Recursa measures tokens, cost and latency itself

async def my_agent(task: dict, ctx: rc.Context):
    resp = await client.messages.create(model="claude-opus-5-5", max_tokens=1024,
                                        system=ctx.system_prompt,
                                        messages=[{"role": "user", "content": task["ticket"]}])
    return parse(resp)

def build() -> rc.Project:                                 # referenced from recursa.yaml as factory: module:build
    agent = rc.wrap(my_agent, name="support-bot", surfaces={"system_prompt": PROMPT},
                    model={"id": "claude-opus-5-5"}, tools=[lookup_order, refund])
    suite = rc.EvalSuite(primary=rc.evals.FieldsMatch(["category", "priority"]),
                         safety=[rc.evals.NoPIIEcho()])
    return rc.Project(agent=agent, suite=suite)
```

**Telemetry is measured, not reported.** Model calls made through `rc.clients` are recorded by Recursa from the API response and its own clock. The cost and latency guardrails trust only those. If an agent reports its own usage through `rc.trace.model(...)` (for example, a simulator or a provider without a wrapper), its runs are marked `self_reported` and the gate fails the cost and latency guardrails. The exception is a project that sets `gate.allow_self_reported_telemetry: true`, as the simulated reference agent does; the review packet says so either way. Because the agent shares Recursa's process, this catches mistakes, not deliberately hostile agent code.

Labelled data is JSONL: `{"id", "input": {...}, "expected": {...}, "group", "tags"}`. `group` clusters correlated examples (same customer, template or source) for the bootstrap.

## How v1 meets the acceptance criteria

| Criterion | How | Test |
| --- | --- | --- |
| Reproducible benchmarks | Each eval run gets a deterministic `ctx.seed` per (example, sample); results are cached by (config hash, example, sample, suite version) | `test_benchmark_is_reproducible` |
| A cycle produces a candidate that passes gate and confirmation | `recursa cycle`: isolated optimizer → dev screening → sealed gate → one-time confirmation set | `test_cycle_yields_eligible_candidate` |
| Gain ≥ minimum, lower bound > 0 | Paired comparison, 3 samples per example, cluster bootstrap over groups, one-sided bound at α/budget (Bonferroni) | same |
| No guardrail regression | Cost and p95 latency: one-sided non-inferiority tests with Holm correction; safety: zero regressions | same, plus `test_compare_*` |
| Authenticated approval bound to the candidate | Passphrase (scrypt) → signed 15-minute token; reviewer must be listed for the surface; approval records the hash of the exact review packet; quorum from `reviewers.yaml` | `test_promotion_requires_authenticated_bound_approval` |
| One-command rollback, verified by eval | `recursa rollback vN` restores the full pinned config (prompt, model, tools, suite and dataset versions) | `test_rollback_restores_previous_version` |
| Optimizer cannot read gate/confirmation data | See below | `test_isolation.py` |

### Sealed data and optimizer isolation

- Gate and confirmation sets are encrypted at rest (AES-256-GCM). The key sits in `.recursa/keys/gate.key` (mode 0600, directory 0700). `recursa data split` deletes the plaintext pool by default.
- The optimizer (reflection, prompt rewriting, dev evaluation) runs in a child process, `python -m recursa.worker`. It receives only train and dev data on stdin, gets an environment with every `RECURSA_*` variable removed, and installs a CPython audit hook before importing any user code. Audit hooks can't be removed once installed. The hook refuses to open, list, glob, link, chmod, or connect via SQLite to anything under `sealed/`, `keys/`, `auth/`, or the main store. It also refuses to spawn subprocesses or load native code through ctypes. The test runs 17 escape attempts, and all of them are denied.
- The optimizer never learns gate outcomes, not even pass or fail. Everything derived from sealed data (gate runs, reports, review packets) lives in `.recursa/sealed/gate.db`. Human rejections reach the optimizer as text, so it stops proposing the same change.
- Gate budget: each gate-set version allows 20 candidate evaluations, and each candidate is evaluated at most once. Rotate with `recursa data rotate gate fresh.jsonl`, which retires the old version into dev. The confirmation set is used once per promotion decision and then retired into dev. Seal a fresh one with `recursa data seal confirm file.jsonl`.
- **Limitation:** the audit hook is defence in depth inside one OS user. For a hard boundary in production, run the gate under a separate OS user that alone owns `sealed/` and `keys/`.

### Other guarantees

- Runs, scores, feedback, versions, deployments, approvals and the audit log are append-only (SQLite triggers abort UPDATE and DELETE). The audit log is hash-chained.
- PII redaction runs before persistence and fails closed: if it errors, the run is kept without its payload. Review packets are redacted too.
- In eval mode, only `read_only` tools run live. Every other capability class must provide a mock; otherwise the run fails closed with `SandboxViolation`.
- Retries are attempts nested inside one run id. Feedback is idempotent per key. Online traces go to an fsync'd write-ahead log first (`learner.flush()` ingests them).
- A candidate goes stale after 7 days or when the baseline changes, and must then pass the gate again. "Request changes" with an edited prompt creates a new human-sourced candidate that must also pass the gate.

## Schema and upgrades

The store's schema lives in numbered, forward-only SQL migrations under [recursa/migrations/sqlite/](https://github.com/neelkamal0666/Recursa/tree/main/recursa/migrations/sqlite). A local store migrates itself when opened. Set `store.auto_migrate: false` to make it refuse an out-of-date database instead, and migrate explicitly:

```bash
recursa db status    # current/latest/pending for the records and sealed databases; exit 1 if anything is pending
recursa db migrate   # apply pending migrations in one transaction
```

A store also refuses to open a database newer than the installed Recursa, or one whose applied migrations no longer match the shipped files.

## Reference agent

[examples/triage/](https://github.com/neelkamal0666/Recursa/tree/main/examples/triage) is a support-ticket triage agent. It returns `{"category", "priority"}` and succeeds when both fields match the label. The design doc left the reference agent as an open decision, so this one is a stand-in:

- `TRIAGE_BACKEND=sim` (default) uses a seeded simulated model. It follows the prompt's guidance and its learned rules, misses cues the prompt doesn't mention, and makes 3% seeded random errors. This lets the whole loop be verified offline at no cost.
- `TRIAGE_BACKEND=claude` calls `claude-opus-5-5` at low effort, with server-side refusal fallbacks enabled, through the Anthropic SDK.
- The labelled data is synthetic (`generate.py`). Real v1 validation needs at least 400 human-labelled tasks from the chosen production agent. Synthetic data that the optimizer could regenerate is not a sealed set in any meaningful sense.

Typical result: the dev success rate goes from 0.61 to 0.71 in the first cycle. On the gate set it goes from 0.56 to 0.71 (lower bound +0.09 at α=0.0025), with a cost ratio of about 1.08 and no safety regressions.

## Optimizer strategies

- `heuristic` (default): `BatchErrorAnalysis` clusters dev failures by (field, expected, got) and finds a phrase that is common in the cluster and rare elsewhere. `RuleRewriter` then appends one compact rule per insight and proposes nested candidates (top 1, 2, 3, 4 rules). Dev screening picks the best one that stays within the cost guardrail.
- `claude`: `ClaudeRewriter` sends the prompt, failure modes, failure samples and past rejections to `claude-opus-5-5` and asks for a minimal rewrite. Set `optimizer.strategy: claude` and provide Anthropic credentials.
- Plugins: set `optimizer.plugin: module:Class` to supply any class with `propose(...)`.

## Deferred (per the v1 scope table)

Other adapters (LangGraph, Agent SDK, MCP), Postgres, OTel export, calibrated judges in the gate (`rc.evals.Judge` exists but is advisory only), few-shot/memory/tool candidates, failure mining and synthetic growth, shadow and canary rollout, Slack/GitHub approval channels, and OIDC (v1 uses local passphrase accounts; the token format is the integration seam).

## Layout

```
recursa/
  adapter.py   wrap(), Agent, Context: the v1 adapter contract
  trace.py     rc.trace.model(), @rc.tool(capability, mock): instrumentation + sandbox
  store.py     SQLite store: append-only tables, hash-chained audit log
  data.py      splits, sealing (AES-GCM), rotation
  evals.py     evaluators, suites, cached eval runner
  stats.py     paired cluster bootstrap, Holm, gate decision
  reflect.py   batch error analysis -> insights
  optimize.py  prompt rewriters (rules, Claude)
  worker.py    isolated optimizer process
  isolation.py audit-hook read isolation
  learner.py   cycle, gate, confirmation, packets, approval, promotion, rollback
  auth.py      reviewers.yaml, passphrases, signed tokens
  cli.py, ui.py
```
