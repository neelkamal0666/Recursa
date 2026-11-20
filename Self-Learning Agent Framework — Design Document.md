# Self-Learning Agent Framework — Design Document

Nov 20, 2026 · @Neel

## Name

Recommended name: **Recursa**. Tagline: *agents that get better every run.* It joins recursion (the loop feeds itself) and reflection, is short to type (`pip install recursa`, `import recursa as rc`), and the PyPI name was free when checked on 20 Nov 2026.

| Name | Why it fits | PyPI name free (20 Nov 2026) |
| --- | --- | --- |
| **Recursa** (recommended) | Recursion + reflection; a loop that improves itself | Yes |
| Loopwise | The loop gets wiser each turn; plain and friendly | Yes |
| Reflexa | Puts reflection, the core mechanism, up front | Yes |
| Autodidact | "One who teaches themselves"; very literal | `autodidact` taken, `autodidact-ai` free |
| Ouroboros | The snake eating its tail; memorable but reads as self-consuming | Taken |
| Kaizen | Continuous improvement; widely used already | Taken |

Open question: check GitHub org, npm and domain availability before committing to the name. The rest of this document uses Recursa.

## Overview

Recursa is an open-source Python library that turns any LLM agent into one that improves itself from its own runs, under evaluation gates, without retraining the model. It wraps an existing agent, records every run, scores it, learns from feedback and failures, and promotes only changes that beat the current version on a held-out golden dataset.

### Problem

Teams that ship agents today hand-roll the same pieces: trace logging, an eval script, a spreadsheet of golden examples, a prompt tweaked after each incident, and a memory store bolted on later. These pieces do not talk to each other, so lessons from production rarely reach the prompt, the memory or the test set, and regressions slip through because nothing gates a change.

### Goals

1. **One closed loop:** run → observe → evaluate → reflect → propose → validate → promote, as a first-class, resumable pipeline.
2. **Living golden dataset:** grows from production failures, human corrections and synthetic variants, with versioning, deduplication and a frozen holdout.
3. **Memory and context that learn:** episodic, semantic and procedural memory with write, consolidate and forget policies, plus a context builder that fits a token budget.
4. **Evals as the gate:** every learned change (prompt, memory, skill, tool policy, few-shot set) is a versioned candidate that must pass the eval gate before promotion.
5. **Framework-agnostic:** adapters for plain functions, LangGraph, OpenAI Agents SDK, Claude Agent SDK and MCP tools; any model provider.
6. **Safe by default:** budgets, mandatory human approval before any promotion, rollback, and audit of every learned change. Recursa suggests changes; it never promotes them on its own.

### Non-goals

- Training or fine-tuning model weights in v1 (exports fine-tuning datasets only; a trainer plugin is a later phase).
- Being an agent orchestration framework; Recursa wraps the agent you already have.
- Hosting a SaaS UI in v1; a local dashboard and OpenTelemetry export cover observability.

## v1 scope and acceptance criteria

v1 proves one claim and nothing more: on one reference agent, Recursa can propose a prompt change that a human approves and that improves a pre-declared primary metric on a sealed evaluation set, with no guardrail regression. Everything else in this document is the target architecture, built only after v1 meets its acceptance criteria.

| Area | In v1 | Deferred |
| --- | --- | --- |
| Agent integration | One adapter: async Python function + the `rc.trace` SDK for model and tool calls | LangGraph, Agent SDK and MCP adapters |
| Run records | Immutable runs, feedback and scores on SQLite | Postgres, object storage, OTel export |
| Evaluation | Deterministic evaluators as the primary metric; LLM judges advisory only | Calibrated judges in the gate |
| Candidate types | One: system prompt / instruction rewrite | Few-shots, procedural memory, tools, model settings |
| Evaluation sets | Hand-curated dev set + sealed gate and confirmation sets (see Promotion protocol) | Failure mining, synthetic examples, automated rotation |
| Human approval | CLI + local UI, authenticated, every promotion | Slack, GitHub and webhook channels; quorum rules |
| Rollout | Promote or roll back the whole version | Shadow and canary rollout, automatic rollback |
| Memory | None beyond the run store | Episodic, semantic and procedural memory, context engine |

**Reference agent and primary metric (decision needed):** pick one agent with a deterministic success check and enough tasks to fill dev, gate and confirmation sets (at least 400 labelled tasks). Candidates: an internal support or ops agent, or a tool-use benchmark such as tau-bench extended with extra tasks. The primary metric is task success rate as judged by that deterministic check.

**Acceptance criteria for v1**

- [ ] Two agent versions can be benchmarked reproducibly: re-running the same eval gives the same deterministic scores.
- [ ] One learning cycle produces at least one candidate that passes the gate and the confirmation set on the reference agent.
- [ ] That candidate improves the primary metric by at least the declared minimum practical gain, with the lower confidence bound above zero.
- [ ] No guardrail metric (safety checks, cost per run, p95 latency) regresses beyond its declared limit.
- [ ] Nothing is promoted without an authenticated human approval bound to the exact candidate, and the decision appears in the audit log.
- [ ] Rollback restores the previous version in one command, verified by re-running the eval.
- [ ] The optimizer process provably has no read access to gate or confirmation data (enforced by storage permissions, tested).

## Core concepts and the learning loop

Recursa runs two nested loops: a fast inner loop on every request that uses what the agent has learned, and a slower outer loop that turns evidence into new, validated agent versions.

&#91;embedded content: Recursa learning loop · inner run loop, outer learning cycle\]

The agent never edits itself mid-request; it only changes when the outer loop promotes a new version.

| Concept | Meaning |
| --- | --- |
| Run | One execution of the agent on one task, fully traced |
| Feedback | Any signal about a run: human, implicit, environment or evaluator |
| Golden dataset | Versioned examples with expected outputs or rubrics, split into train, dev and a sealed, versioned holdout (gate + confirmation sets) |
| Memory | What the agent recalls at run time: episodic, semantic and procedural |
| Insight | A named failure mode or lesson, backed by evidence runs |
| Candidate | A proposed change to one learnable surface (prompt, memory, few-shots, tools, config) |
| Agent version | An immutable, promoted bundle of all learnable surfaces; the unit of rollback |

## Architecture

Recursa has four layers: adapters around the user's agent, a runtime that serves every run, a learning layer that runs in cycles, and a data layer underneath; governance and telemetry span all four.

&#91;embedded content: Recursa architecture · 4 layers + cross-cutting concerns\]

The runtime and learning layers only meet through the registry: the runtime always serves the current promoted version, and the learning layer can only change it by promoting a new one.

## Component designs

Each component is a Python protocol with a default implementation, so teams can swap any piece without forking the loop.

### 1. Harness (`recursa.harness`)

The harness runs the agent and owns the loop. It has three modes that share one code path:

- **Online:** wraps production calls; records traces, collects feedback, never mutates the live agent mid-request.
- **Offline eval:** replays a dataset split against a candidate agent version, in parallel, with caching and seeded determinism where the provider allows it.
- **Learning cycle:** a scheduled or triggered job that runs reflect → propose → validate → promote, checkpointed so a crash resumes at the last step.

Key parts: `AgentAdapter` (wraps the user's agent behind `async run(task, context) -> Result`), `Sandbox` (tool calls in dry-run, mocked or live mode), `Budget` (tokens, dollars, wall-clock, tool calls per run and per cycle), and `Scheduler` (cron, every N runs, or on a failure-rate threshold).

#### Adapter contract and trace fidelity

"Any agent" means any agent that meets the adapter contract; what Recursa can learn depends on how much the adapter exposes. An adapter must provide:

- `run(task, context) -> Result`, async, returning the final output and a status.
- **Learnable surfaces** it can get and set by name (v1: the system prompt), so a candidate can be applied without code changes.
- **A tool registry** for any tool the agent calls, so the sandbox can enforce modes and capabilities. Tools called outside the registry are recorded as opaque and disable sandboxed evaluation for that agent.

| Fidelity level | How it is captured | What it enables | Limits |
| --- | --- | --- | --- |
| 0 · Black box | Input, output, timing only | Outcome evals, feedback | No trajectory evals, no tool sandbox, no cost per step |
| 1 · Instrumented (v1) | `rc.trace` wrappers or OTel auto-instrumentation of the model client and registered tools | Per-call cost and latency, trajectory evals, tool sandbox | Framework-internal state and planner decisions not visible |
| 2 · Native adapter (post-v1) | Framework hooks (for example LangGraph node events) | Agent state snapshots, resumable runs, step-level replay | Tied to one framework's version; adapters must be maintained |

**Streaming** is recorded as the final message plus chunk timings. **Retries** are separate attempts nested in one run, so the run id stays stable and feedback attaches to the run, not the attempt. **Provider-specific features** (cached tokens, tool-choice modes, reasoning traces) are stored as raw provider metadata on each call; evals may use them only through declared, versioned fields.

### 2. Tracing and run store (`recursa.trace`)

Every run becomes a `Trajectory`: input, context snapshot, each model call, each tool call with arguments and results, final output, cost and latency. Traces follow the OpenTelemetry GenAI semantic conventions so they also flow to existing observability tools. PII redaction runs before anything is persisted.

### 3. Memory (`recursa.memory`)

| Memory type | Holds | Written when | Retrieved by |
| --- | --- | --- | --- |
| Working | Scratchpad for the current run | During the run | Direct access |
| Episodic | Summaries of past runs and their outcomes | After each run | Similarity to the task + recency + outcome |
| Semantic | Facts and user or domain knowledge | Reflection extracts them | Vector + keyword hybrid search |
| Procedural | Lessons, rules, playbooks, learned skills | Reflection proposes, gate checks, human approves | Task-type tags + similarity |

Policies are pluggable: a **write policy** (what is worth storing, with a salience score), a **consolidation job** (merges duplicates, resolves contradictions, summarizes old episodes), and a **forgetting policy** (decay by age and low usefulness; hard delete on user request). Each memory item carries provenance (which run or human created it), a confidence score, and an observational usefulness score (see below). Procedural memory changes behaviour, so it is versioned and gated like a prompt change.

**Scope, authority and conflicts.**

- **Scope:** every memory item has a scope (global, tenant, user or session). Retrieval never crosses tenants, and user-scoped facts are never used for another user.
- **Authority order:** system and admin configuration > human-verified facts > facts from trusted systems of record > model-inferred facts > user-supplied or retrieved content. A narrower scope wins for preferences (a user's chosen language), but never overrides system policy.
- **Conflicts:** both facts are kept with timestamps and sources. At equal authority the newer fact wins at retrieval; at different authority the higher one wins. Unresolved conflicts are flagged for the consolidation job and, if they persist, for human review.
- **Staleness:** each type has a time-to-live, and facts past it are retrieved only with an "as of" date or not at all.
- **Untrusted content never becomes instructions.** User input, web pages and tool outputs can be stored as facts but can never become procedural memory directly; procedural memory changes only through a candidate and human approval. Stored content is rendered in the context as quoted data, never as instructions.

**Usefulness is observational.** The usefulness score records how often runs that retrieved an item succeeded; it shows correlation, not that the item helped. Causal estimates come from periodic ablation on dev: replaying runs with the item withheld and comparing outcomes. Forgetting decisions use ablation results where they exist and fall back to age and retrieval frequency otherwise.

### 4. Context engine (`recursa.context`)

Builds the prompt for each run from ranked sources: system instructions, active policy version, retrieved memories, few-shot examples chosen from the golden set, tool descriptions and conversation state. It packs to a token budget with per-source quotas, compresses long items, and logs exactly what went in, so evals can attribute a failure to missing or misleading context.

### 5. Golden dataset manager (`recursa.data`)

The dataset is versioned like code. Each `Example` has input, expected output or a rubric, tags, difficulty, source and lineage.

- **Growth sources:** production failures (low score or negative feedback), human corrections, edge cases found by reflection, and synthetic variants (paraphrase, perturbation, adversarial mutation) generated from existing examples.
- **Curation:** near-duplicate detection by embedding, a label-review queue for humans, and coverage reports by tag so gaps are visible.
- **Splits:** `train` (few-shot and reflection material), `dev` (used by the optimizer), and a frozen `holdout` that only the promotion gate may read (a sealed gate set plus a one-time confirmation set; see Promotion protocol), which stops the agent from overfitting to its own test set.
- **Exports:** JSONL for fine-tuning and preference pairs (chosen vs rejected) for future trainer plugins.

### 6. Evals (`recursa.evals`)

An `Evaluator` returns a score in 0–1 plus a rationale. Built-in evaluator kinds:

- **Deterministic:** exact match, regex, JSON schema, unit tests, tool-call correctness.
- **Model-graded:** LLM-as-judge with rubrics, pairwise comparison between versions, and a judge-calibration step against human labels.
- **Trajectory:** did it pick the right tools, in a sensible order, within the step and cost budget.
- **Safety:** policy violations, prompt-injection susceptibility, data leakage.

Suites combine evaluators with weights. Reports include confidence intervals and a significance test between candidate and baseline, so a 1-point gain on 30 examples is not mistaken for progress.

**Evidence trust model.** Recursa ranks evaluation evidence in three tiers and never lets a lower tier stand in for a higher one:

1. **Ground truth:** deterministic checks and human-verified labels. Only this tier can be the primary metric in v1.
2. **Calibrated judge scores:** an LLM judge counts in the gate (post-v1) only while its agreement with human labels on the current calibration set meets a threshold (default Cohen's kappa ≥ 0.6) and that calibration is less than 30 days old. Otherwise its scores are shown as advisory and excluded from the decision.
3. **Judge rationales:** explanations only. A rationale is never evidence for promotion, and an insight backed only by rationales is marked unverified in the review packet.

Every evaluator is versioned (judge model, prompt and rubric hash, code version), and every score records the evaluator version that produced it, so scores from different evaluator versions are never compared.

### 7. Feedback (`recursa.feedback`)

One schema for every signal: explicit (thumbs, ratings, free-text, corrected output), implicit (retry, abandon, edit distance between agent output and what the user kept), environment (tests passed, task completed) and evaluator scores. Feedback attaches to a run id and flows to three places: the golden-set candidate queue, the reflection engine, and memory usefulness counters.

### 8. Reflection engine (`recursa.reflect`)

Turns evidence into hypotheses. Strategies, selectable per agent:

- **Per-run critique** (Reflexion-style): a short verbal lesson after a failed run, stored in episodic memory and reused on similar tasks.
- **Batch error analysis:** clusters failures from a window of runs, names the failure modes, and ranks them by frequency × severity.
- **Contrastive reflection:** compares successful and failed runs of the same task type to extract what made the difference.
- **Skill extraction:** turns repeated successful tool sequences into a reusable procedural skill.

Output is a list of `Insight` objects with evidence links to the runs that support them; insights with no evidence are dropped.

### 9. Optimizer (`recursa.optimize`)

Turns insights into `Candidate` changes. Each candidate targets one surface: system prompt, procedural memory, few-shot selection, tool descriptions or routing, or model and parameter choice. Strategies are plugins: an LLM rewriter guided by insights, a DSPy- or GEPA-style prompt search, few-shot bootstrapping from the train split, and simple A/B of model settings. The optimizer may only read `train` and `dev`.

### 10. Promotion gate and registry (`recursa.registry`)

The registry versions every learnable artifact (prompts, memories, skills, configs) as an immutable `AgentVersion`. A candidate is promoted only when it beats the baseline on the holdout by a configured margin with statistical significance, does not regress any guarded metric (safety, cost, latency), and is approved by a human reviewer through the HITL pipeline below. Rollout supports shadow mode, canary percentage and instant rollback to any prior version.

#### Promotion protocol

**What "better" means.** Each agent declares, before any optimization starts, one primary metric, a minimum practical improvement, and guardrail metrics with a maximum allowed regression. A candidate is eligible only if the primary metric improves by at least the minimum and the lower bound of its confidence interval is above zero, and every guardrail passes a one-sided non-inferiority test. Weighted composite scores are reported but never used to decide; trade-offs (better quality, higher cost) go to the human reviewer, not to a formula.

| Setting | v1 default |
| --- | --- |
| Primary metric | Task success rate from the deterministic check |
| Minimum practical improvement | +2 percentage points absolute |
| Guardrails | Safety checks: zero regressions; cost per run: at most +10%; p95 latency: at most +15% |
| Significance | α = 0.05, adjusted for repeated attempts (below) |

**Sealed evaluation.** The labelled pool is split into four disjoint sets with separate storage permissions:

1. **Train:** few-shot and reflection material; the optimizer reads it freely.
2. **Dev:** the optimizer evaluates on it freely, per example.
3. **Gate set:** sealed. Only the gate process reads it. Each candidate is evaluated on it once, and the optimizer learns nothing from it, not even pass/fail per candidate. Each gate-set version has an evaluation budget (default 20 candidate evaluations); when it is spent the version retires to dev and a new one is drawn from fresh, human-labelled examples.
4. **Confirmation set:** used once per promotion decision, only for the finalist that passed the gate. Its result goes into the review packet. A failed confirmation sends the candidate back; the confirmation set is then retired.

**Versioned, not refreshed in place.** "Frozen" means each gate or confirmation set version is immutable. Rotation creates a new version; a candidate and its baseline are always compared on the same version, so the baseline is re-evaluated whenever the version changes.

**Statistics.**

- Comparisons are paired: candidate and baseline run on the same examples, with repeated samples per example where the model is non-deterministic (default 3).
- Confidence intervals come from a cluster bootstrap over example groups (same template, source or user), so correlated examples are not counted as independent evidence.
- The gate refuses to decide below a minimum sample size (default 150 gate examples).
- The primary metric is the only test of superiority; guardrails are tested for non-inferiority with a Holm correction across them.
- Repeated attempts spend an α budget: with a budget of 20 evaluations per gate-set version, each test runs at α/20 (Bonferroni). The review packet always states how many candidates were tried on that version.

**What a version contains.** An `AgentVersion` pins the full effective runtime configuration: model id and parameters, prompts, tool definitions and their capability manifests, context settings, the procedural and semantic memory snapshot, the evaluator suite version and the dataset versions used to gate it. Rollback restores all of these together. Episodic memory written after the rolled-back version stays, tagged with the version that wrote it, and can be quarantined in one command.

**Rollout (post-v1).** Shadow mode first: the candidate runs on mirrored traffic and its outputs are never shown. Then a canary percentage, compared with the baseline by a sequential test on the primary metric and guardrails. Automatic rollback fires on a guardrail breach, an error-rate spike or a significant primary-metric drop, and only ever to the last human-approved version.

### 11. Human-in-the-loop pipeline (`recursa.hitl`)

Recursa proposes and people decide: no candidate is ever promoted without explicit human approval, and v1 ships with no auto-promote mode. The gate only marks a candidate as eligible; the HITL pipeline takes it from there.

**Where humans come in**

| Touchpoint | What the human does | Required? |
| --- | --- | --- |
| Promotion review | Approves, rejects or requests changes to every candidate that passed the gate | Always |
| Golden set labelling | Confirms expected outputs or rubrics for mined failures and synthetic examples before they enter `dev` or `holdout` | Always |
| Judge calibration | Labels a sample of judged runs so LLM-judge agreement can be measured | Scheduled sample |
| Runtime escalation | Approves risky tool calls or low-confidence answers mid-run (optional per agent) | Configurable |
| Rollback | Triggers rollback; automatic rollback is allowed only to a version a human already approved | Optional |

**Review pipeline for a candidate**

1. **Eligible:** the candidate passes the holdout gate and moves to `awaiting_review`.
2. **Review packet:** Recursa builds a self-contained packet: the diff, the eval report vs baseline with confidence intervals, the insights and linked evidence runs, side-by-side baseline vs candidate outputs on sampled holdout cases, and cost and latency deltas.
3. **Routing:** reviewers are chosen by surface from `reviewers.yaml` (like CODEOWNERS), with a configurable quorum, for example two approvers for system-prompt changes. Packets go to the local UI, CLI, Slack, a GitHub pull request or a webhook.
4. **Decision:** approve, approve for shadow or canary only, request changes (the reviewer edits the candidate and it is re-gated), or reject with a reason.
5. **Record and learn:** every decision is written to the audit log with reviewer and reason. Rejections feed back into reflection, so the optimizer stops proposing the same change, and become preference pairs for later training.
6. **Expiry:** a candidate goes stale if the baseline changes or after a time limit (default 7 days); stale candidates must pass the gate again before review.

```python
hitl = rc.HITL(
    reviewers="reviewers.yaml",
    channels=[rc.channels.UI(), rc.channels.Slack("#agent-reviews")],
    quorum={"system_prompt": 2, "tools": 2, "default": 1},
    expire_after="7d",
)
```

## Data model and storage

Everything Recursa learns is an immutable, versioned record linked back to the runs that justified it, so any behaviour can be traced to its evidence and rolled back.

| Entity | Key fields | Links to |
| --- | --- | --- |
| `Run` / `Trajectory` | id, agent\_version, input, context\_snapshot, steps\[\], output, cost, latency, status | AgentVersion, Feedback, Score |
| `Feedback` | run\_id, kind (explicit, implicit, environment), value, correction, author | Run |
| `Score` | run\_id, evaluator, value 0–1, rationale, suite\_version | Run, EvalSuite |
| `Example` | id, input, expected or rubric, tags, split, source, lineage, dataset\_version | Run it came from |
| `MemoryItem` | id, type, content, embedding, provenance, confidence, usefulness, ttl | Runs, Insights |
| `Insight` | id, statement, failure\_mode, evidence\_run\_ids\[\], severity | Runs |
| `Candidate` | id, surface, diff, insight\_ids\[\], status | Insights, EvalReport |
| `EvalReport` | candidate\_id, baseline\_version, per-metric scores, CI, p-value, verdict | Candidate |
| `AgentVersion` | id, parent, prompt, memory snapshot ref, skills, config, promoted\_at, approver | Candidate |

**Storage backends** are pluggable behind one `Store` interface:

- **Local default:** SQLite for records + a local vector index (LanceDB or sqlite-vec) + files for artifacts. Zero setup, good for development.
- **Production:** Postgres with pgvector, or Postgres + an external vector DB (Qdrant, Pinecone); object storage (S3, GCS) for traces and dataset snapshots.
- **Datasets** snapshot to Parquet with a content hash, so an eval report always names the exact data it ran on.

## Public API and developer experience

A developer should get a self-improving agent in under 20 lines, then customize one component at a time.

```python
import recursa as rc

# 1. Wrap the agent you already have
agent = rc.wrap(my_agent_fn, name="support-bot")

# 2. Declare how to judge it
suite = rc.EvalSuite([
    rc.evals.JsonSchema(TicketSchema),
    rc.evals.Judge(rubric="Resolves the issue politely and correctly", weight=2),
    rc.evals.Trajectory(max_steps=8),
])

# 3. Build the learner
learner = rc.Learner(
    agent=agent,
    evals=suite,
    dataset=rc.Dataset.load("golden/support.jsonl"),
    memory=rc.Memory(store="sqlite:///recursa.db"),
    reflect=rc.reflect.BatchErrorAnalysis(window=200),
    optimize=[rc.optimize.PromptRewrite(), rc.optimize.FewShotBootstrap(k=4)],
    gate=rc.Gate(min_gain=0.02, significance=0.05, guard=["safety", "cost"]),
    hitl=rc.HITL(reviewers="reviewers.yaml"),   # every promotion needs a human
)

# 4. Serve with learning on
result = await learner.run(task)          # traced, memory-aware
learner.feedback(result.run_id, thumbs=-1, correction="...")
await learner.cycle()                     # proposes candidates for review; schedule with rc.every("6h")
```

**CLI:**

- `recursa init` scaffolds config, a sample golden set and an eval suite.
- `recursa eval --version current --split dev` runs a suite and prints a report.
- `recursa cycle` runs one learning cycle; `recursa diff v12 v13` shows what changed and why.
- `recursa promote <candidate>` / `recursa rollback v12` to record a human decision; recursa review lists candidates awaiting approval and opens their review packets.
- `recursa ui` opens a local dashboard of runs, scores, insights and versions.

**Extension points:** every component is a protocol (`Evaluator`, `Reflector`, `Optimizer`, `MemoryStore`, `ContextSource`, `AgentAdapter`, `Store`). Plugins register through Python entry points, so `pip install recursa-langgraph` adds an adapter with no code change. Config lives in `recursa.yaml` and can be overridden in code.

**Async-first:** the core is `asyncio`, with sync wrappers. A TypeScript client for tracing and feedback is a later phase.

### Runtime and operational semantics

- **Trace writes:** `learner.run()` appends its trace to a local write-ahead log and returns; a background flusher writes to the store. Serving never waits on the store.
- **Store unavailable:** the log buffers up to a size limit, then follows a configured policy: fail-open (keep serving, drop traces, count the drops) or fail-closed (refuse runs). The default is fail-open in production and fail-closed in evaluation, where a missing trace would corrupt results.
- **Feedback durability:** `learner.feedback()` is acknowledged only after the write-ahead log syncs to disk, and takes an idempotency key so retried calls do not double-count.
- **Cancellation:** a cancelled run is closed with status `cancelled`, keeps its partial steps, and is excluded from metrics by default.
- **Retries:** see the adapter contract: attempts nest inside one run id.
- **Approval identity:** the UI authenticates reviewers through the organization's identity provider (OIDC or SSO); the CLI uses a short-lived token tied to the same identity; chat and GitHub approvals count only from accounts mapped to a reviewer in `reviewers.yaml`. Each approval record holds the reviewer, time, channel and the hash of the review packet, so an approval binds to the exact candidate and evaluation it saw and cannot be reused for a changed one.

## Safety, guardrails and governance

A self-modifying agent can learn the wrong lesson, so no learned change reaches users without passing the gate, and every change is reversible.

| Risk | Guardrail |
| --- | --- |
| Reward hacking: the agent learns to please the judge, not the user | Judges calibrated against human labels; frozen holdout; mixed deterministic and model-graded metrics; periodic human audit sample |
| Overfitting to the golden set | Optimizer never sees holdout; sealed gate and confirmation sets with an evaluation budget, rotated as new versions; significance test on gains |
| Memory poisoning (prompt injection stored as a "lesson") | Untrusted content tagged at ingestion; procedural memory writes gated; injection evals in the safety suite |
| Drift and silent regression | Guarded metrics must not regress; shadow and canary rollout; automatic rollback on live score drop |
| Runaway cost | Per-run and per-cycle budgets; cycle aborts when budget is spent |
| Privacy | Fail-closed redaction, per-tenant isolation and a defined deletion contract (see Data, privacy and deletion) |
| Unreviewed behaviour change | Every promotion requires human approval, on every surface; Recursa suggests but never auto-promotes |

Every promotion writes an audit record: who or what approved it, the eval report, the insights and runs behind it, and the diff.

### Tool execution boundary

Candidate evaluation can never touch production systems, because evaluation runs never hold production credentials.

- **Capability manifest:** every registered tool declares a class: read-only, reversible write, external side effect, or irreversible or financial. The class decides what each mode may do.
- **Modes by environment:** in evaluation, read-only tools may run live against allow-listed endpoints; every write or side-effecting tool runs against a mock, a recorded fixture (record and replay) or a dedicated sandbox environment. In production, irreversible or financial tools need runtime escalation to a human when the agent's policy says so.
- **Credentials:** injected per call from a secret provider (environment, Vault or a cloud secret manager), scoped per environment, and never written to traces, memory or datasets. Known secret values are masked by exact match before anything is persisted.
- **Isolation:** each evaluation runs in its own namespace: separate store tables, separate memory snapshot, separate credentials, and a network egress allow-list. A write that reaches a non-sandbox endpoint fails closed and fails the run.
- **Evals that need real tools:** declared explicitly per suite, limited to read-only tools, and listed in the review packet so reviewers know which results depend on live systems.

### Data, privacy and deletion

- **Redaction:** built-in detectors (emails, phone numbers, card numbers, government ids) plus custom patterns and classifiers, configured per field in `recursa.yaml`. Redaction fails closed: if the redactor errors, the raw payload is not stored and the run is marked as unredacted-dropped.
- **What redaction misses:** a secret scanner runs on every persisted record; hits are quarantined and alerted. Teams can set fields to hash-only or never-store.
- **Subject tagging:** every run, feedback item, memory item, example and embedding carries the subject ids (user, tenant) it came from.
- **Deletion contract:** a delete request writes a tombstone at once, so the data is excluded from retrieval, evaluation and exports immediately. Primary records, embeddings and caches are hard-deleted within a configurable window (default 30 days). Dataset snapshots that contain the subject are re-materialized as new versions and the old versions purged. Backups age out on their own retention cycle and deleted ids are re-applied on any restore.
- **Exports:** every export is logged with destination and dataset version. Data already exported outside Recursa cannot be recalled; the deletion receipt lists those exports so owners can act.
- **Retention defaults:** raw traces 90 days, aggregates and audit log kept, all configurable per tenant.

## Observability and metrics

The headline question the dashboard answers is: is the agent getting better, and why?

| Metric | What it shows |
| --- | --- |
| Task success rate (live and holdout) | Whether learning is working, per agent version |
| Score by eval dimension | Which quality axis moved |
| Cost and latency per run | Whether gains came at an unacceptable price |
| Learning velocity | Candidates proposed, approved, rejected and rolled back per cycle; review queue age |
| Golden set size and tag coverage | Whether the test set keeps pace with production traffic |
| Memory hit rate and usefulness | Whether retrieved memories correlate with success (observational; ablation for causal estimates) |
| Top failure modes | Ranked clusters from batch error analysis, with trend |
| Judge agreement with humans | Whether the evals can still be trusted |

Traces export over OpenTelemetry, so teams can view them in Langfuse, Arize Phoenix, Datadog or any OTel backend alongside the built-in local UI.

## Roadmap, risks and open questions

### Phases

Phases 1 and 2 together form v1, cut down to the v1 scope above; phases 3 to 5 start only after v1 meets its acceptance criteria.

1. **Foundation:** adapter, tracing, run store, eval suites with deterministic and judge evaluators, dataset versioning, CLI `eval`. Exit: a team can benchmark two agent versions reproducibly.
2. **Closed loop:** feedback API, batch error analysis, prompt-rewrite optimizer (few-shot bootstrap moves to phase 3), promotion gate, HITL review queue and review packets, registry with rollback. Exit: one full learning cycle improves holdout score on a reference agent where the only human step is approving the change.
3. **Memory and context:** four memory types, consolidation and forgetting jobs, context engine with budgets, memory usefulness tracking. Exit: measurable gain from memory on a multi-session benchmark.
4. **Golden set growth:** failure mining, synthetic variants, review queue, coverage reports, holdout refresh. Exit: dataset grows from production with under 5% duplicate rate on review.
5. **Scale and ecosystem:** framework adapters, Postgres and vector backends, OTel export, local UI, canary rollout, skill extraction. Later: trainer plugin for fine-tuning and RL from exported preference pairs.

### Risks

- **Judge cost dominates:** model-graded evals on every run can cost more than the agent. Mitigation: sample live runs, cache judgments, use small judges with periodic calibration.
- **Too many knobs:** a powerful framework can be hard to adopt. Mitigation: opinionated defaults and a 20-line quickstart.
- **Noisy gains:** small datasets make improvements look real when they are not. Mitigation: confidence intervals and a minimum holdout size before promotion is allowed.

### Open questions

- Should the default reflection model be the agent's own model or a separate, stronger "teacher" model?
- Single-agent first, or support multi-agent systems where each role learns separately?
- Decided 20 Nov 2026: every promotion requires human approval in v1; Recursa suggests but never auto-promotes (see HITL pipeline).
- Licence: Apache-2.0 or MIT?
- Which reference agent and primary metric does v1 use? It needs a deterministic success check and at least 400 labelled tasks (see v1 scope).
