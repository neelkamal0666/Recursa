"""Thin helper for calling Claude (used by the prompt rewriter and the advisory judge)."""
from __future__ import annotations

from typing import Any

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output). Used for cost accounting in traces.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}


class LLMRefusal(RuntimeError):
    pass


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0) -> float:
    """USD for one call. Cache reads bill at 5% of input, 5-minute cache writes at 125%."""
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * pin + output_tokens * pout + cache_read_tokens * pin * 0.05
            + cache_write_tokens * pin * 1.25) / 1_000_000


def complete(prompt: str, system: str | None = None, model: str = DEFAULT_MODEL, effort: str = "high",
             max_tokens: int = 16000) -> tuple[str, dict[str, Any]]:
    """One Messages API call with server-side refusal fallbacks. Returns (text, usage)."""
    import anthropic

    client = anthropic.Anthropic()
    kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, output_config={"effort": effort},
                                  messages=[{"role": "user", "content": prompt}])
    if system:
        kwargs["system"] = system
    resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    if resp.stop_reason == "refusal":
        raise LLMRefusal(str(getattr(resp, "stop_details", "") or "refused"))
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
             "model": resp.model, "stop_reason": resp.stop_reason}
    usage["cost_usd"] = cost_usd(model, usage["input_tokens"], usage["output_tokens"])
    return text, usage
