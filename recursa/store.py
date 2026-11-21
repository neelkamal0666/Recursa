"""SQLite store. Runs, scores, feedback, versions, approvals and the audit log are
append-only: triggers abort any UPDATE or DELETE on them."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import asdict
from typing import Any, Iterator

from .redact import RedactionError, Redactor
from .types import Candidate, Run, Score, Step, canonical_json, content_hash, new_id, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, agent TEXT, agent_version TEXT, mode TEXT, example_id TEXT, sample INTEGER,
  status TEXT, input_json TEXT, output_json TEXT, steps_json TEXT, cost_usd REAL, latency_ms REAL,
  started_at REAL, context_json TEXT, error TEXT, redaction TEXT, telemetry TEXT);
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
"""

IMMUTABLE = ["runs", "scores", "feedback", "eval_cache", "versions", "deployments", "approvals", "audit",
             "gate_ledger", "packets", "insights"]


class Store:
    def __init__(self, path: str, redactor: Redactor | None = None):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.redactor = redactor or Redactor()
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "telemetry" not in cols:  # databases created before telemetry was recorded
            self.db.execute("ALTER TABLE runs ADD COLUMN telemetry TEXT")
        for t in IMMUTABLE:
            for op in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                    f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")

    def close(self) -> None:
        self.db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self.db.execute(sql, args)
            cur.row_factory = sqlite3.Row
            return cur.fetchall()

    def _x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self.db.execute(sql, args)

    # -- runs -------------------------------------------------------------------
    def put_run(self, run: Run) -> None:
        try:
            inp = self.redactor(run.input)
            out = self.redactor(run.output)
            steps = self.redactor([asdict(s) for s in run.steps])
            ctx = self.redactor(run.context_snapshot)
            err = self.redactor(run.error) if run.error else None
            redaction = "applied"
        except RedactionError:
            # Fail closed: keep the run's metrics, never its raw payload.
            inp = out = ctx = None
            steps = [{k: v for k, v in asdict(s).items() if k not in ("input", "output")} for s in run.steps]
            err, redaction = "redaction failed; payload dropped", "unredacted_dropped"
        self._x(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.id, run.agent, run.agent_version, run.mode, run.example_id, run.sample, run.status,
             json.dumps(inp, default=str), json.dumps(out, default=str), json.dumps(steps, default=str),
             run.cost_usd, run.latency_ms, run.started_at, json.dumps(ctx, default=str), err, redaction,
             run.telemetry))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        r = dict(rows[0])
        for k in ("input_json", "output_json", "steps_json", "context_json"):
            r[k[:-5]] = json.loads(r.pop(k)) if r[k] is not None else None
        return r

    def list_runs(self, mode: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT id FROM runs" + (" WHERE mode=?" if mode else "") + " ORDER BY started_at DESC LIMIT ?"
        args = (mode, limit) if mode else (limit,)
        return [self.get_run(r["id"]) for r in self._q(sql, args)]

    def put_scores(self, scores: list[Score]) -> None:
        for s in scores:
            self._x("INSERT OR IGNORE INTO scores VALUES (?,?,?,?,?,?)",
                    (s.run_id, s.evaluator, s.evaluator_version, s.value, s.rationale, s.tier))

    def scores_for(self, run_id: str) -> list[Score]:
        return [Score(**dict(r)) for r in self._q("SELECT * FROM scores WHERE run_id=?", (run_id,))]

    def cache_get(self, key: str) -> str | None:
        rows = self._q("SELECT run_id FROM eval_cache WHERE key=?", (key,))
        return rows[0]["run_id"] if rows else None

    def cache_put(self, key: str, run_id: str) -> None:
        self._x("INSERT OR IGNORE INTO eval_cache VALUES (?,?)", (key, run_id))

    # -- feedback ---------------------------------------------------------------
    def put_feedback(self, run_id: str, kind: str, value: float | None, correction: Any, author: str,
                     idempotency_key: str | None) -> str:
        key = idempotency_key or new_id("fbk")
        existing = self._q("SELECT id FROM feedback WHERE idempotency_key=?", (key,))
        if existing:
            return existing[0]["id"]
        if not self._q("SELECT 1 FROM runs WHERE id=?", (run_id,)):
            raise KeyError(f"unknown run {run_id}")
        fid = new_id("fb")
        self._x("INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)",
                (fid, run_id, kind, value, json.dumps(self.redactor(correction)), author, key, now()))
        return fid

    def feedback_for(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM feedback WHERE run_id=?", (run_id,))]

    # -- versions & deployments ----------------------------------------------------
    def put_version(self, parent: str | None, config: dict[str, Any], candidate_id: str | None,
                    approvers: list[str]) -> str:
        with self._lock:
            seq = (self._q("SELECT COALESCE(MAX(seq),0) AS m FROM versions")[0]["m"]) + 1
            vid = f"v{seq}"
            self._x("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                    (vid, seq, parent, canonical_json(config), content_hash(config), candidate_id,
                     json.dumps(approvers), now()))
            return vid

    def get_version(self, vid: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM versions WHERE id=?", (vid,))
        if not rows:
            return None
        r = dict(rows[0])
        r["config"] = json.loads(r.pop("config_json"))
        r["approvers"] = json.loads(r.pop("approvers_json"))
        return r

    def list_versions(self) -> list[dict[str, Any]]:
        return [self.get_version(r["id"]) for r in self._q("SELECT id FROM versions ORDER BY seq")]

    def deploy(self, version_id: str, action: str, actor: str, reason: str = "") -> None:
        self._x("INSERT INTO deployments (version_id, action, actor, reason, at) VALUES (?,?,?,?,?)",
                (version_id, action, actor, reason, now()))

    def current_version(self) -> str | None:
        rows = self._q("SELECT version_id FROM deployments ORDER BY seq DESC LIMIT 1")
        return rows[0]["version_id"] if rows else None

    def deployments(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM deployments ORDER BY seq")]

    # -- candidates ----------------------------------------------------------------
    def put_candidate(self, c: Candidate) -> None:
        self._x("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (c.id, c.surface, c.baseline_version, canonical_json(c.proposed), json.dumps(c.insight_ids),
                 c.status, c.created_at, c.source, c.parent_candidate, json.dumps(c.dev_report), c.rationale))

    def set_candidate_status(self, cid: str, status: str) -> None:
        self._x("UPDATE candidates SET status=? WHERE id=?", (status, cid))

    def get_candidate(self, cid: str) -> Candidate | None:
        rows = self._q("SELECT * FROM candidates WHERE id=?", (cid,))
        if not rows:
            return None
        r = dict(rows[0])
        return Candidate(id=r["id"], surface=r["surface"], baseline_version=r["baseline_version"],
                         proposed=json.loads(r["proposed_json"]), insight_ids=json.loads(r["insight_ids_json"]),
                         status=r["status"], created_at=r["created_at"], source=r["source"],
                         parent_candidate=r["parent_candidate"], dev_report=json.loads(r["dev_report_json"]),
                         rationale=r["rationale"])

    def list_candidates(self, status: str | None = None) -> list[Candidate]:
        sql = "SELECT id FROM candidates" + (" WHERE status=?" if status else "") + " ORDER BY created_at"
        return [self.get_candidate(r["id"]) for r in self._q(sql, (status,) if status else ())]

    def put_insight(self, cycle_id: str, insight: dict[str, Any]) -> None:
        self._x("INSERT OR IGNORE INTO insights VALUES (?,?,?,?)",
                (insight["id"], cycle_id, json.dumps(insight), now()))

    def get_insights(self, ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for i in ids:
            rows = self._q("SELECT json FROM insights WHERE id=?", (i,))
            if rows:
                out.append(json.loads(rows[0]["json"]))
        return out

    # -- approvals & audit -------------------------------------------------------------
    def put_approval(self, candidate_id: str, reviewer: str, decision: str, reason: str, channel: str,
                     packet_hash: str) -> str:
        aid = new_id("apr")
        self._x("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?)",
                (aid, candidate_id, reviewer, decision, reason, channel, packet_hash, now()))
        return aid

    def approvals_for(self, candidate_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM approvals WHERE candidate_id=? ORDER BY at",
                                         (candidate_id,))]

    def audit(self, actor: str, action: str, subject: str, details: dict[str, Any] | None = None) -> None:
        """Hash-chained audit log: each record commits to the one before it."""
        with self._lock:
            prev = self._q("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")
            prev_hash = prev[0]["hash"] if prev else ""
            at = now()
            body = {"at": at, "actor": actor, "action": action, "subject": subject, "details": details or {},
                    "prev": prev_hash}
            self._x("INSERT INTO audit (at, actor, action, subject, details_json, prev_hash, hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (at, actor, action, subject, canonical_json(details or {}), prev_hash, content_hash(body)))

    def audit_log(self) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._q("SELECT * FROM audit ORDER BY seq")]
        for r in rows:
            r["details"] = json.loads(r.pop("details_json"))
        return rows

    def verify_audit_chain(self) -> bool:
        prev = ""
        for r in self.audit_log():
            body = {"at": r["at"], "actor": r["actor"], "action": r["action"], "subject": r["subject"],
                    "details": r["details"], "prev": prev}
            if r["prev_hash"] != prev or r["hash"] != content_hash(body):
                return False
            prev = r["hash"]
        return True

    # -- cycles (checkpoints) -----------------------------------------------------------
    def save_cycle(self, cycle_id: str, state: dict[str, Any]) -> None:
        self._x("INSERT INTO cycles VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, "
                "updated_at=excluded.updated_at", (cycle_id, json.dumps(state, default=str), now()))

    def load_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT state_json FROM cycles WHERE id=?", (cycle_id,))
        return json.loads(rows[0]["state_json"]) if rows else None

    # -- sealed-side tables ---------------------------------------------------------------
    def ledger_add(self, gate_version: str, candidate_id: str, kind: str) -> None:
        self._x("INSERT INTO gate_ledger VALUES (?,?,?,?)", (gate_version, candidate_id, kind, now()))

    def ledger_count(self, gate_version: str, kind: str) -> int:
        return self._q("SELECT COUNT(*) AS n FROM gate_ledger WHERE gate_version=? AND kind=?",
                       (gate_version, kind))[0]["n"]

    def ledger_has(self, gate_version: str, candidate_id: str, kind: str) -> bool:
        return bool(self._q("SELECT 1 FROM gate_ledger WHERE gate_version=? AND candidate_id=? AND kind=?",
                            (gate_version, candidate_id, kind)))

    def put_packet(self, candidate_id: str, packet: dict[str, Any]) -> str:
        h = content_hash(packet)
        self._x("INSERT OR IGNORE INTO packets VALUES (?,?,?,?)", (candidate_id, h, canonical_json(packet), now()))
        return h

    def latest_packet(self, candidate_id: str) -> tuple[str, dict[str, Any]] | None:
        rows = self._q("SELECT hash, json FROM packets WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1",
                       (candidate_id,))
        return (rows[0]["hash"], json.loads(rows[0]["json"])) if rows else None


def steps_from_dicts(steps: list[dict[str, Any]]) -> list[Step]:
    return [Step(**s) for s in steps]


def iter_rows(store: Store, sql: str, args: tuple = ()) -> Iterator[dict[str, Any]]:
    for r in store._q(sql, args):
        yield dict(r)
