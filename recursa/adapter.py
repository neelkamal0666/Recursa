"""Adapter contract for v1: an async Python function plus learnable surfaces.

    async def my_agent(task: dict, ctx: rc.Context) -> Any: ...
    agent = rc.wrap(my_agent, name="triage",
                    surfaces={"system_prompt": PROMPT},
                    model={"id": "claude-opus-5-5"},
                    tools=[lookup_order])
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import trace as _trace
from .types import Result, Run, Step, new_id

V1_SURFACES = ("system_prompt",)


@dataclass
class Context:
    """What the agent sees at run time: the effective configuration of one version."""

    surfaces: dict[str, Any]
    model: dict[str, Any]
    version: str
    mode: str
    tools: dict[str, Callable[..., Any]] = field(default_factory=dict)
    # Deterministic per (example, sample) in eval mode; pass it to providers that accept a seed.
    seed: int = 0

    @property
    def system_prompt(self) -> str:
        return self.surfaces["system_prompt"]


AgentFn = Callable[[dict[str, Any], Context], Awaitable[Any]]


class Agent:
    def __init__(self, fn: AgentFn, name: str, surfaces: dict[str, Any], model: dict[str, Any] | None = None,
                 tools: list[Callable[..., Any]] | None = None, max_attempts: int = 1):
        unknown = set(surfaces) - set(V1_SURFACES)
        if unknown:
            raise ValueError(f"v1 supports only these learnable surfaces: {V1_SURFACES}; got {sorted(unknown)}")
        if "system_prompt" not in surfaces:
            raise ValueError("the adapter must expose the 'system_prompt' surface")
        self.fn = fn
        self.name = name
        self._surfaces = dict(surfaces)
        self.model = dict(model or {})
        self.tools = list(tools or [])
        self.tool_manifest = _trace.tool_manifest(self.tools)
        self.max_attempts = max(1, int(max_attempts))

    # -- learnable surfaces ---------------------------------------------------
    def get_surface(self, name: str) -> Any:
        return self._surfaces[name]

    def surfaces(self) -> dict[str, Any]:
        return dict(self._surfaces)

    def default_config(self) -> dict[str, Any]:
        """The full effective runtime configuration an AgentVersion pins."""
        return {
            "surfaces": copy.deepcopy(self._surfaces),
            "model": copy.deepcopy(self.model),
            "tools": copy.deepcopy(self.tool_manifest),
        }

    # -- running --------------------------------------------------------------
    async def run(self, task: dict[str, Any], config: dict[str, Any], version: str, mode: str = "online",
                  example_id: str | None = None, sample: int = 0) -> Run:
        """Run once. Retries are attempts nested inside one run id."""
        if config.get("tools", self.tool_manifest) != self.tool_manifest:
            raise RuntimeError("version pins a different tool manifest than this agent registers; refusing to run")
        ctx = Context(surfaces=dict(config["surfaces"]), model=dict(config.get("model", {})), version=version,
                      mode=mode, tools={m["name"]: t for m, t in zip(self.tool_manifest, sorted(
                          self.tools, key=lambda t: t.__recursa_tool__["name"]))},
                      seed=_seed(example_id, sample) if mode == "eval" else secrets.randbits(31))
        rec = _trace.RunRecorder(mode=mode)
        started = time.time()
        t0 = time.perf_counter()
        output: Any = None
        status = "error"
        error: str | None = None
        with _trace.recording(rec):
            for attempt in range(self.max_attempts):
                rec.attempt = attempt
                try:
                    out = await self.fn(task, ctx)
                    res = out if isinstance(out, Result) else Result(output=out)
                    output, status, error = res.output, res.status, None
                    if status == "ok":
                        break
                except asyncio.CancelledError:
                    status, error = "cancelled", "cancelled"
                    break
                except _trace.SandboxViolation as e:
                    status, error = "error", f"SandboxViolation: {e}"
                    break  # fail closed, never retry a sandbox breach
                except Exception as e:  # noqa: BLE001 - agent code may raise anything
                    status, error = "error", f"{type(e).__name__}: {e}"
        wall_ms = (time.perf_counter() - t0) * 1000
        steps: list[Step] = rec.steps
        return Run(
            id=new_id("run"), agent=self.name, agent_version=version, mode=mode, input=task, output=output,
            status=status, steps=steps, cost_usd=sum(s.cost_usd for s in steps),
            latency_ms=_run_latency(steps, wall_ms), started_at=started,
            context_snapshot={"surfaces": ctx.surfaces, "model": ctx.model}, error=error,
            example_id=example_id, sample=sample,
        )


def _seed(example_id: str | None, sample: int) -> int:
    return int(hashlib.sha256(f"{example_id}:{sample}".encode()).hexdigest()[:8], 16)


def _run_latency(steps: list[Step], wall_ms: float) -> float:
    # Simulators report per-step latency; when every step does, use their sum so
    # benchmark latency is deterministic. Otherwise wall-clock time is the truth.
    if steps and all(s.provider_metadata.get("simulated_latency") for s in steps):
        return max(sum(s.latency_ms for s in steps), 0.0)
    return wall_ms


def wrap(fn: AgentFn, name: str, surfaces: dict[str, Any], model: dict[str, Any] | None = None,
         tools: list[Callable[..., Any]] | None = None, max_attempts: int = 1) -> Agent:
    return Agent(fn, name=name, surfaces=surfaces, model=model, tools=tools, max_attempts=max_attempts)
