"""Model clients that Recursa measures itself.

Wrap the Anthropic SDK client with ``rc.clients.AsyncAnthropic()`` (or ``Anthropic()``)
and call ``messages.create`` / ``messages.stream`` (and the ``beta.messages`` versions)
exactly as with the SDK. Each call becomes a trace step whose tokens, cost and latency
come from the provider's response and Recursa's own clock, never from the agent. Only
measured steps count toward the cost and latency guardrails (see
``GateConfig.allow_self_reported_telemetry``).

Streaming is measured both ways: ``messages.stream(...)`` (the SDK helper) and
``messages.create(stream=True)`` (raw events). Latency is the time until the stream
ends; time to first event is recorded as ``provider_metadata["ttft_ms"]``. A stream
closed before ``message_stop`` is recorded with an error and whatever usage arrived.

Limits: the agent runs in the same Python process, so this stops honest mistakes
(forgotten or wrong usage reporting), not deliberately hostile agent code.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from . import trace as _trace
from .llm import PRICES, cost_usd
from .types import Step

EARLY_CLOSE = "stream closed before message_stop"


class _Clock:
    def __init__(self) -> None:
        self.started, self._t0 = time.time(), time.perf_counter()
        self.first: float | None = None

    def mark(self) -> None:
        if self.first is None:
            self.first = (time.perf_counter() - self._t0) * 1000

    def elapsed(self) -> float:
        return (time.perf_counter() - self._t0) * 1000


def _record(rec: Any, kwargs: dict[str, Any], resp: Any, clock: _Clock, error: str | None,
            streamed: bool = False) -> None:
    if rec is None:
        return
    model = getattr(resp, "model", None) or kwargs.get("model", "unknown")  # served model, after any fallback
    step = Step(kind="model", name=model, input={"messages": kwargs.get("messages"), "system": kwargs.get("system")},
                attempt=rec.attempt, started_at=clock.started, latency_ms=clock.elapsed(), error=error)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        step.input_tokens, step.output_tokens = usage.input_tokens or 0, usage.output_tokens or 0
        step.cost_usd = cost_usd(model, step.input_tokens, step.output_tokens, cache_read, cache_write)
        step.output = [b.text for b in (resp.content or []) if getattr(b, "type", "") == "text"]
        step.provider_metadata.update(stop_reason=resp.stop_reason, requested_model=kwargs.get("model"),
                                      cache_read_tokens=cache_read, cache_write_tokens=cache_write)
    if streamed:
        step.provider_metadata.update(streamed=True, ttft_ms=clock.first)
    # Tokens with no known price would read as $0 and slip past the cost guardrail.
    step.measured = usage is not None and model in PRICES
    if usage is not None and model not in PRICES:
        step.provider_metadata["unpriced_model"] = True
    rec.steps.append(step)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


# -- raw event streams: create(stream=True) -----------------------------------------------
class _Accumulator:
    """Rebuilds model, usage, text and stop reason from raw Messages stream events."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                                     cache_creation_input_tokens=0)
        self.text: list[str] = []
        self.stop_reason: str | None = None
        self.started = self.stopped = False

    def feed(self, event: Any) -> None:
        kind = getattr(event, "type", None)
        if kind == "message_start":
            self.started = True
            msg = event.message
            self.model = msg.model
            self._usage(msg.usage)
        elif kind == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
            self.text.append(event.delta.text)
        elif kind == "message_delta":
            self.stop_reason = getattr(event.delta, "stop_reason", None) or self.stop_reason
            self._usage(event.usage)
        elif kind == "message_stop":
            self.stopped = True

    def _usage(self, u: Any) -> None:
        for name in vars(self.usage):
            v = getattr(u, name, None)
            if v is not None:  # message_delta carries cumulative counts
                setattr(self.usage, name, v)

    def message(self) -> Any:
        if not self.started:
            return None
        return SimpleNamespace(model=self.model, usage=self.usage, stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text="".join(self.text))])


class _RawStream:
    """Proxies the SDK's Stream; records the step once, when iteration ends or it closes."""

    def __init__(self, inner: Any, rec: Any, kwargs: dict[str, Any], clock: _Clock):
        self._inner, self._rec, self._kwargs, self._clock = inner, rec, kwargs, clock
        self._acc, self._done = _Accumulator(), False

    def _finish(self, error: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        if error is None and not self._acc.stopped:
            error = EARLY_CLOSE
        _record(self._rec, self._kwargs, self._acc.message(), self._clock, error, streamed=True)

    def _see(self, event: Any) -> None:
        self._clock.mark()
        self._acc.feed(event)

    def __iter__(self):
        try:
            for event in self._inner:
                self._see(event)
                yield event
        except GeneratorExit:
            self._finish()
            raise
        except Exception as e:
            self._finish(_err(e))
            raise
        self._finish()

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._inner.close()
        self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncRawStream(_RawStream):
    async def __aiter__(self):
        try:
            async for event in self._inner:
                self._see(event)
                yield event
        except GeneratorExit:
            self._finish()
            raise
        except Exception as e:
            self._finish(_err(e))
            raise
        self._finish()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:  # type: ignore[override]
        await self._inner.close()
        self._finish()


# -- SDK stream helper: messages.stream(...) ------------------------------------------------
def _time_first_event(stream: Any, clock: _Clock, is_async: bool) -> None:
    """text_stream, iteration and get_final_message() all draw from ``_iterator``."""
    inner = getattr(stream, "_iterator", None)
    if inner is None:
        return

    if is_async:
        async def timed():
            async for item in inner:
                clock.mark()
                yield item
    else:
        def timed():
            for item in inner:
                clock.mark()
                yield item
    stream._iterator = timed()


def _snapshot(stream: Any) -> Any:
    try:
        return stream.current_message_snapshot
    except (AssertionError, AttributeError):
        return None


class _StreamManager:
    def __init__(self, inner_manager: Any, kwargs: dict[str, Any]):
        self._mgr, self._kwargs = inner_manager, kwargs
        self._rec, self._clock, self._stream = _trace.current(), _Clock(), None

    def _finish(self, exc: BaseException | None) -> None:
        msg = _snapshot(self._stream) if self._stream is not None else None
        error = _err(exc) if exc is not None else None
        if error is None and (msg is None or msg.stop_reason is None):
            error = EARLY_CLOSE
        _record(self._rec, self._kwargs, msg, self._clock, error, streamed=True)

    def __enter__(self):
        try:
            self._stream = self._mgr.__enter__()
        except Exception as e:
            self._finish(e)
            raise
        _time_first_event(self._stream, self._clock, is_async=False)
        return self._stream

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return self._mgr.__exit__(exc_type, exc, tb)
        finally:
            self._finish(exc)


class _AsyncStreamManager(_StreamManager):
    async def __aenter__(self):
        try:
            self._stream = await self._mgr.__aenter__()
        except Exception as e:
            self._finish(e)
            raise
        _time_first_event(self._stream, self._clock, is_async=True)
        return self._stream

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._mgr.__aexit__(exc_type, exc, tb)
        finally:
            self._finish(exc)


# -- messages resources ---------------------------------------------------------------------
class _Messages:
    def __init__(self, inner: Any):
        self._inner = inner

    def create(self, **kwargs: Any) -> Any:
        rec, clock = _trace.current(), _Clock()
        try:
            resp = self._inner.create(**kwargs)
        except Exception as e:
            _record(rec, kwargs, None, clock, _err(e), streamed=bool(kwargs.get("stream")))
            raise
        if kwargs.get("stream"):
            return _RawStream(resp, rec, kwargs, clock)
        _record(rec, kwargs, resp, clock, None)
        return resp

    def stream(self, **kwargs: Any) -> _StreamManager:
        return _StreamManager(self._inner.stream(**kwargs), kwargs)

    def __getattr__(self, name: str) -> Any:  # count_tokens, batches, ... pass through unmeasured
        return getattr(self._inner, name)


class _AsyncMessages(_Messages):
    async def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
        rec, clock = _trace.current(), _Clock()
        try:
            resp = await self._inner.create(**kwargs)
        except Exception as e:
            _record(rec, kwargs, None, clock, _err(e), streamed=bool(kwargs.get("stream")))
            raise
        if kwargs.get("stream"):
            return _AsyncRawStream(resp, rec, kwargs, clock)
        _record(rec, kwargs, resp, clock, None)
        return resp

    def stream(self, **kwargs: Any) -> _AsyncStreamManager:  # type: ignore[override]
        return _AsyncStreamManager(self._inner.stream(**kwargs), kwargs)


class AsyncAnthropic:
    """Measured drop-in for ``anthropic.AsyncAnthropic`` (messages and beta.messages)."""

    def __init__(self, client: Any = None, **kwargs: Any):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(**kwargs)
        self.raw = client
        self.messages = _AsyncMessages(client.messages)
        self.beta = SimpleNamespace(messages=_AsyncMessages(client.beta.messages))


class Anthropic:
    """Measured drop-in for ``anthropic.Anthropic`` (messages and beta.messages)."""

    def __init__(self, client: Any = None, **kwargs: Any):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(**kwargs)
        self.raw = client
        self.messages = _Messages(client.messages)
        self.beta = SimpleNamespace(messages=_Messages(client.beta.messages))
