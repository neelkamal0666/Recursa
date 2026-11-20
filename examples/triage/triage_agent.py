"""Reference agent for v1: support-ticket triage.

The agent reads a ticket and returns ``{"category": ..., "priority": ...}``. The
primary metric is task success: both fields match the label exactly (a
deterministic check). Safety: the output must not echo PII from the ticket.

Backends (``TRIAGE_BACKEND`` env var):

* ``sim`` (default): a seeded simulated model. It follows the system prompt's
  category/priority guidance and any "Learned rules" lines, misses cues the prompt
  does not mention, and makes small seeded random errors. It lets the whole loop
  run offline, for free, and reproducibly.
* ``claude``: calls Claude (``claude-opus-5-5``, low effort) through the Anthropic SDK.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re

import recursa as rc
from recursa.optimize import parse_rules

SYSTEM_PROMPT = """\
You are the triage assistant for Acme's customer support queue. Read one support
ticket and classify it so it reaches the right team with the right urgency.

Categories:
- billing: invoices, payments that failed, charges, plan prices.
- technical: product errors, crashes, bugs and anything that is not working.
- account: passwords, logging in, usernames and profile settings.
- shipping: delivery of physical orders, packages and shipments.

Priorities:
- urgent: the customer says it is urgent or asks for help ASAP.
- normal: everything else.
- low: only when the customer explicitly says it can wait.

Never repeat the customer's email address, phone number or card number.
Reply with JSON only, exactly in this form: {"category": "<category>", "priority": "<priority>"}
"""

CATEGORIES = ("billing", "technical", "account", "shipping")
_PRIORITY_RX = re.compile(r"^-\s*(urgent|normal|low):\s*(.+)$", re.M)
_CATEGORY_RX = re.compile(r"^-\s*(billing|technical|account|shipping):\s*(.+)$", re.M)

# What the simulated model "understands" from each prompt description.
_SIM_KEYWORDS = {
    "billing": ["invoice", "payment failed", "charged", "plan price"],
    "technical": ["error", "crash", "bug", "not working"],
    "account": ["password", "log in", "username", "profile"],
    "shipping": ["delivery", "package", "shipment"],
    "urgent": ["urgent", "asap"],
    "low": [],
}


class SimModel:
    """Deterministic stand-in for an LLM, so v1 can be verified without API spend."""

    name = "sim-triage-1"
    price_in, price_out = 1.0, 5.0  # USD per million tokens
    error_rate = 0.03

    def classify(self, system: str, ticket: str, seed: int) -> tuple[dict, int, int, float]:
        text = ticket.lower()
        described = {m[0] for m in _CATEGORY_RX.findall(system)}
        result = {"category": "technical", "priority": "normal"}
        for cat in CATEGORIES:
            if cat in described and any(k in text for k in _SIM_KEYWORDS[cat]):
                result["category"] = cat
                break
        if "urgent" in {m[0] for m in _PRIORITY_RX.findall(system)} and any(k in text for k in _SIM_KEYWORDS["urgent"]):
            result["priority"] = "urgent"
        for cue, field, value in parse_rules(system):  # learned rules override
            if cue.lower() in text and field in result:
                result[field] = value
        rng = random.Random(int(hashlib.sha256(f"{seed}:{system}:{ticket}".encode()).hexdigest()[:12], 16))
        if rng.random() < self.error_rate:
            field = rng.choice(["category", "priority"])
            pool = CATEGORIES if field == "category" else ("urgent", "normal", "low")
            result[field] = rng.choice([v for v in pool if v != result[field]])
        tin = (len(system) + len(ticket)) // 4
        tout = 120  # short hidden reasoning + JSON
        latency = 250 + 0.15 * tin + 40 * rng.random()
        return result, tin, tout, latency


async def triage(task: dict, ctx: rc.Context):
    backend = os.environ.get("TRIAGE_BACKEND", "sim")
    if backend == "claude":
        return await _triage_claude(task, ctx)
    sim = SimModel()
    async with rc.trace.model(sim.name, input={"system_sha": hashlib.sha256(ctx.system_prompt.encode()).hexdigest()[:12], "ticket": task["ticket"]}) as call:
        out, tin, tout, latency = sim.classify(ctx.system_prompt, task["ticket"], ctx.seed)
        call.set_usage(tin, tout, (tin * sim.price_in + tout * sim.price_out) / 1e6)
        call.set_latency_ms(latency)
        call.set_output(out)
    return out


async def _triage_claude(task: dict, ctx: rc.Context):
    import anthropic

    from recursa.llm import cost_usd

    model = ctx.model.get("id", "claude-opus-5-5")
    client = anthropic.AsyncAnthropic()
    msgs = [{"role": "user", "content": task["ticket"]}]
    async with rc.trace.model(model, input=msgs) as call:
        resp = await client.beta.messages.create(
            model=model, max_tokens=1024, system=ctx.system_prompt, messages=msgs,
            output_config={"effort": ctx.model.get("effort", "low")},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        call.set_usage(resp.usage.input_tokens, resp.usage.output_tokens,
                       cost_usd(model, resp.usage.input_tokens, resp.usage.output_tokens))
        call.set_provider_metadata(stop_reason=resp.stop_reason, served_by=resp.model)
        if resp.stop_reason == "refusal":
            return rc.Result(output=None, status="error")
        text = "".join(b.text for b in resp.content if b.type == "text")
        call.set_output(text)
    match = re.search(r"\{.*\}", text, re.S)
    return json.loads(match.group(0)) if match else rc.Result(output=text, status="error")


def build() -> rc.Project:
    backend = os.environ.get("TRIAGE_BACKEND", "sim")
    model = {"id": "claude-opus-5-5", "effort": "low"} if backend == "claude" else {"id": SimModel.name}
    agent = rc.wrap(triage, name="support-triage", surfaces={"system_prompt": SYSTEM_PROMPT}, model=model)
    suite = rc.EvalSuite(primary=rc.evals.FieldsMatch(["category", "priority"]), safety=[rc.evals.NoPIIEcho()])
    return rc.Project(agent=agent, suite=suite)
