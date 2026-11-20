"""Core record types. Every persisted record is immutable once written."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(obj: Any) -> str:
    data = obj if isinstance(obj, (bytes, bytearray)) else canonical_json(obj).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass
class Example:
    """One labelled task. `group` clusters correlated examples for the bootstrap."""

    id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    group: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = "human"

    def __post_init__(self) -> None:
        if not self.group:
            self.group = self.id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Example":
        return cls(
            id=str(d["id"]),
            input=d["input"],
            expected=d["expected"],
            group=str(d.get("group") or d["id"]),
            tags=list(d.get("tags", [])),
            source=d.get("source", "human"),
        )


@dataclass
class Step:
    """One model or tool call inside a run (or an attempt of a run)."""

    kind: str  # "model" | "tool"
    name: str
    input: Any = None
    output: Any = None
    attempt: int = 0
    started_at: float = 0.0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    capability: str | None = None  # tools only
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    """What an agent returns. Plain return values are wrapped automatically."""

    output: Any
    status: str = "ok"  # ok | error | cancelled


@dataclass
class Run:
    id: str
    agent: str
    agent_version: str
    mode: str  # online | eval
    input: Any
    output: Any
    status: str
    steps: list[Step]
    cost_usd: float
    latency_ms: float
    started_at: float
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    example_id: str | None = None
    sample: int = 0
    redaction: str = "applied"  # applied | unredacted_dropped


@dataclass
class Score:
    run_id: str
    evaluator: str
    evaluator_version: str
    value: float
    rationale: str = ""
    tier: str = "ground_truth"  # ground_truth | safety | advisory


@dataclass
class Insight:
    id: str
    statement: str
    failure_mode: str
    evidence_run_ids: list[str]
    severity: float
    frequency: int
    data: dict[str, Any] = field(default_factory=dict)
    verified: bool = True  # False when backed only by judge rationales


@dataclass
class Candidate:
    id: str
    surface: str
    baseline_version: str
    proposed: dict[str, Any]  # surface name -> new value
    insight_ids: list[str]
    status: str
    created_at: float
    source: str = "optimizer"  # optimizer | human
    parent_candidate: str | None = None
    dev_report: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


CANDIDATE_STATUSES = {
    "proposed",          # produced by the optimizer or a human edit
    "gate_rejected",     # failed the sealed gate (reason never returned to the optimizer)
    "confirm_failed",    # passed the gate, failed the one-time confirmation set
    "awaiting_review",   # eligible; waiting for human approval
    "rejected",          # a human rejected it
    "changes_requested", # a human asked for changes; a new candidate carries them
    "promoted",          # approved and promoted to a new AgentVersion
    "stale",             # baseline changed or review window expired
}
