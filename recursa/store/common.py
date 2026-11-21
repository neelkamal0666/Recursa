"""Logic shared by every backend, so redaction and the audit chain behave identically
whether records live in SQLite, Postgres or behind Recursa Server."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Iterable

from ..redact import RedactionError, Redactor
from ..types import Run, content_hash, now

# Columns of the runs table, in schema order (the transfer format; see base.py).
RUN_COLUMNS = ("id", "agent", "agent_version", "mode", "example_id", "sample", "status", "input_json",
               "output_json", "steps_json", "cost_usd", "latency_ms", "started_at", "context_json", "error",
               "redaction", "telemetry")
RUN_JSON_COLUMNS = ("input_json", "output_json", "steps_json", "context_json")

# Rows moved by export_records / import_records, and the JSON-as-text columns in each.
TRANSFER_TABLES = ("runs", "scores", "eval_cache")
TRANSFER_JSON = {"runs": RUN_JSON_COLUMNS, "scores": (), "eval_cache": ()}


def redact_run(run: Run, redactor: Redactor) -> dict[str, Any]:
    """Column values (JSON columns as Python objects) for one run, redacted. If
    redaction fails, the payload is dropped and only metrics are kept (fail closed)."""
    try:
        inp, out = redactor(run.input), redactor(run.output)
        steps = redactor([asdict(s) for s in run.steps])
        ctx = redactor(run.context_snapshot)
        err = redactor(run.error) if run.error else None
        redaction = "applied"
    except RedactionError:
        inp = out = ctx = None
        steps = [{k: v for k, v in asdict(s).items() if k not in ("input", "output")} for s in run.steps]
        err, redaction = "redaction failed; payload dropped", "unredacted_dropped"
    return {"id": run.id, "agent": run.agent, "agent_version": run.agent_version, "mode": run.mode,
            "example_id": run.example_id, "sample": run.sample, "status": run.status, "input_json": inp,
            "output_json": out, "steps_json": steps, "cost_usd": run.cost_usd, "latency_ms": run.latency_ms,
            "started_at": run.started_at, "context_json": ctx, "error": err, "redaction": redaction,
            "telemetry": run.telemetry}


def to_json_text(value: Any) -> str:
    return json.dumps(value, default=str)


def redact_transfer_row(table: str, row: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """Re-apply redaction to an imported row (transfer format) without trusting the sender."""
    cols = TRANSFER_JSON.get(table, ())
    if not cols or row.get("redaction") == "unredacted_dropped":
        return row
    out = dict(row)
    try:
        for c in cols:
            if out.get(c) is not None:
                out[c] = to_json_text(redactor(json.loads(out[c])))
        if out.get("error"):
            out["error"] = redactor(out["error"])
    except RedactionError:
        for c in ("input_json", "output_json", "context_json"):
            out[c] = None
        out["error"], out["redaction"] = "redaction failed; payload dropped", "unredacted_dropped"
    return out


def audit_entry(prev_hash: str, actor: str, action: str, subject: str,
                details: dict[str, Any] | None) -> dict[str, Any]:
    """A new audit entry committing to the previous one."""
    at = now()
    body = {"at": at, "actor": actor, "action": action, "subject": subject, "details": details or {},
            "prev": prev_hash}
    return {"at": at, "actor": actor, "action": action, "subject": subject, "details": details or {},
            "prev_hash": prev_hash, "hash": content_hash(body)}


def verify_chain(entries: Iterable[dict[str, Any]]) -> bool:
    prev = ""
    for r in entries:
        body = {"at": r["at"], "actor": r["actor"], "action": r["action"], "subject": r["subject"],
                "details": r["details"], "prev": prev}
        if r["prev_hash"] != prev or r["hash"] != content_hash(body):
            return False
        prev = r["hash"]
    return True
