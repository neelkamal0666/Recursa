import asyncio
import json
import os
import sqlite3

import pytest

import recursa as rc
from recursa.evals import EvalResult, ExampleResult
from recursa.learner import GateBudgetExhausted
from recursa.redact import Redactor
from recursa.stats import GateConfig, compare, holm
from recursa.store import Store
from recursa.types import Candidate, Run, new_id, now

from conftest import make_project


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
    for r in (base, good):  # telemetry defaults to empty in these synthetic results (= measured)
        assert all(not e.telemetry for e in r.examples)
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
    assert run.status == "ok" and "category" in run.output  # online seeds are random; the sim errs 3% of the time
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


# -- measured telemetry (rc.clients) ------------------------------------------------------------------
class _FakeMessages:
    def __init__(self, model="claude-opus-5-5", fail=False):
        self.model, self.fail = model, fail

    async def create(self, **kw):
        from types import SimpleNamespace as NS

        if self.fail:
            raise RuntimeError("overloaded")
        await asyncio.sleep(0.01)
        return NS(model=self.model, stop_reason="end_turn",
                  content=[NS(type="text", text='{"category": "billing", "priority": "normal"}')],
                  usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=500,
                           cache_creation_input_tokens=0))


def _fake_client(**kw):
    from types import SimpleNamespace as NS

    m = _FakeMessages(**kw)
    return NS(messages=m, beta=NS(messages=m))


def _measured_agent(fake, report_usage=False):
    client = rc.clients.AsyncAnthropic(client=fake)

    async def agent(task, ctx):
        resp = await client.beta.messages.create(model="claude-opus-5-5", max_tokens=100,
                                                 messages=[{"role": "user", "content": task["t"]}])
        if report_usage:  # an agent that also self-reports (here: wrongly, $0) through rc.trace
            async with rc.trace.model("claude-opus-5-5") as call:
                call.set_usage(0, 0, 0.0)
        return json.loads(resp.content[0].text)

    return rc.wrap(agent, "m", {"system_prompt": "p"})


def test_clients_measure_usage_cost_and_latency():
    a = _measured_agent(_fake_client())
    run = asyncio.run(a.run({"t": "hi"}, a.default_config(), "v1", mode="eval", example_id="e", sample=0))
    (step,) = run.steps
    assert run.telemetry == "measured" and step.measured
    assert (step.input_tokens, step.output_tokens) == (1000, 200)
    # 1000 in @ $4/M + 200 out @ $20/M + 500 cache reads @ 5% of input price
    assert step.cost_usd == pytest.approx((1000 * 4 + 200 * 20 + 500 * 4 * 0.05) / 1e6)
    assert run.cost_usd == step.cost_usd and step.latency_ms >= 10


def test_self_reporting_or_unpriced_models_are_not_measured():
    a = _measured_agent(_fake_client(), report_usage=True)
    run = asyncio.run(a.run({"t": "hi"}, a.default_config(), "v1", mode="eval"))
    assert run.telemetry == "self_reported"
    b = _measured_agent(_fake_client(model="some-unpriced-model"))
    run = asyncio.run(b.run({"t": "hi"}, b.default_config(), "v1", mode="eval"))
    assert run.telemetry == "self_reported" and run.steps[0].provider_metadata["unpriced_model"]
    c = _measured_agent(_fake_client(fail=True))
    run = asyncio.run(c.run({"t": "hi"}, c.default_config(), "v1", mode="eval"))
    assert run.status == "error" and run.steps[0].error.startswith("RuntimeError")


def test_gate_distrusts_self_reported_cost_and_latency():
    base = _res(lambda i: 1.0 if i % 10 < 6 else 0.0)
    good = _res(lambda i: 1.0 if i % 10 < 8 else 0.0)
    for r in (base, good):
        for e in r.examples:
            e.telemetry = ["self_reported"] * r.samples
    strict = compare(base, good, GateConfig(bootstrap=1000), 0.0025, 150)
    assert strict["verdict"] == "not_eligible"
    assert not strict["guardrails"]["cost"]["pass"] and not strict["telemetry"]["trusted"]
    assert any("rc.clients" in reason for reason in strict["reasons"])
    lenient = compare(base, good, GateConfig(bootstrap=1000, allow_self_reported_telemetry=True), 0.0025, 150)
    assert lenient["verdict"] == "eligible"
    too_few = compare(base, good, GateConfig(bootstrap=500, allow_self_reported_telemetry=True), 0.0025, 150)
    assert too_few["verdict"] == "insufficient_bootstrap"


def test_strict_project_rejects_a_self_reporting_agent(tmp_path):
    from conftest import make_project

    learner = make_project(tmp_path, self_reported=False)  # the simulated model self-reports
    out = learner.cycle()
    assert set(out["candidates"].values()) == {"gate_rejected"}
    learner.close()


def test_store_migrates_databases_without_telemetry(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, agent TEXT, agent_version TEXT, mode TEXT, example_id TEXT, "
                "sample INTEGER, status TEXT, input_json TEXT, output_json TEXT, steps_json TEXT, cost_usd REAL, "
                "latency_ms REAL, started_at REAL, context_json TEXT, error TEXT, redaction TEXT)")
    old.close()
    s = Store(path)
    r = _run()
    s.put_run(r)
    assert s.get_run(r.id)["telemetry"] == "measured"


# -- schema migrations ----------------------------------------------------------------------
def test_fresh_store_records_every_migration(tmp_path):
    from recursa import migrations

    s = Store(str(tmp_path / "s.db"))
    st = migrations.status(s.db)
    assert st["current"] == st["latest"] == len(migrations.load())
    assert st["pending"] == [] and s.applied_migrations == [m.label for m in migrations.load()]
    with pytest.raises(sqlite3.IntegrityError):
        s.db.execute("DELETE FROM schema_migrations")
    s.close()
    assert Store(str(tmp_path / "s.db")).applied_migrations == []  # idempotent on reopen


def test_legacy_store_with_telemetry_is_adopted_without_reapplying(tmp_path):
    path = str(tmp_path / "legacy.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, agent TEXT, agent_version TEXT, mode TEXT, example_id TEXT, "
                "sample INTEGER, status TEXT, input_json TEXT, output_json TEXT, steps_json TEXT, cost_usd REAL, "
                "latency_ms REAL, started_at REAL, context_json TEXT, error TEXT, redaction TEXT, telemetry TEXT)")
    old.close()
    s = Store(path)
    assert "0002_run_telemetry" not in s.applied_migrations  # its column already existed
    assert s.applied_migrations[0] == "0001_initial"
    r = _run()
    s.put_run(r)
    assert s.get_run(r.id)["telemetry"] == "measured"


def test_store_refuses_out_of_date_schema_without_auto_migrate(tmp_path):
    from recursa.migrations import SchemaError

    path = str(tmp_path / "s.db")
    with pytest.raises(SchemaError, match="recursa db migrate"):
        Store(path, auto_migrate=False)
    Store(path).close()
    Store(path, auto_migrate=False).close()  # up to date: opens fine


def test_store_refuses_newer_or_edited_schema(tmp_path):
    from recursa.migrations import SchemaError

    newer = str(tmp_path / "newer.db")
    Store(newer).db.execute("INSERT INTO schema_migrations VALUES (99, 'future', 'x', 0)")
    with pytest.raises(SchemaError, match="upgrade Recursa"):
        Store(newer)

    edited = str(tmp_path / "edited.db")
    db = sqlite3.connect(edited)
    db.executescript("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                     "checksum TEXT NOT NULL, applied_at REAL NOT NULL);"
                     "INSERT INTO schema_migrations VALUES (1, 'initial', 'not-the-real-checksum', 0);")
    db.close()
    with pytest.raises(SchemaError, match="forward-only"):
        Store(edited)


def test_cli_db_status_and_migrate(tmp_path, capsys):
    from recursa.cli import main

    learner = make_project(tmp_path)
    cfg = os.path.join(learner.cfg.root, "recursa.yaml")
    learner.close()
    main(["--config", cfg, "db", "status"])
    out = json.loads(capsys.readouterr().out)
    assert out["records"]["pending"] == [] and out["records"]["current"] == out["records"]["latest"]
    main(["--config", cfg, "db", "migrate"])
    assert json.loads(capsys.readouterr().out)["records"]["applied"] == []


def test_store_transfer_is_idempotent_and_charges_only_new_runs(tmp_path):
    src, dst = Store(str(tmp_path / "w.db")), Store(str(tmp_path / "m.db"))
    a, b = _run(), _run()
    src.put_run(a)
    src.put_run(b)
    dst.put_run(a)  # already present in the destination
    recs = src.export_records()
    assert dst.import_records(recs) == pytest.approx(b.cost_usd)
    assert dst.import_records(recs) == 0.0
    assert dst.get_run(b.id)["id"] == b.id


def test_no_sql_or_backend_classes_outside_the_store():
    """Backends are swappable: only recursa/store/ (and migrations) may touch SQLite or
    construct a backend; everything else uses the protocols and the open_* factories."""
    import re
    from pathlib import Path

    pkg = Path(rc.__file__).parent
    offenders = []
    for path in pkg.rglob("*.py"):
        rel = path.relative_to(pkg).as_posix()
        if rel.startswith(("store/", "migrations/")) or rel == "isolation.py":
            continue
        text = path.read_text()
        if re.search(r"import sqlite3|\._q\(|\._x\(|\.db\.execute|\b(Local)?Store\(|import (Local)?Store\b", text):
            offenders.append(rel)
    assert offenders == []


def test_store_config_validation():
    import tempfile
    from pathlib import Path

    from recursa.project import load_config

    base = "project: p\nfactory: m:f\n"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "recursa.yaml"
        for extra, msg in (("store:\n  colour: red\n", "colour"), ("store:\n  url: ftp://x\n", "http"),
                           ("store:\n  url: https://x\n  api_key: rk_1\n", "environment variable")):
            p.write_text(base + extra)
            with pytest.raises(ValueError, match=msg):
                load_config(str(p))
        p.write_text(base + "store:\n  url: https://recursa.internal\n  key_env: MY_KEY\n")
        assert load_config(str(p)).store["url"] == "https://recursa.internal"
        p.write_text(base + "store:\n  auto_migrate: false\n")
        assert load_config(str(p)).store == {"auto_migrate": False}
