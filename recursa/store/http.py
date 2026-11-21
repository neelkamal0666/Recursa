"""HTTP client backend: the store protocols over Recursa Server's API.

Selected with ``store: {url: https://recursa.internal, key_env: RECURSA_API_KEY}``.
Uses only the standard library, so the base package stays dependency-light.

Retries: reads and idempotent writes (runs, scores, cache, candidates, insights,
feedback with an idempotency key, ledger, packets, checkpoints) are retried on
connection errors and 5xx responses with exponential backoff. Writes that are not
idempotent (versions, deployments, approvals, audit entries) are sent once; a
failure surfaces to the caller instead of risking a duplicate.

Redaction happens on the server before anything is stored; the client sends what
the agent produced, so use TLS for any non-local server.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from dataclasses import asdict
from typing import Any

from ..types import Candidate, Run, Score, new_id

_BATCH = 500  # rows per request when shipping runs or importing records


class ServerError(RuntimeError):
    pass


# Servers close idle keep-alive connections (uvicorn after 5 s). Reopen before that, and
# treat a dead reused connection as "not sent": retry once on a fresh one.
_MAX_IDLE_S = 2.0
_STALE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected)


class _Client:
    """Keep-alive HTTP/1.1 client: one persistent connection per thread, reopened on error."""

    def __init__(self, url: str, key: str, timeout: float = 30.0, retries: int = 4):
        if not key:
            raise PermissionError("no API key: set the environment variable named by store.key_env")
        parts = urllib.parse.urlsplit(url.rstrip("/"))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"store.url must be an http(s) URL, got {url!r}")
        self.base, self.scheme, self.host, self.port = url.rstrip("/"), parts.scheme, parts.hostname, parts.port
        self.root = parts.path.rstrip("/")
        self.key, self.timeout, self.retries = key, timeout, retries
        self._local = threading.local()

    def _conn(self) -> tuple[http.client.HTTPConnection, bool]:
        """(connection, reused). Connections idle for too long are replaced, not reused."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and time.monotonic() - getattr(self._local, "used", 0.0) > _MAX_IDLE_S:
            self._drop()
            conn = None
        if conn is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            conn = cls(self.host, self.port, timeout=self.timeout)
            self._local.conn = conn
            return conn, False
        return conn, True

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def close(self) -> None:
        self._drop()

    def call(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None,
             idempotent: bool = True) -> Any:
        target = self.root + urllib.parse.quote(path, safe="/:")
        q = {k: v for k, v in (query or {}).items() if v is not None}
        if q:
            target += "?" + urllib.parse.urlencode(q)
        data = None if body is None else json.dumps(body, default=str).encode()
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = self.retries + 1 if (idempotent or method == "GET") else 1
        attempt, stale_retry_used = 0, False
        while True:
            reused = False
            try:
                conn, reused = self._conn()
                conn.request(method, target, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                self._local.used = time.monotonic()
            except (OSError, http.client.HTTPException) as e:
                self._drop()
                if reused and isinstance(e, _STALE) and not stale_retry_used:
                    stale_retry_used = True  # the server had closed the idle connection: not processed
                    continue
                attempt += 1
                if attempt >= attempts:
                    raise ServerError(f"cannot reach Recursa Server at {self.base}: {e}") from None
                time.sleep(min(0.2 * 2 ** (attempt - 1), 5.0))
                continue
            if status < 300:
                return json.loads(raw) if raw else None
            detail = _detail(raw)
            if status in (401, 403):
                raise PermissionError(f"Recursa Server refused {method} {path}: {detail}")
            if status == 404:
                raise KeyError(detail)
            attempt += 1
            if status < 500 or attempt >= attempts:
                raise ServerError(f"{method} {path} failed with HTTP {status}: {detail}")
            time.sleep(min(0.2 * 2 ** (attempt - 1), 5.0))


def _detail(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("detail"))
    except Exception:  # noqa: BLE001
        return raw[:200].decode(errors="replace")


class _HttpEvalRuns:
    prefix = ""

    def __init__(self, url: str, key: str, timeout: float = 30.0):
        self.c = _Client(url, key, timeout)

    def close(self) -> None:
        self.c.close()

    def put_run(self, run: Run) -> None:
        self.c.call("POST", f"{self.prefix}/runs", asdict(run))

    def put_runs(self, runs: list[Run]) -> int:
        n = 0
        for i in range(0, len(runs), _BATCH):
            n += self.c.call("POST", f"{self.prefix}/runs:batch", {"runs": [asdict(r) for r in runs[i:i + _BATCH]]})[
                "stored"]
        return n

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.c.call("GET", f"{self.prefix}/runs/{run_id}")

    def put_scores(self, scores: list[Score]) -> None:
        self.c.call("POST", f"{self.prefix}/scores", {"scores": [asdict(s) for s in scores]})

    def scores_for(self, run_id: str) -> list[Score]:
        return [Score(**s) for s in self.c.call("GET", f"{self.prefix}/runs/{run_id}/scores")]

    def cache_get(self, key: str) -> str | None:
        return self.c.call("GET", f"{self.prefix}/eval-cache/{key}")["run_id"]

    def cache_put(self, key: str, run_id: str) -> None:
        self.c.call("PUT", f"{self.prefix}/eval-cache/{key}", {"run_id": run_id})

    def save_cycle(self, cycle_id: str, state: dict[str, Any]) -> None:
        self.c.call("PUT", f"{self.prefix}/cycles/{cycle_id}", {"state": json.loads(json.dumps(state, default=str))})

    def load_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        return self.c.call("GET", f"{self.prefix}/cycles/{cycle_id}")


class HttpRecordStore(_HttpEvalRuns):
    prefix = "/v1"

    def list_runs(self, mode: str | None = None, limit: int = 100,
                  before: str | None = None) -> list[dict[str, Any]]:
        return self.c.call("GET", "/v1/runs", query={"mode": mode, "limit": limit, "before": before})

    def export_records(self) -> dict[str, list[dict[str, Any]]]:
        return self.c.call("GET", "/v1/records:export")

    def import_records(self, records: dict[str, list[dict[str, Any]]]) -> float:
        cost = 0.0
        # Runs before scores and cache entries, in batches; each batch is idempotent.
        for table in ("runs", "scores", "eval_cache"):
            rows = records.get(table, [])
            for i in range(0, len(rows), _BATCH):
                cost += self.c.call("POST", "/v1/records:import", {"records": {table: rows[i:i + _BATCH]}})[
                    "cost_usd"]
        return cost

    def put_feedback(self, run_id: str, kind: str, value: float | None, correction: Any, author: str,
                     idempotency_key: str | None) -> str:
        key = idempotency_key or new_id("fbk")  # makes the retry safe
        return self.c.call("POST", "/v1/feedback", {"run_id": run_id, "kind": kind, "value": value,
                                                    "correction": correction, "author": author,
                                                    "idempotency_key": key})["id"]

    def feedback_for(self, run_id: str) -> list[dict[str, Any]]:
        return self.c.call("GET", f"/v1/runs/{run_id}/feedback")

    def put_version(self, parent: str | None, config: dict[str, Any], candidate_id: str | None,
                    approvers: list[str]) -> str:
        return self.c.call("POST", "/v1/versions", {"parent": parent, "config": config, "candidate_id": candidate_id,
                                                    "approvers": approvers}, idempotent=False)["id"]

    def get_version(self, vid: str) -> dict[str, Any] | None:
        return self.c.call("GET", f"/v1/versions/{vid}")

    def list_versions(self) -> list[dict[str, Any]]:
        return self.c.call("GET", "/v1/versions")

    def deploy(self, version_id: str, action: str, actor: str, reason: str = "") -> None:
        self.c.call("POST", "/v1/deployments", {"version_id": version_id, "action": action, "actor": actor,
                                                "reason": reason}, idempotent=False)

    def current_version(self) -> str | None:
        return self.c.call("GET", "/v1/deployments/current")["version_id"]

    def deployments(self) -> list[dict[str, Any]]:
        return self.c.call("GET", "/v1/deployments")

    def put_candidate(self, c: Candidate) -> None:
        self.c.call("POST", "/v1/candidates", asdict(c))

    def set_candidate_status(self, cid: str, status: str) -> None:
        self.c.call("PATCH", f"/v1/candidates/{cid}", {"status": status})

    def get_candidate(self, cid: str) -> Candidate | None:
        d = self.c.call("GET", f"/v1/candidates/{cid}")
        return Candidate(**d) if d else None

    def list_candidates(self, status: str | None = None, limit: int | None = None,
                        after: str | None = None) -> list[Candidate]:
        return [Candidate(**d) for d in self.c.call("GET", "/v1/candidates",
                                                    query={"status": status, "limit": limit, "after": after})]

    def put_insight(self, cycle_id: str, insight: dict[str, Any]) -> None:
        self.c.call("POST", "/v1/insights", {"cycle_id": cycle_id, "insight": insight})

    def get_insights(self, ids: list[str]) -> list[dict[str, Any]]:
        return self.c.call("POST", "/v1/insights:lookup", {"ids": list(ids)})

    def put_approval(self, candidate_id: str, reviewer: str, decision: str, reason: str, channel: str,
                     packet_hash: str) -> str:
        return self.c.call("POST", "/v1/approvals", {"candidate_id": candidate_id, "reviewer": reviewer,
                                                     "decision": decision, "reason": reason, "channel": channel,
                                                     "packet_hash": packet_hash}, idempotent=False)["id"]

    def approvals_for(self, candidate_id: str) -> list[dict[str, Any]]:
        return self.c.call("GET", f"/v1/candidates/{candidate_id}/approvals")

    def audit(self, actor: str, action: str, subject: str, details: dict[str, Any] | None = None) -> None:
        self.c.call("POST", "/v1/audit", {"actor": actor, "action": action, "subject": subject,
                                          "details": json.loads(json.dumps(details or {}, default=str))},
                    idempotent=False)

    def audit_log(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        return self.c.call("GET", "/v1/audit", query={"after": after, "limit": limit})

    def verify_audit_chain(self) -> bool:
        return self.c.call("GET", "/v1/audit:verify")["intact"]


class HttpSealedStore(_HttpEvalRuns):
    prefix = "/v1/sealed"

    def ledger_add(self, gate_version: str, candidate_id: str, kind: str) -> bool:
        return self.c.call("POST", "/v1/sealed/ledger", {"gate_version": gate_version, "candidate_id": candidate_id,
                                                         "kind": kind})["charged"]

    def ledger_count(self, gate_version: str, kind: str) -> int:
        return self.c.call("GET", "/v1/sealed/ledger/count", query={"gate_version": gate_version, "kind": kind})[
            "count"]

    def ledger_has(self, gate_version: str, candidate_id: str, kind: str) -> bool:
        return self.c.call("GET", "/v1/sealed/ledger/has", query={"gate_version": gate_version,
                                                                  "candidate_id": candidate_id, "kind": kind})["has"]

    def put_packet(self, candidate_id: str, packet: dict[str, Any]) -> str:
        return self.c.call("POST", "/v1/sealed/packets", {"candidate_id": candidate_id, "packet": packet})["hash"]

    def latest_packet(self, candidate_id: str) -> tuple[str, dict[str, Any]] | None:
        d = self.c.call("GET", f"/v1/sealed/packets/{candidate_id}/latest")
        return (d["hash"], d["packet"]) if d else None
