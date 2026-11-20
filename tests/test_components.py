import asyncio
import sqlite3

import pytest

import recursa as rc
from recursa.evals import EvalResult, ExampleResult
from recursa.learner import GateBudgetExhausted
from recursa.redact import Redactor
from recursa.stats import GateConfig, compare, holm
from recursa.store import Store
from recursa.types import Candidate, Run, new_id, now


# -- stats -----------------------------------------------------------------------------------------
def _res(succ, cost=1.0, lat=100.0, safe=1.0, n=200, samples=3, group_size=2):
    exs = [ExampleResult(f"e{i}", f"g{i // group_size}", [succ(i)] * samples, [safe] * samples, [cost] * samples,
                         [lat] * samples, [f"r{i}"] * samples, [None] * samples) for i in range(n)]
    return EvalResult("v", "h", "suite", "ds", samples, exs)


def test_holm():
    assert holm({"a": 0.001, "b": 0.02}, 0.05) == {"a": True, "b": True}
    assert holm({"a": 0.04, "b": 0.03}, 0.05) == {"a": False, "b": False}


def test_compare_refuses_small_samples():
    cfg = GateConfig(bootstrap=200)
    r = compare(_res(lambda i: 0, n=50), _res(lambda i: 1, n=50), cfg, 0.0025, 150)
    assert r["verdict"] == "insufficient_sample"


def test_compare_detects_real_gain_and_guardrail_breach():
    cfg = GateConfig(bootstrap=1000)
    base = _res(lambda i: 1.0 if i % 10 < 6 else 0.0)
    good = _res(lambda i: 1.0 if i % 10 < 8 else 0.0)
    assert compare(base, good, cfg, 0.0025, 150)["verdict"] == "eligible"
    pricey = _res(lambda i: 1.0 if i % 10 < 8 else 0.0, cost=1.5)
    r = compare(base, pricey, cfg, 0.0025, 150)
    assert r["verdict"] == "not_eligible" and not r["guardrails"]["cost"]["pass"]
    unsafe = _res(lambda i: 1.0 if i % 10 < 8 else 0.0, safe=0.0)
    assert not compare(base, unsafe, cfg, 0.0025, 150)["guardrails"]["safety"]["pass"]
    tiny = _res(lambda i: 1.0 if (i % 10 < 6 or i % 100 == 99) else 0.0)
    assert compare(base, tiny, cfg, 0.0025, 150)["verdict"] == "not_eligible"  # below min practical gain


def test_compare_rejects_mismatched_versions():
    a, b = _res(lambda i: 0), _res(lambda i: 1)
    b.suite_version = "other"
    with pytest.raises(ValueError):
        compare(a, b, GateConfig(bootstrap=10), 0.05, 1)


# -- store -------------------------------------------------------------------------------------------
def _run(**kw):
    d = dict(id=new_id("run"), agent="a", agent_version="v1", mode="online", input={"q": "mail me at a@b.co"},
             output="ok", status="ok", steps=[], cost_usd=0.0, latency_ms=1.0, started_at=now())
    d.update(kw)
    return Run(**d)


def test_runs_are_immutable_and_redacted(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    r = _run()
    s.put_run(r)
    assert s.get_run(r.id)["input"] == {"q": "mail me at [EMAIL]"}
    with pytest.raises(sqlite3.DatabaseError):
        s._x("UPDATE runs SET status='x' WHERE id=?", (r.id,))
    with pytest.raises(sqlite3.DatabaseError):
        s._x("DELETE FROM runs WHERE id=?", (r.id,))


def test_redaction_fails_closed(tmp_path):
    class Broken(Redactor):
        def _walk(self, obj):
            raise RuntimeError("boom")

    s = Store(str(tmp_path / "s.db"), redactor=Broken())
    r = _run()
    s.put_run(r)
    got = s.get_run(r.id)
    assert got["redaction"] == "unredacted_dropped" and got["input"] is None


def test_feedback_is_idempotent(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    r = _run()
    s.put_run(r)
    a = s.put_feedback(r.id, "explicit", -1, "fix", "u", "key-1")
    b = s.put_feedback(r.id, "explicit", -1, "fix", "u", "key-1")
    assert a == b and len(s.feedback_for(r.id)) == 1


def test_audit_chain_detects_tampering(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    s.audit("a", "x", "s1"); s.audit("b", "y", "s2")
    assert s.verify_audit_chain()
    s.db.execute("DROP TRIGGER audit_no_update")
    s.db.execute("UPDATE audit SET actor='evil' WHERE seq=1")
    assert not s.verify_audit_chain()


# -- adapter, tracing and sandbox ------------------------------------------------------------------
def test_tool_sandbox_and_retries():
    calls = {"live": 0, "mock": 0}

    @rc.tool(capability="read_only")
    async def lookup(x):
        return x * 2

    @rc.tool(capability="irreversible", mock=lambda amount: {"refunded": amount, "mock": True})
    async def refund(amount):
        calls["live"] += 1
        return {"refunded": amount}

    @rc.tool(capability="external_side_effect")
    async def email(to):
        return "sent"

    attempts = {"n": 0}

    async def agent(task, ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        v = await ctx.tools["lookup"](task["x"])
        out = await ctx.tools["refund"](v)
        if task.get("email"):
            await ctx.tools["email"]("someone")
        return out

    a = rc.wrap(agent, "t", {"system_prompt": "p"}, tools=[lookup, refund, email], max_attempts=3)
    cfg = a.default_config()
    run = asyncio.run(a.run({"x": 2}, cfg, "v1", mode="eval"))
    assert run.status == "ok" and run.output == {"refunded": 4, "mock": True} and calls["live"] == 0
    assert {s.attempt for s in run.steps} == {1}  # retry steps nest in one run id
    attempts["n"] = 1
    bad = asyncio.run(a.run({"x": 1, "email": True}, cfg, "v1", mode="eval"))
    assert bad.status == "error" and "SandboxViolation" in bad.error
    attempts["n"] = 1
    live = asyncio.run(a.run({"x": 1}, cfg, "v1", mode="online"))
    assert live.output == {"refunded": 2} and calls["live"] == 1
    with pytest.raises(ValueError):
        rc.wrap(agent, "t", {"system_prompt": "p"}, tools=[lambda: None])
    with pytest.raises(ValueError):
        rc.wrap(agent, "t", {"system_prompt": "p", "few_shots": []})  # not a v1 surface


# -- gate budget & once-per-candidate --------------------------------------------------------------
def test_gate_budget_and_single_evaluation(tmp_path):
    from conftest import make_project

    learner = make_project(tmp_path, budget=1)
    _, cfg = learner.current()

    def cand():
        c = Candidate(id=new_id("cand"), surface="system_prompt", baseline_version="v1",
                      proposed={"system_prompt": cfg["surfaces"]["system_prompt"] + "\n"}, insight_ids=[],
                      status="proposed", created_at=now())
        learner.store.put_candidate(c)
        return c.id

    c1 = cand()
    assert learner.gate(c1)["status"] == "gate_rejected"  # no real change -> no gain
    learner.store.set_candidate_status(c1, "proposed")
    with pytest.raises(GateBudgetExhausted):
        learner.gate(cand())
    with pytest.raises(GateBudgetExhausted):
        learner.cycle()
    learner.close()


def test_online_run_and_feedback(learner):
    run = asyncio.run(learner.run({"ticket": "My invoice is wrong. Email me at x@y.com"}))
    assert run.output["category"] == "billing"
    fid = learner.feedback(run.id, value=-1, correction={"priority": "urgent"}, idempotency_key="k1")
    assert learner.feedback(run.id, value=-1, idempotency_key="k1") == fid
    stored = learner.store.get_run(run.id)
    assert "x@y.com" not in str(stored["input"]) and stored["mode"] == "online"


def test_reflection_finds_every_cause_in_a_mixed_cluster():
    from recursa.reflect import BatchErrorAnalysis
    from recursa.types import Example

    texts = ([f"ticket {i}: production is down" for i in range(6)]
             + [f"ticket {i}: all of our users are blocked" for i in range(6, 12)]
             + [f"ticket {i}: hello there" for i in range(12, 40)])
    exs = [Example(id=f"e{i}", input={"t": t}, expected={"priority": "urgent" if i < 12 else "normal"})
           for i, t in enumerate(texts)]
    rows = [ExampleResult(e.id, e.id, [0.0 if i < 12 else 1.0], [1.0], [0.0], [1.0], [f"r{i}"],
                          [{"priority": "normal"}]) for i, e in enumerate(exs)]
    insights = BatchErrorAnalysis()(exs, EvalResult("v", "h", "s", "d", 1, rows))
    cues = {i.data["cue"] for i in insights}
    assert len(insights) == 2 and all(i.frequency == 6 for i in insights)
    assert any("production" in c or "down" in c for c in cues)
    assert any(w in c for c in cues for w in ("users", "blocked", "all"))
