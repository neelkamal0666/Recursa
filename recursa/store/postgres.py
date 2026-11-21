"""Postgres backend, used by Recursa Server (needs ``pip install recursa[server]``).

``PostgresRecordStore`` works in the ``recursa`` schema and ``PostgresSealedStore``
in ``recursa_sealed``; the server connects to each with a different database role
(see migrations/postgres/0003). Semantics are those of base.py, checked by the same
contract tests as the SQLite store.

The schema is never created or changed from here: stores check that the database is
at the version this release expects and refuse to open otherwise
(``recursa server migrate`` applies migrations with the owner's credentials).
"""
from __future__ import annotations

import json
from typing import Any

from .. import migrations
from ..redact import Redactor
from ..types import Candidate, Run, Score, canonical_json, content_hash, new_id, now
from .common import (RUN_COLUMNS, RUN_JSON_COLUMNS, TRANSFER_JSON, TRANSFER_TABLES, audit_entry, redact_run,
                     redact_transfer_row, to_json_text, verify_chain)

AUDIT_LOCK, VERSION_LOCK = 7262818, 7262819


def _pool(dsn: str, min_size: int, max_size: int):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=True)


def check_schema(dsn: str) -> dict[str, Any]:
    """Raise SchemaError unless the database is exactly at this release's version."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        migrations.migrate(conn, "postgres", apply=False)
        return migrations.status(conn, "postgres")


class _PgBase:
    schema = ""

    def __init__(self, dsn: str, redactor: Redactor | None = None, min_size: int = 1, max_size: int = 10,
                 check: bool = True):
        if check:
            check_schema(dsn)
        self.redactor = redactor or Redactor()
        self.pool = _pool(dsn, min_size, max_size)

    def close(self) -> None:
        self.pool.close()

    def _q(self, sql: str, args: Any = ()) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: Any = ()) -> int:
        with self.pool.connection() as conn:
            return conn.execute(sql, args).rowcount

    def _t(self, name: str) -> str:
        return f"{self.schema}.{name}"

    # -- runs, scores and the eval cache (both sides) ------------------------------------------
    def _insert_run(self, conn: Any, run: Run) -> bool:
        from psycopg.types.json import Jsonb

        row = redact_run(run, self.redactor)
        for c in RUN_JSON_COLUMNS:
            row[c] = Jsonb(row[c])
        cols = (*RUN_COLUMNS, "received_at")
        vals = (*(row[c] for c in RUN_COLUMNS), now())
        cur = conn.execute(f"INSERT INTO {self._t('runs')} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
                           "ON CONFLICT (id) DO NOTHING", vals)
        return cur.rowcount == 1

    def put_run(self, run: Run) -> None:
        with self.pool.connection() as conn:
            self._insert_run(conn, run)

    def put_runs(self, runs: list[Run]) -> int:
        with self.pool.connection() as conn, conn.transaction():
            return sum(self._insert_run(conn, r) for r in runs)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._q(f"SELECT * FROM {self._t('runs')} WHERE id=%s", (run_id,))
        if not rows:
            return None
        r = rows[0]
        for k in RUN_JSON_COLUMNS:
            r[k[:-5]] = r.pop(k)
        return r

    def put_scores(self, scores: list[Score]) -> None:
        with self.pool.connection() as conn, conn.transaction():
            for s in scores:
                conn.execute(f"INSERT INTO {self._t('scores')} VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                             (s.run_id, s.evaluator, s.evaluator_version, s.value, s.rationale, s.tier))

    def scores_for(self, run_id: str) -> list[Score]:
        return [Score(**r) for r in self._q(f"SELECT * FROM {self._t('scores')} WHERE run_id=%s", (run_id,))]

    def cache_get(self, key: str) -> str | None:
        rows = self._q(f"SELECT run_id FROM {self._t('eval_cache')} WHERE key=%s", (key,))
        return rows[0]["run_id"] if rows else None

    def cache_put(self, key: str, run_id: str) -> None:
        self._x(f"INSERT INTO {self._t('eval_cache')} VALUES (%s,%s) ON CONFLICT DO NOTHING", (key, run_id))

    # -- cycle checkpoints (both sides) --------------------------------------------------------
    def save_cycle(self, cycle_id: str, state: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        self._x(f"INSERT INTO {self._t('cycles')} VALUES (%s,%s,%s) ON CONFLICT (id) DO UPDATE "
                "SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                (cycle_id, Jsonb(json.loads(json.dumps(state, default=str))), now()))

    def load_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        rows = self._q(f"SELECT state_json FROM {self._t('cycles')} WHERE id=%s", (cycle_id,))
        return rows[0]["state_json"] if rows else None


class PostgresRecordStore(_PgBase):
    schema = "recursa"

    def list_runs(self, mode: str | None = None, limit: int = 100,
                  before: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if mode:
            where.append("mode=%s")
            args.append(mode)
        if before:
            cur = self._q("SELECT started_at FROM recursa.runs WHERE id=%s", (before,))
            if not cur:
                return []
            where.append("(started_at, id) < (%s, %s)")
            args += [cur[0]["started_at"], before]
        sql = ("SELECT id FROM recursa.runs" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY started_at DESC, id DESC LIMIT %s")
        return [self.get_run(r["id"]) for r in self._q(sql, (*args, limit))]

    # -- transfer ---------------------------------------------------------------------------------
    def export_records(self) -> dict[str, list[dict[str, Any]]]:
        out = {}
        for t in TRANSFER_TABLES:
            rows = self._q(f"SELECT * FROM recursa.{t}")
            for r in rows:
                for c in TRANSFER_JSON[t]:
                    if r.get(c) is not None:
                        r[c] = to_json_text(r[c])
            out[t] = rows
        return out

    def import_records(self, records: dict[str, list[dict[str, Any]]]) -> float:
        from psycopg.types.json import Jsonb

        cost = 0.0
        with self.pool.connection() as conn, conn.transaction():
            for t in TRANSFER_TABLES:
                cols = [r["column_name"] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='recursa' "
                    "AND table_name=%s ORDER BY ordinal_position", (t,)).fetchall()]
                for row in records.get(t, []):
                    row = redact_transfer_row(t, row, self.redactor)
                    vals = []
                    for c in cols:
                        v = row.get(c)
                        if c in TRANSFER_JSON[t] and v is not None:
                            v = Jsonb(json.loads(v))
                        if c == "received_at":
                            v = now()
                        vals.append(v)
                    cur = conn.execute(f"INSERT INTO recursa.{t} ({','.join(cols)}) VALUES "
                                       f"({','.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING", vals)
                    if cur.rowcount == 1 and t == "runs":
                        cost += row.get("cost_usd") or 0.0
        return cost

    # -- feedback ---------------------------------------------------------------------------------
    def put_feedback(self, run_id: str, kind: str, value: float | None, correction: Any, author: str,
                     idempotency_key: str | None) -> str:
        key = idempotency_key or new_id("fbk")
        with self.pool.connection() as conn, conn.transaction():
            existing = conn.execute("SELECT id FROM recursa.feedback WHERE idempotency_key=%s", (key,)).fetchall()
            if existing:
                return existing[0]["id"]
            if not conn.execute("SELECT 1 FROM recursa.runs WHERE id=%s", (run_id,)).fetchall():
                raise KeyError(f"unknown run {run_id}")
            fid = new_id("fb")
            cur = conn.execute("INSERT INTO recursa.feedback VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                               "ON CONFLICT (idempotency_key) DO NOTHING",
                               (fid, run_id, kind, value, json.dumps(self.redactor(correction)), author, key, now()))
            if cur.rowcount == 0:  # a concurrent request with the same key won
                return conn.execute("SELECT id FROM recursa.feedback WHERE idempotency_key=%s",
                                    (key,)).fetchone()["id"]
            return fid

    def feedback_for(self, run_id: str) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM recursa.feedback WHERE run_id=%s ORDER BY created_at", (run_id,))

    # -- versions and deployments -----------------------------------------------------------------
    def put_version(self, parent: str | None, config: dict[str, Any], candidate_id: str | None,
                    approvers: list[str]) -> str:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (VERSION_LOCK,))
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) AS m FROM recursa.versions").fetchone()["m"] + 1
            vid = f"v{seq}"
            conn.execute("INSERT INTO recursa.versions VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (vid, seq, parent, Jsonb(config), content_hash(config), candidate_id, Jsonb(approvers),
                          now()))
            return vid

    def get_version(self, vid: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM recursa.versions WHERE id=%s", (vid,))
        if not rows:
            return None
        r = rows[0]
        r["config"] = r.pop("config_json")
        r["approvers"] = r.pop("approvers_json")
        return r

    def list_versions(self) -> list[dict[str, Any]]:
        return [self.get_version(r["id"]) for r in self._q("SELECT id FROM recursa.versions ORDER BY seq")]

    def deploy(self, version_id: str, action: str, actor: str, reason: str = "") -> None:
        self._x("INSERT INTO recursa.deployments (version_id, action, actor, reason, at) VALUES (%s,%s,%s,%s,%s)",
                (version_id, action, actor, reason, now()))

    def current_version(self) -> str | None:
        rows = self._q("SELECT version_id FROM recursa.deployments ORDER BY seq DESC LIMIT 1")
        return rows[0]["version_id"] if rows else None

    def deployments(self) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM recursa.deployments ORDER BY seq")

    # -- candidates and insights ------------------------------------------------------------------
    def put_candidate(self, c: Candidate) -> None:
        from psycopg.types.json import Jsonb

        self._x("INSERT INTO recursa.candidates VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (c.id, c.surface, c.baseline_version, Jsonb(c.proposed), Jsonb(c.insight_ids), c.status,
                 c.created_at, c.source, c.parent_candidate, Jsonb(c.dev_report), c.rationale))

    def set_candidate_status(self, cid: str, status: str) -> None:
        self._x("UPDATE recursa.candidates SET status=%s WHERE id=%s", (status, cid))

    @staticmethod
    def _candidate(r: dict[str, Any]) -> Candidate:
        return Candidate(id=r["id"], surface=r["surface"], baseline_version=r["baseline_version"],
                         proposed=r["proposed_json"], insight_ids=r["insight_ids_json"], status=r["status"],
                         created_at=r["created_at"], source=r["source"], parent_candidate=r["parent_candidate"],
                         dev_report=r["dev_report_json"], rationale=r["rationale"])

    def get_candidate(self, cid: str) -> Candidate | None:
        rows = self._q("SELECT * FROM recursa.candidates WHERE id=%s", (cid,))
        return self._candidate(rows[0]) if rows else None

    def list_candidates(self, status: str | None = None, limit: int | None = None,
                        after: str | None = None) -> list[Candidate]:
        where, args = [], []
        if status:
            where.append("status=%s")
            args.append(status)
        if after:
            cur = self._q("SELECT created_at FROM recursa.candidates WHERE id=%s", (after,))
            if not cur:
                return []
            where.append("(created_at, id) > (%s, %s)")
            args += [cur[0]["created_at"], after]
        sql = ("SELECT * FROM recursa.candidates" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY created_at, id" + (" LIMIT %s" if limit is not None else ""))
        return [self._candidate(r) for r in self._q(sql, (*args, limit) if limit is not None else tuple(args))]

    def put_insight(self, cycle_id: str, insight: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        self._x("INSERT INTO recursa.insights VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (insight["id"], cycle_id, Jsonb(json.loads(json.dumps(insight, default=str))), now()))

    def get_insights(self, ids: list[str]) -> list[dict[str, Any]]:
        found = {r["id"]: r["json"] for r in self._q("SELECT id, json FROM recursa.insights WHERE id = ANY(%s)",
                                                     (list(ids),))}
        return [found[i] for i in ids if i in found]

    # -- approvals and audit ----------------------------------------------------------------------
    def put_approval(self, candidate_id: str, reviewer: str, decision: str, reason: str, channel: str,
                     packet_hash: str) -> str:
        aid = new_id("apr")
        self._x("INSERT INTO recursa.approvals VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (aid, candidate_id, reviewer, decision, reason, channel, packet_hash, now()))
        return aid

    def approvals_for(self, candidate_id: str) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM recursa.approvals WHERE candidate_id=%s ORDER BY at, id", (candidate_id,))

    def audit(self, actor: str, action: str, subject: str, details: dict[str, Any] | None = None) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
            prev = conn.execute("SELECT hash FROM recursa.audit ORDER BY seq DESC LIMIT 1").fetchall()
            e = audit_entry(prev[0]["hash"] if prev else "", actor, action, subject, details)
            conn.execute("INSERT INTO recursa.audit (at, actor, action, subject, details_json, prev_hash, hash) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (e["at"], actor, action, subject, canonical_json(e["details"]), e["prev_hash"], e["hash"]))

    def audit_log(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recursa.audit WHERE seq > %s ORDER BY seq" + (" LIMIT %s" if limit is not None else "")
        rows = self._q(sql, (after, limit) if limit is not None else (after,))
        for r in rows:
            r["details"] = json.loads(r.pop("details_json"))
        return rows

    def verify_audit_chain(self) -> bool:
        return verify_chain(self.audit_log())


class PostgresSealedStore(_PgBase):
    schema = "recursa_sealed"

    def ledger_add(self, gate_version: str, candidate_id: str, kind: str) -> bool:
        return self._x("INSERT INTO recursa_sealed.gate_ledger VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (gate_version, candidate_id, kind, now())) == 1

    def ledger_count(self, gate_version: str, kind: str) -> int:
        return self._q("SELECT COUNT(*) AS n FROM recursa_sealed.gate_ledger WHERE gate_version=%s AND kind=%s",
                       (gate_version, kind))[0]["n"]

    def ledger_has(self, gate_version: str, candidate_id: str, kind: str) -> bool:
        return bool(self._q("SELECT 1 FROM recursa_sealed.gate_ledger WHERE gate_version=%s AND candidate_id=%s "
                            "AND kind=%s", (gate_version, candidate_id, kind)))

    def put_packet(self, candidate_id: str, packet: dict[str, Any]) -> str:
        h = content_hash(packet)
        self._x("INSERT INTO recursa_sealed.packets VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (candidate_id, h, canonical_json(packet), now()))
        return h

    def latest_packet(self, candidate_id: str) -> tuple[str, dict[str, Any]] | None:
        rows = self._q("SELECT hash, json FROM recursa_sealed.packets WHERE candidate_id=%s "
                       "ORDER BY created_at DESC LIMIT 1", (candidate_id,))
        return (rows[0]["hash"], json.loads(rows[0]["json"])) if rows else None
