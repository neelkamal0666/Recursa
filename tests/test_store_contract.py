"""Contract every storage backend must pass (semantics in recursa/store/base.py).

Only protocol methods are used here, never SQL, so every backend runs the same
tests: SQLite, Postgres, and the HTTP client talking to Recursa Server.
Backend-specific guarantees (e.g. SQLite triggers rejecting raw UPDATEs) are tested
next to the backend instead.
"""
import threading

import pytest

from recursa.redact import RedactionError, Redactor
from recursa.store import LocalStore, RecordStore, SealedStore
from recursa.types import Candidate, Run, Score, Step, content_hash, new_id, now


def _local(request, tmp_path, redactor):
    return LocalStore(str(tmp_path / "records.db"), redactor=redactor), LocalStore(str(tmp_path / "sealed.db"))


def _postgres(request, tmp_path, redactor):
    from recursa.store.postgres import PostgresRecordStore, PostgresSealedStore

    url = request.getfixturevalue("pg_database")()
    return PostgresRecordStore(url, redactor=redactor), PostgresSealedStore(url)


def _http(request, tmp_path, redactor):
    from recursa.store.http import HttpRecordStore, HttpSealedStore

    if redactor is not None:
        pytest.skip("the server applies its own redaction; a client-side redactor does not apply")
    url, mint, _ = request.getfixturevalue("recursa_server")()
    key = mint("ingest", "optimizer", "gate", "reviewer", "admin")
    return HttpRecordStore(url, key), HttpSealedStore(url, key)


BACKENDS = {"local": _local, "postgres": _postgres, "http": _http}


@pytest.fixture(params=sorted(BACKENDS))
def make(request, tmp_path):
    opened = []

    def factory(redactor=None, sub="a"):
        d = tmp_path / sub
        d.mkdir(exist_ok=True)
        pair = BACKENDS[request.param](request, d, redactor)
        opened.extend(pair)
        return pair

    yield factory
    for s in opened:
        s.close()


@pytest.fixture
def records(make) -> RecordStore:
    return make()[0]


@pytest.fixture
def sealed(make) -> SealedStore:
    return make()[1]


def _run(**kw):
    d = dict(id=new_id("run"), agent="a", agent_version="v1", mode="eval", input={"q": "mail a@b.co"},
             output={"answer": "ok"}, status="ok", cost_usd=0.01, latency_ms=12.0, started_at=now(),
             steps=[Step(kind="model", name="claude-opus-5-5", input={"t": "x@y.io"}, output="out",
                         input_tokens=10, output_tokens=2, cost_usd=0.01, measured=True)],
             example_id="ex1", sample=0, telemetry="measured")
    d.update(kw)
    return Run(**d)


def _cand(**kw):
    d = dict(id=new_id("cand"), surface="system_prompt", baseline_version="v1", proposed={"system_prompt": "p2"},
             insight_ids=["ins1"], status="proposed", created_at=now(), dev_report={"gain": 0.1}, rationale="why")
    d.update(kw)
    return Candidate(**d)


# -- protocol conformance ----------------------------------------------------------------------
def test_backend_implements_both_protocols(make):
    r, s = make()
    assert isinstance(r, RecordStore) and isinstance(s, SealedStore)


# -- runs ----------------------------------------------------------------------------------------
def test_run_roundtrip_is_redacted(records):
    run = _run()
    records.put_run(run)
    got = records.get_run(run.id)
    assert got["input"] == {"q": "mail [EMAIL]"} and got["steps"][0]["input"] == {"t": "[EMAIL]"}
    assert got["output"] == {"answer": "ok"} and got["redaction"] == "applied"
    for k in ("agent_version", "mode", "example_id", "sample", "status", "cost_usd", "latency_ms", "telemetry"):
        assert got[k] == getattr(run, k), k
    assert records.get_run("missing") is None


def test_run_whose_redaction_fails_keeps_metrics_only(make):
    class Broken(Redactor):
        def __call__(self, obj):
            raise RedactionError("boom")

    records = make(redactor=Broken())[0]
    run = _run()
    records.put_run(run)
    got = records.get_run(run.id)
    assert got["redaction"] == "unredacted_dropped" and got["input"] is None and got["output"] is None
    assert got["cost_usd"] == run.cost_usd and "input" not in got["steps"][0]


def test_put_run_is_idempotent_first_write_wins(records):
    run = _run()
    records.put_run(run)
    records.put_run(_run(id=run.id, output={"answer": "changed"}))
    assert records.get_run(run.id)["output"] == {"answer": "ok"}


def test_list_runs_newest_first_with_mode_filter_and_cursor(records):
    t = now()
    runs = [_run(started_at=t + i, mode="eval" if i % 2 else "online") for i in range(7)]
    for r in runs:
        records.put_run(r)
    newest_first = [r.id for r in sorted(runs, key=lambda r: r.started_at, reverse=True)]
    page1 = records.list_runs(limit=3)
    page2 = records.list_runs(limit=3, before=page1[-1]["id"])
    page3 = records.list_runs(limit=3, before=page2[-1]["id"])
    assert [r["id"] for r in page1 + page2 + page3] == newest_first
    assert {r["mode"] for r in records.list_runs(mode="eval")} == {"eval"}
    assert len(records.list_runs(mode="eval")) == 3


def test_scores_and_eval_cache(records):
    run = _run()
    records.put_run(run)
    s1 = Score(run.id, "fields", "1", 1.0, "match")
    records.put_scores([s1, Score(run.id, "pii", "1", 1.0, tier="safety")])
    records.put_scores([s1])  # idempotent
    assert sorted((s.evaluator, s.tier) for s in records.scores_for(run.id)) == [("fields", "ground_truth"),
                                                                                ("pii", "safety")]
    assert records.cache_get("k") is None
    records.cache_put("k", run.id)
    records.cache_put("k", "other")  # first write wins
    assert records.cache_get("k") == run.id


def test_export_import_between_stores(make):
    src, _ = make(sub="src")
    dst, _ = make(sub="dst")
    a, b = _run(cost_usd=0.25), _run(cost_usd=0.5)
    for r in (a, b):
        src.put_run(r)
    src.put_scores([Score(a.id, "fields", "1", 1.0)])
    src.cache_put("k", a.id)
    dst.put_run(a)
    recs = src.export_records()
    assert dst.import_records(recs) == pytest.approx(0.5)
    assert dst.import_records(recs) == 0.0
    assert dst.get_run(b.id)["input"] == {"q": "mail [EMAIL]"} and dst.cache_get("k") == a.id
    assert [s.evaluator for s in dst.scores_for(a.id)] == ["fields"]


# -- feedback ------------------------------------------------------------------------------------
def test_feedback_is_idempotent_and_redacted(records):
    run = _run()
    records.put_run(run)
    f1 = records.put_feedback(run.id, "explicit", 0.0, {"fix": "email z@z.io"}, "user", "key-1")
    f2 = records.put_feedback(run.id, "explicit", 1.0, None, "user", "key-1")
    assert f1 == f2
    (fb,) = records.feedback_for(run.id)
    assert fb["value"] == 0.0 and "[EMAIL]" in fb["correction"]
    with pytest.raises(KeyError):
        records.put_feedback("missing", "explicit", 1.0, None, "user", None)


# -- versions and deployments --------------------------------------------------------------------
def test_versions_are_sequential_and_deployments_track_current(records):
    assert records.current_version() is None and records.get_version("v1") is None
    v1 = records.put_version(None, {"surfaces": {"system_prompt": "a"}}, None, ["init"])
    v2 = records.put_version(v1, {"surfaces": {"system_prompt": "b"}}, "cand1", ["alice"])
    assert (v1, v2) == ("v1", "v2")
    got = records.get_version(v2)
    assert got["parent"] == v1 and got["config"] == {"surfaces": {"system_prompt": "b"}}
    assert got["approvers"] == ["alice"] and got["candidate_id"] == "cand1"
    assert got["config_hash"] == content_hash({"surfaces": {"system_prompt": "b"}})
    assert [v["id"] for v in records.list_versions()] == [v1, v2]
    records.deploy(v1, "bootstrap", "init")
    records.deploy(v2, "promote", "alice")
    records.deploy(v1, "rollback", "alice", "regression")
    assert records.current_version() == v1
    assert [(d["version_id"], d["action"]) for d in records.deployments()] == [
        (v1, "bootstrap"), (v2, "promote"), (v1, "rollback")]


def test_concurrent_put_version_allocates_unique_ids(records):
    ids, lock = [], threading.Lock()

    def worker():
        vid = records.put_version(None, {"n": threading.get_ident()}, None, [])
        with lock:
            ids.append(vid)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(ids, key=lambda v: int(v[1:])) == [f"v{i}" for i in range(1, 9)]


# -- candidates and insights ---------------------------------------------------------------------
def test_candidate_roundtrip_status_and_pagination(records):
    t = now()
    cands = [_cand(created_at=t + i) for i in range(5)]
    for c in cands:
        records.put_candidate(c)
    records.put_candidate(_cand(id=cands[0].id, rationale="changed"))  # idempotent
    assert records.get_candidate(cands[0].id) == cands[0]
    assert records.get_candidate("missing") is None
    records.set_candidate_status(cands[1].id, "gate_rejected")
    assert [c.id for c in records.list_candidates(status="gate_rejected")] == [cands[1].id]
    page1 = records.list_candidates(limit=2)
    page2 = records.list_candidates(limit=2, after=page1[-1].id)
    page3 = records.list_candidates(limit=2, after=page2[-1].id)
    assert [c.id for c in page1 + page2 + page3] == [c.id for c in cands]


def test_insights_keep_requested_order_and_skip_unknown(records):
    records.put_insight("cyc", {"id": "i1", "statement": "one"})
    records.put_insight("cyc", {"id": "i2", "statement": "two"})
    records.put_insight("cyc", {"id": "i1", "statement": "changed"})  # idempotent
    assert [i["statement"] for i in records.get_insights(["i2", "nope", "i1"])] == ["two", "one"]


# -- approvals and audit -------------------------------------------------------------------------
def test_approvals_in_order(records):
    a1 = records.put_approval("c1", "alice", "approve", "looks good", "cli", "h1")
    a2 = records.put_approval("c1", "bob", "reject", "no", "ui", "h1")
    assert a1 != a2
    got = records.approvals_for("c1")
    assert [(a["reviewer"], a["decision"], a["packet_hash"]) for a in got] == [("alice", "approve", "h1"),
                                                                                ("bob", "reject", "h1")]
    assert records.approvals_for("other") == []


def test_audit_chain_and_pagination(records):
    assert records.verify_audit_chain()
    for i in range(5):
        records.audit("alice", f"act{i}", f"s{i}", {"i": i})
    log = records.audit_log()
    assert [e["action"] for e in log] == [f"act{i}" for i in range(5)] and log[2]["details"] == {"i": 2}
    page1 = records.audit_log(limit=2)
    page2 = records.audit_log(after=page1[-1]["seq"], limit=2)
    page3 = records.audit_log(after=page2[-1]["seq"])
    assert [e["seq"] for e in page1 + page2 + page3] == [e["seq"] for e in log]
    assert records.verify_audit_chain()


# -- checkpoints (both sides) --------------------------------------------------------------------
@pytest.mark.parametrize("side", ["records", "sealed"])
def test_cycle_checkpoints_overwrite(make, side):
    r, s = make()
    store = r if side == "records" else s
    assert store.load_cycle("c") is None
    store.save_cycle("c", {"step": 1})
    store.save_cycle("c", {"step": 2})
    assert store.load_cycle("c") == {"step": 2}


# -- sealed side ---------------------------------------------------------------------------------
def test_gate_ledger_charges_once_per_candidate(sealed):
    assert sealed.ledger_count("g1", "gate") == 0 and not sealed.ledger_has("g1", "c1", "gate")
    assert sealed.ledger_add("g1", "c1", "gate") is True
    assert sealed.ledger_add("g1", "c1", "gate") is False
    assert sealed.ledger_add("g1", "c2", "gate") is True
    assert sealed.ledger_add("g1", "c1", "confirm") is True
    assert sealed.ledger_add("g2", "c1", "gate") is True
    assert sealed.ledger_count("g1", "gate") == 2 and sealed.ledger_count("g1", "confirm") == 1
    assert sealed.ledger_has("g1", "c1", "gate") and not sealed.ledger_has("g1", "c3", "gate")


def test_packets_are_content_addressed_and_latest_wins(sealed):
    assert sealed.latest_packet("c1") is None
    h1 = sealed.put_packet("c1", {"diff": "a"})
    assert sealed.put_packet("c1", {"diff": "a"}) == h1 == content_hash({"diff": "a"})
    h2 = sealed.put_packet("c1", {"diff": "b"})
    assert sealed.latest_packet("c1") == (h2, {"diff": "b"})
