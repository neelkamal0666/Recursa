"""LocalStore: the embedded SQLite backend, implementing both RecordStore and
SealedStore (see base.py for the semantics). Append-only tables are also guarded by
triggers that abort any UPDATE or DELETE. The schema lives in versioned migrations
(recursa/migrations/sqlite); see recursa.migrations."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from .. import migrations
from ..redact import Redactor
from ..types import Candidate, Run, Score, canonical_json, content_hash, new_id, now
from .common import (RUN_COLUMNS, RUN_JSON_COLUMNS, TRANSFER_TABLES, audit_entry, redact_run,
                     redact_transfer_row, to_json_text, verify_chain)


class LocalStore:
    def __init__(self, path: str, redactor: Redactor | None = None, auto_migrate: bool = True):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.redactor = redactor or Redactor()
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        # A local SQLite store is zero-setup, so it migrates itself by default. With
        # auto_migrate=False it refuses to open an out-of-date database instead.
        self.applied_migrations = migrations.migrate(self.db, "sqlite", apply=auto_migrate)

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
    def _insert_run(self, run: Run) -> bool:
        row = redact_run(run, self.redactor)
        for c in RUN_JSON_COLUMNS:
            row[c] = to_json_text(row[c])
        cur = self.db.execute(f"INSERT OR IGNORE INTO runs ({','.join(RUN_COLUMNS)}) "
                              f"VALUES ({','.join('?' * len(RUN_COLUMNS))})", tuple(row[c] for c in RUN_COLUMNS))
        return cur.rowcount == 1

    def put_run(self, run: Run) -> None:
        with self._lock:
            self._insert_run(run)

    def put_runs(self, runs: list[Run]) -> int:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                n = sum(self._insert_run(r) for r in runs)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return n

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        r = dict(rows[0])
        for k in ("input_json", "output_json", "steps_json", "context_json"):
            r[k[:-5]] = json.loads(r.pop(k)) if r[k] is not None else None
        return r

    def list_runs(self, mode: str | None = None, limit: int = 100,
                  before: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if mode:
            where.append("mode=?")
            args.append(mode)
        if before:
            cur = self._q("SELECT started_at FROM runs WHERE id=?", (before,))
            if not cur:
                return []
            where.append("(started_at < ? OR (started_at = ? AND id < ?))")
            args += [cur[0]["started_at"], cur[0]["started_at"], before]
        sql = ("SELECT id FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY started_at DESC, id DESC LIMIT ?")
        return [self.get_run(r["id"]) for r in self._q(sql, (*args, limit))]

    # -- bulk transfer between stores (optimizer worker -> main store) -------------------------
    def export_records(self) -> dict[str, list[dict[str, Any]]]:
        """Rows exactly as stored (already redacted), for import_records on another store."""
        return {t: [dict(r) for r in self._q(f"SELECT * FROM {t}")] for t in TRANSFER_TABLES}

    def import_records(self, records: dict[str, list[dict[str, Any]]]) -> float:
        """Append another store's rows, skipping any already present. Returns the cost of
        the runs that were new, so the caller can charge them to a budget."""
        cost = 0.0
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for t in TRANSFER_TABLES:
                    cols = [r[1] for r in self.db.execute(f"PRAGMA table_info({t})")]
                    sql = f"INSERT OR IGNORE INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                    for row in records.get(t, []):
                        row = redact_transfer_row(t, row, self.redactor)
                        if self.db.execute(sql, tuple(row.get(c) for c in cols)).rowcount and t == "runs":
                            cost += row.get("cost_usd") or 0.0
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return cost

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
        self._x("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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

    def list_candidates(self, status: str | None = None, limit: int | None = None,
                        after: str | None = None) -> list[Candidate]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if after:
            cur = self._q("SELECT created_at FROM candidates WHERE id=?", (after,))
            if not cur:
                return []
            where.append("(created_at > ? OR (created_at = ? AND id > ?))")
            args += [cur[0]["created_at"], cur[0]["created_at"], after]
        sql = ("SELECT id FROM candidates" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY created_at, id" + (" LIMIT ?" if limit is not None else ""))
        return [self.get_candidate(r["id"]) for r in self._q(sql, (*args, limit) if limit is not None else tuple(args))]

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
            e = audit_entry(prev[0]["hash"] if prev else "", actor, action, subject, details)
            self._x("INSERT INTO audit (at, actor, action, subject, details_json, prev_hash, hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (e["at"], actor, action, subject, canonical_json(e["details"]), e["prev_hash"], e["hash"]))

    def audit_log(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit WHERE seq > ? ORDER BY seq" + (" LIMIT ?" if limit is not None else "")
        rows = [dict(r) for r in self._q(sql, (after, limit) if limit is not None else (after,))]
        for r in rows:
            r["details"] = json.loads(r.pop("details_json"))
        return rows

    def verify_audit_chain(self) -> bool:
        return verify_chain(self.audit_log())

    # -- cycles (checkpoints) -----------------------------------------------------------
    def save_cycle(self, cycle_id: str, state: dict[str, Any]) -> None:
        self._x("INSERT INTO cycles VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, "
                "updated_at=excluded.updated_at", (cycle_id, json.dumps(state, default=str), now()))

    def load_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT state_json FROM cycles WHERE id=?", (cycle_id,))
        return json.loads(rows[0]["state_json"]) if rows else None

    # -- sealed-side tables ---------------------------------------------------------------
    def ledger_add(self, gate_version: str, candidate_id: str, kind: str) -> bool:
        with self._lock:
            cur = self.db.execute("INSERT OR IGNORE INTO gate_ledger VALUES (?,?,?,?)",
                                  (gate_version, candidate_id, kind, now()))
            return cur.rowcount == 1

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
        rows = self._q("SELECT hash, json FROM packets WHERE candidate_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                       (candidate_id,))
        return (rows[0]["hash"], json.loads(rows[0]["json"])) if rows else None

