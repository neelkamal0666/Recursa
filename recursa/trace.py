"""Instrumentation SDK (fidelity level 1).

Agents record model calls with ``rc.trace.model(...)`` and declare tools with
``@rc.tool(capability=...)``. Both write steps onto the run that is active in the
current asyncio context, so concurrent runs never mix their traces.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from .types import Step

CAPABILITIES = ("read_only", "reversible_write", "external_side_effect", "irreversible")


class SandboxViolation(RuntimeError):
    """A tool tried to do something its capability class forbids in this mode."""


@dataclass
class RunRecorder:
    mode: str  # online | eval
    steps: list[Step] = field(default_factory=list)
    attempt: int = 0
    opaque_tools: int = 0
    sandbox_violation: str | None = None


_current: contextvars.ContextVar[RunRecorder | None] = contextvars.ContextVar("recursa_run", default=None)


def current() -> RunRecorder | None:
    return _current.get()


@contextmanager
def recording(rec: RunRecorder):
    token = _current.set(rec)
    try:
        yield rec
    finally:
        _current.reset(token)


class ModelCall:
    """Handle yielded by ``rc.trace.model``; fill in usage and output."""

    def __init__(self, step: Step):
        self._step = step
        self._latency_override: float | None = None

    def set_usage(self, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0) -> None:
        self._step.input_tokens = int(input_tokens)
        self._step.output_tokens = int(output_tokens)
        self._step.cost_usd = float(cost_usd)

    def set_output(self, output: Any) -> None:
        self._step.output = output

    def set_latency_ms(self, ms: float) -> None:
        """Override measured latency (used by deterministic simulators)."""
        self._latency_override = float(ms)
        self._step.provider_metadata["simulated_latency"] = True

    def set_provider_metadata(self, **meta: Any) -> None:
        self._step.provider_metadata.update(meta)


@asynccontextmanager
async def model(name: str, input: Any = None):
    """Record one model call. Usage: ``async with rc.trace.model("claude-opus-5-5", input=msgs) as call:``"""
    rec = current()
    step = Step(kind="model", name=name, input=input, started_at=time.time(), attempt=rec.attempt if rec else 0)
    call = ModelCall(step)
    t0 = time.perf_counter()
    try:
        yield call
    except BaseException as e:  # record and re-raise
        step.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        measured = (time.perf_counter() - t0) * 1000
        step.latency_ms = call._latency_override if call._latency_override is not None else measured
        if rec is not None:
            rec.steps.append(step)


def tool(capability: str, mock: Callable[..., Any] | None = None, name: str | None = None):
    """Register a tool with a capability class.

    In ``eval`` mode only ``read_only`` tools run live; every other class runs its
    ``mock`` (a recorded fixture or fake). A non-read-only tool without a mock fails
    the run closed with ``SandboxViolation``.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"capability must be one of {CAPABILITIES}")

    def deco(fn: Callable[..., Any]):
        tool_name = name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            rec = current()
            mode = rec.mode if rec else "online"
            impl = fn
            if mode == "eval" and capability != "read_only":
                if mock is None:
                    msg = f"tool '{tool_name}' ({capability}) has no mock; refusing to run live in eval mode"
                    if rec:
                        rec.sandbox_violation = msg
                    raise SandboxViolation(msg)
                impl = mock
            step = Step(kind="tool", name=tool_name, input={"args": list(args), "kwargs": kwargs},
                        started_at=time.time(), capability=capability, attempt=rec.attempt if rec else 0)
            t0 = time.perf_counter()
            try:
                out = impl(*args, **kwargs)
                if inspect.isawaitable(out):
                    out = await out
                step.output = out
                return out
            except BaseException as e:
                step.error = f"{type(e).__name__}: {e}"
                raise
            finally:
                step.latency_ms = (time.perf_counter() - t0) * 1000
                if rec is not None:
                    rec.steps.append(step)

        wrapper.__recursa_tool__ = {"name": tool_name, "capability": capability, "has_mock": mock is not None}
        return wrapper

    return deco


def tool_manifest(tools: list[Callable[..., Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        meta = getattr(t, "__recursa_tool__", None)
        if meta is None:
            raise ValueError(f"{t!r} is not decorated with @rc.tool; register every tool the agent calls")
        out.append(dict(meta))
    return sorted(out, key=lambda m: m["name"])
