"""Model clients that Recursa measures itself.

Wrap the Anthropic SDK client with ``rc.clients.AsyncAnthropic()`` (or ``Anthropic()``)
and call ``messages.create`` / ``beta.messages.create`` exactly as with the SDK. Each
call becomes a trace step whose tokens, cost and latency come from the provider's
response and Recursa's own clock, never from the agent. Only measured steps count
toward the cost and latency guardrails (see ``GateConfig.allow_self_reported_telemetry``).

Limits: the agent runs in the same Python process, so this stops honest mistakes
(forgotten or wrong usage reporting), not deliberately hostile agent code. Streaming
via ``stream=True`` is not measured yet; use non-streaming calls or ``rc.trace.model``.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from . import trace as _trace
from .llm import PRICES, cost_usd
from .types import Step


def _record(kwargs: dict[str, Any], resp: Any, started: float, elapsed_ms: float, error: str | None) -> None:
    rec = _trace.current()
    if rec is None:
        return
    model = getattr(resp, "model", None) or kwargs.get("model", "unknown")  # served model, after any fallback
    step = Step(kind="model", name=model, input={"messages": kwargs.get("messages"), "system": kwargs.get("system")},
                attempt=rec.attempt, started_at=started, latency_ms=elapsed_ms, error=error)
    if resp is not None:
        u = resp.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        step.input_tokens, step.output_tokens = u.input_tokens, u.output_tokens
        step.cost_usd = cost_usd(model, u.input_tokens, u.output_tokens, cache_read, cache_write)
        step.output = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        step.provider_metadata.update(stop_reason=resp.stop_reason, requested_model=kwargs.get("model"),
                                      cache_read_tokens=cache_read, cache_write_tokens=cache_write)
    # Tokens with no known price would read as $0 and slip past the cost guardrail.
    step.measured = resp is not None and model in PRICES
    if resp is not None and model not in PRICES:
        step.provider_metadata["unpriced_model"] = True
    rec.steps.append(step)


def _check(kwargs: dict[str, Any]) -> None:
    if kwargs.get("stream"):
        raise NotImplementedError("rc.clients does not measure stream=True calls yet; use a non-streaming call")


class _AsyncMessages:
    def __init__(self, inner: Any):
        self._inner = inner

    async def create(self, **kwargs: Any) -> Any:
        _check(kwargs)
        started, t0 = time.time(), time.perf_counter()
        try:
            resp = await self._inner.create(**kwargs)
        except Exception as e:
            _record(kwargs, None, started, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
            raise
        _record(kwargs, resp, started, (time.perf_counter() - t0) * 1000, None)
        return resp


class _Messages:
    def __init__(self, inner: Any):
        self._inner = inner

    def create(self, **kwargs: Any) -> Any:
        _check(kwargs)
        started, t0 = time.time(), time.perf_counter()
        try:
            resp = self._inner.create(**kwargs)
        except Exception as e:
            _record(kwargs, None, started, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
            raise
        _record(kwargs, resp, started, (time.perf_counter() - t0) * 1000, None)
        return resp


class AsyncAnthropic:
    """Measured drop-in for ``anthropic.AsyncAnthropic`` (messages.create, beta.messages.create)."""

    def __init__(self, client: Any = None, **kwargs: Any):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(**kwargs)
        self.raw = client
        self.messages = _AsyncMessages(client.messages)
        self.beta = SimpleNamespace(messages=_AsyncMessages(client.beta.messages))


class Anthropic:
    """Measured drop-in for ``anthropic.Anthropic`` (messages.create, beta.messages.create)."""

    def __init__(self, client: Any = None, **kwargs: Any):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(**kwargs)
        self.raw = client
        self.messages = _Messages(client.messages)
        self.beta = SimpleNamespace(messages=_Messages(client.beta.messages))
