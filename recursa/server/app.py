"""Recursa Server HTTP API (v1).

The API mirrors the store protocols (recursa/store/base.py) one to one: records
under ``/v1/...``, the sealed side under ``/v1/sealed/...``. Every route declares
the API-key scopes allowed to call it (``ROUTES``); the scope tests check each
route against every scope. Bodies and responses are JSON; dataclasses are sent as
their fields. A missing record is ``null``, not an error.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..store.base import RecordStore, SealedStore
from ..types import Candidate, Run, Score, Step
from .keys import Principal, Verifier

# Scope sets, named by what they allow.
EVERYONE = {"ingest", "optimizer", "gate", "reviewer", "admin"}
READ_RECORDS = {"optimizer", "gate", "reviewer", "admin"}
WRITE_EVAL_RUNS = {"optimizer", "gate", "admin"}
APPEND_RUNS = {"ingest", "optimizer", "gate", "admin"}
FEEDBACK = {"ingest", "reviewer", "admin"}
PROMOTE = {"reviewer", "admin"}
PROPOSE = {"optimizer", "reviewer", "admin"}
CANDIDATE_STATUS = {"optimizer", "gate", "reviewer", "admin"}
INSIGHTS_WRITE = {"optimizer", "admin"}
APPROVE = {"reviewer"}  # admins operate the server; they do not approve
APPROVALS_READ = {"gate", "reviewer", "admin"}
AUDIT_WRITE = {"optimizer", "gate", "reviewer", "admin"}  # moves server-side in phase 3
AUDIT_READ = {"reviewer", "admin"}
CYCLES = {"optimizer", "gate", "reviewer", "admin"}
SEALED = {"gate"}
PACKETS_READ = {"gate", "reviewer", "admin"}

# (method, path) -> allowed scopes. Filled in by create_app; exported for tests and docs.
ROUTES: dict[tuple[str, str], frozenset[str]] = {}


def _run(d: dict[str, Any]) -> Run:
    d = dict(d)
    d["steps"] = [Step(**s) for s in d.get("steps", [])]
    return Run(**d)


def create_app(records: RecordStore, sealed: SealedStore, verifier: Verifier,
               ready: Callable[[], dict[str, Any]] | None = None):
    from .. import __version__

    app = FastAPI(title="Recursa Server", version=__version__, openapi_url="/v1/openapi.json", docs_url=None,
                  redoc_url=None)

    def principal(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        p = verifier.verify(token)
        if p is None:
            raise HTTPException(401, "missing, invalid, expired or revoked API key",
                                headers={"WWW-Authenticate": "Bearer"})
        return p

    def route(method: str, path: str, scopes: set[str]):
        allowed = frozenset(scopes)
        ROUTES[(method, path)] = allowed

        def check(p: Principal = Depends(principal)) -> Principal:
            if not (p.scopes & allowed):
                raise HTTPException(403, f"key {p.key_id} lacks a scope for {method} {path} "
                                         f"(needs one of: {', '.join(sorted(allowed))})")
            return p

        def deco(fn):
            app.add_api_route(path, fn, methods=[method], dependencies=[Depends(check)])
            return fn
        return deco

    # Handlers read fields straight from the JSON body, so a missing or mistyped field
    # surfaces as KeyError/TypeError/ValueError: answer 422, never 500.
    @app.exception_handler(KeyError)
    async def _missing_field(request, exc):  # noqa: ANN001
        return JSONResponse({"detail": f"missing field {exc.args[0] if exc.args else ''}"}, status_code=422)

    @app.exception_handler(TypeError)
    @app.exception_handler(ValueError)
    async def _bad_body(request, exc):  # noqa: ANN001
        return JSONResponse({"detail": f"invalid request body: {exc}"}, status_code=422)

    # -- health (no key) -------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            info = ready() if ready else {}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"status": "not_ready", "error": f"{type(e).__name__}: {e}"}, status_code=503)
        return {"status": "ready", **info}

    # -- runs, scores, eval cache, checkpoints: same shape on both sides -------------------------------
    def eval_routes(prefix: str, store: Any, append: set[str], write: set[str], read: set[str],
                    cycles: set[str]):
        @route("POST", f"{prefix}/runs", append)
        def put_run(body: dict = Body(...)):
            store.put_run(_run(body))
            return {"ok": True}

        @route("POST", f"{prefix}/runs:batch", append)
        def put_runs(body: dict = Body(...)):
            return {"stored": store.put_runs([_run(r) for r in body["runs"]])}

        @route("GET", f"{prefix}/runs/{{run_id}}", read)
        def get_run(run_id: str):
            return store.get_run(run_id)

        @route("POST", f"{prefix}/scores", write)
        def put_scores(body: dict = Body(...)):
            store.put_scores([Score(**s) for s in body["scores"]])
            return {"ok": True}

        @route("GET", f"{prefix}/runs/{{run_id}}/scores", read)
        def scores_for(run_id: str):
            return [asdict(s) for s in store.scores_for(run_id)]

        @route("GET", f"{prefix}/eval-cache/{{key}}", read)
        def cache_get(key: str):
            return {"run_id": store.cache_get(key)}

        @route("PUT", f"{prefix}/eval-cache/{{key}}", write)
        def cache_put(key: str, body: dict = Body(...)):
            store.cache_put(key, body["run_id"])
            return {"ok": True}

        @route("PUT", f"{prefix}/cycles/{{cycle_id}}", cycles)
        def save_cycle(cycle_id: str, body: dict = Body(...)):
            store.save_cycle(cycle_id, body["state"])
            return {"ok": True}

        @route("GET", f"{prefix}/cycles/{{cycle_id}}", cycles)
        def load_cycle(cycle_id: str):
            return store.load_cycle(cycle_id)

    eval_routes("/v1", records, APPEND_RUNS, WRITE_EVAL_RUNS, READ_RECORDS, CYCLES)
    eval_routes("/v1/sealed", sealed, SEALED, SEALED, SEALED, SEALED)

    # -- records only ----------------------------------------------------------------------------------
    @route("GET", "/v1/runs", READ_RECORDS)
    def list_runs(mode: str | None = None, limit: int = 100, before: str | None = None):
        return records.list_runs(mode=mode, limit=min(limit, 1000), before=before)

    @route("GET", "/v1/records:export", WRITE_EVAL_RUNS)
    def export_records():
        return records.export_records()

    @route("POST", "/v1/records:import", WRITE_EVAL_RUNS)
    def import_records(body: dict = Body(...)):
        return {"cost_usd": records.import_records(body["records"])}

    @route("POST", "/v1/feedback", FEEDBACK)
    def put_feedback(body: dict = Body(...)):
        args = (body["run_id"], body["kind"], body.get("value"), body.get("correction"), body["author"],
                body.get("idempotency_key"))
        try:
            return {"id": records.put_feedback(*args)}
        except KeyError as e:  # the run does not exist
            raise HTTPException(404, str(e.args[0] if e.args else e)) from None

    @route("GET", "/v1/runs/{run_id}/feedback", READ_RECORDS)
    def feedback_for(run_id: str):
        return records.feedback_for(run_id)

    @route("POST", "/v1/versions", PROMOTE)
    def put_version(body: dict = Body(...)):
        return {"id": records.put_version(body.get("parent"), body["config"], body.get("candidate_id"),
                                          body.get("approvers", []))}

    @route("GET", "/v1/versions", EVERYONE)
    def list_versions():
        return records.list_versions()

    @route("GET", "/v1/versions/{vid}", EVERYONE)
    def get_version(vid: str):
        return records.get_version(vid)

    @route("POST", "/v1/deployments", PROMOTE)
    def deploy(body: dict = Body(...)):
        records.deploy(body["version_id"], body["action"], body["actor"], body.get("reason", ""))
        return {"ok": True}

    @route("GET", "/v1/deployments", EVERYONE)
    def deployments():
        return records.deployments()

    @route("GET", "/v1/deployments/current", EVERYONE)
    def current_version():
        return {"version_id": records.current_version()}

    @route("POST", "/v1/candidates", PROPOSE)
    def put_candidate(body: dict = Body(...)):
        records.put_candidate(Candidate(**body))
        return {"ok": True}

    @route("PATCH", "/v1/candidates/{cid}", CANDIDATE_STATUS)
    def set_candidate_status(cid: str, body: dict = Body(...)):
        records.set_candidate_status(cid, body["status"])
        return {"ok": True}

    @route("GET", "/v1/candidates/{cid}", READ_RECORDS)
    def get_candidate(cid: str):
        c = records.get_candidate(cid)
        return asdict(c) if c else None

    @route("GET", "/v1/candidates", READ_RECORDS)
    def list_candidates(status: str | None = None, limit: int | None = None, after: str | None = None):
        return [asdict(c) for c in records.list_candidates(status=status, limit=limit, after=after)]

    @route("POST", "/v1/insights", INSIGHTS_WRITE)
    def put_insight(body: dict = Body(...)):
        records.put_insight(body["cycle_id"], body["insight"])
        return {"ok": True}

    @route("POST", "/v1/insights:lookup", READ_RECORDS)
    def get_insights(body: dict = Body(...)):
        return records.get_insights(body["ids"])

    @route("POST", "/v1/approvals", APPROVE)
    def put_approval(body: dict = Body(...)):
        return {"id": records.put_approval(body["candidate_id"], body["reviewer"], body["decision"], body["reason"],
                                           body["channel"], body["packet_hash"])}

    @route("GET", "/v1/candidates/{cid}/approvals", APPROVALS_READ)
    def approvals_for(cid: str):
        return records.approvals_for(cid)

    @route("POST", "/v1/audit", AUDIT_WRITE)
    def audit(body: dict = Body(...)):
        records.audit(body["actor"], body["action"], body["subject"], body.get("details"))
        return {"ok": True}

    @route("GET", "/v1/audit", AUDIT_READ)
    def audit_log(after: int = 0, limit: int | None = None):
        return records.audit_log(after=after, limit=limit)

    @route("GET", "/v1/audit:verify", AUDIT_READ)
    def verify_audit_chain():
        return {"intact": records.verify_audit_chain()}

    # -- sealed only -------------------------------------------------------------------------------------
    @route("POST", "/v1/sealed/ledger", SEALED)
    def ledger_add(body: dict = Body(...)):
        return {"charged": sealed.ledger_add(body["gate_version"], body["candidate_id"], body["kind"])}

    @route("GET", "/v1/sealed/ledger/count", SEALED)
    def ledger_count(gate_version: str, kind: str):
        return {"count": sealed.ledger_count(gate_version, kind)}

    @route("GET", "/v1/sealed/ledger/has", SEALED)
    def ledger_has(gate_version: str, candidate_id: str, kind: str):
        return {"has": sealed.ledger_has(gate_version, candidate_id, kind)}

    @route("POST", "/v1/sealed/packets", SEALED)
    def put_packet(body: dict = Body(...)):
        return {"hash": sealed.put_packet(body["candidate_id"], body["packet"])}

    @route("GET", "/v1/sealed/packets/{cid}/latest", PACKETS_READ)
    def latest_packet(cid: str):
        p = sealed.latest_packet(cid)
        return {"hash": p[0], "packet": p[1]} if p else None

    return app
