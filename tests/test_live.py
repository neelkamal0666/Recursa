"""Live check of rc.clients against the real Claude API. Costs a few cents.

Skipped unless RECURSA_LIVE=1 and ANTHROPIC_API_KEY are set:

    RECURSA_LIVE=1 ANTHROPIC_API_KEY=... python -m pytest tests/test_live.py -v -s
"""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("RECURSA_LIVE") == "1" and os.environ.get("ANTHROPIC_API_KEY")),
                                reason="set RECURSA_LIVE=1 and ANTHROPIC_API_KEY to run against the real API")

N_EXAMPLES = 5


def test_triage_agent_on_claude_records_measured_telemetry(monkeypatch):
    import generate  # examples/triage, put on sys.path by conftest
    import triage_agent

    from recursa.llm import PRICES

    monkeypatch.setenv("TRIAGE_BACKEND", "claude")
    project = triage_agent.build()
    agent = project.agent
    runs = []
    for i, ex in enumerate(generate.generate(N_EXAMPLES, 11, "live")):
        runs.append(asyncio.run(agent.run(ex["input"], agent.default_config(), "live", mode="eval",
                                          example_id=ex["id"], sample=0)))
    total = 0.0
    for run in runs:
        assert run.telemetry == "measured", run.steps
        step = next(s for s in run.steps if s.kind == "model")
        assert step.measured and step.name in PRICES
        assert step.input_tokens > 0 and step.output_tokens > 0 and step.latency_ms > 0
        pin, pout = PRICES[step.name]
        md = step.provider_metadata
        expected = (step.input_tokens * pin + step.output_tokens * pout + md["cache_read_tokens"] * pin * 0.05
                    + md["cache_write_tokens"] * pin * 1.25) / 1e6
        assert step.cost_usd == pytest.approx(expected)
        total += step.cost_usd
        print(f"{run.example_id}: {step.name} in={step.input_tokens} out={step.output_tokens} "
              f"${step.cost_usd:.5f} {step.latency_ms:.0f}ms status={run.status}")
    print(f"total measured cost: ${total:.4f} for {len(runs)} runs")
    assert sum(r.status == "ok" for r in runs) >= N_EXAMPLES - 1
