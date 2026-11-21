"""v1 acceptance criteria, one test (or more) per criterion."""
import time

import pytest

from recursa.auth import Auth, AuthError

from conftest import PASS, make_project


@pytest.fixture(params=["local", "server"])
def learner(request, tmp_path):
    """Every acceptance criterion holds with the embedded store and with Recursa Server."""
    if request.param == "server":
        yield request.getfixturevalue("server_learner")
        return
    lr = make_project(tmp_path)
    yield lr
    lr.close()


def _cycle_to_review(learner):
    out = learner.cycle(actor="test")
    (cid, status), = out["candidates"].items()
    assert status == "awaiting_review", out
    return cid


# 1. Two agent versions can be benchmarked reproducibly.
def test_benchmark_is_reproducible(learner):
    a = learner.eval("v1", "dev", samples=3, use_cache=False)
    b = learner.eval("v1", "dev", samples=3, use_cache=False)
    assert [e.success for e in a.examples] == [e.success for e in b.examples]
    assert a.summary()["success_rate"] == b.summary()["success_rate"]
    c = learner.eval("v1", "dev", samples=3, use_cache=True)  # cached path returns the same scores
    assert [e.success for e in a.examples] == [e.success for e in c.examples]


# 2 + 3 + 4. One cycle yields a candidate that passes gate and confirmation, gains at least
# the minimum with a positive lower bound, and no guardrail regresses.
def test_cycle_yields_eligible_candidate(learner):
    cid = _cycle_to_review(learner)
    _, packet = learner.packet(cid)
    for report in (packet["gate_report"], packet["confirmation_report"]):
        assert report["verdict"] == "eligible"
        assert report["primary"]["gain"] >= learner.cfg.gate.min_gain
        assert report["primary"]["lower_bound"] > 0
        g = report["guardrails"]
        assert g["safety"]["regressions"] == 0
        assert g["cost"]["pass"] and g["latency_p95"]["pass"]
    assert packet["gate_report"]["alpha"] == pytest.approx(0.05 / 20)
    assert packet["gate_report"]["n_examples"] >= 150
    assert packet["candidates_tried_on_gate_version"] == 1


# 5. Nothing is promoted without an authenticated approval bound to the exact candidate.
def test_promotion_requires_authenticated_bound_approval(learner, token):
    cid = _cycle_to_review(learner)
    h, _ = learner.packet(cid)
    auth = Auth(learner.paths.auth)
    with pytest.raises(AuthError):
        learner.decide(cid, "approve", None, h)
    with pytest.raises(AuthError):
        learner.decide(cid, "approve", token[:-4] + "0000", h)  # forged signature
    with pytest.raises(AuthError):
        learner.decide(cid, "approve", auth.issue_token("mallory", PASS), h)  # not a reviewer
    with pytest.raises(AuthError):
        learner.decide(cid, "approve", auth.issue_token("bob", PASS), h)  # wrong surface
    with pytest.raises(AuthError):
        learner.decide(cid, "approve", auth.issue_token("alice", PASS, ttl_s=-1), h)  # expired
    with pytest.raises(AuthError):
        learner.decide(cid, "approve", token, "0" * 64)  # approval bound to another packet
    with pytest.raises(AuthError):
        auth.issue_token("alice", "wrong passphrase!")
    assert learner.store.current_version() == "v1"
    assert learner.store.get_candidate(cid).status == "awaiting_review"

    out = learner.decide(cid, "approve", token, h)
    assert out == {"status": "promoted", "version": "v2"}
    assert learner.store.current_version() == "v2"
    approvals = learner.store.approvals_for(cid)
    assert [(a["reviewer"], a["decision"], a["packet_hash"]) for a in approvals] == [("alice", "approve", h)]
    actions = [(r["action"], r["subject"]) for r in learner.store.audit_log()]
    assert ("review_approve", cid) in actions and ("promote", "v2") in actions
    assert learner.store.verify_audit_chain()
    with pytest.raises(RuntimeError):
        learner.decide(cid, "approve", token, h)  # cannot be reused


def test_no_auto_promotion_path(learner):
    _cycle_to_review(learner)
    assert learner.store.current_version() == "v1"  # the cycle itself never promotes


# 6. Rollback restores the previous version in one command, verified by re-running the eval.
def test_rollback_restores_previous_version(learner, token):
    cid = _cycle_to_review(learner)
    h, _ = learner.packet(cid)
    learner.decide(cid, "approve", token, h)
    assert learner.version_config("v2")["datasets"]["gate"].startswith("gate-")
    v1_score = learner.eval("v1", "dev", use_cache=False).summary()
    current_before = learner.eval(None, "dev", use_cache=False).summary()
    assert current_before["version"] == "v2" and current_before["success_rate"] > v1_score["success_rate"]

    learner.rollback("v1", token, "test")  # one command

    assert learner.current()[0] == "v1"
    assert learner.current()[1] == learner.version_config("v1")  # every pinned surface restored together
    after = learner.eval(None, "dev", use_cache=False).summary()
    assert after["version"] == "v1"
    assert after["success_rate"] == v1_score["success_rate"]
    assert after["mean_cost_usd"] == v1_score["mean_cost_usd"]
    assert ("rollback", "v1") in [(r["action"], r["subject"]) for r in learner.store.audit_log()]
    with pytest.raises(AuthError):
        learner.rollback("v2", None, "no auth")


# 7 is in test_isolation.py (optimizer has no read access to sealed data).


def test_stale_candidate_cannot_be_approved(learner, token, monkeypatch):
    import recursa.learner

    cid = _cycle_to_review(learner)
    h, _ = learner.packet(cid)
    real_now = recursa.learner.now
    monkeypatch.setattr(recursa.learner, "now", lambda: real_now() + 8 * 86400)  # eight days later
    with pytest.raises(RuntimeError, match="stale"):
        learner.decide(cid, "approve", token, h)
    assert learner.store.get_candidate(cid).status == "stale"


def test_reject_and_request_changes(learner, token):
    cid = _cycle_to_review(learner)
    h, p = learner.packet(cid)
    with pytest.raises(ValueError):
        learner.decide(cid, "reject", token, h, reason="")
    out = learner.decide(cid, "request_changes", token, h, reason="drop the last rule",
                         edited_prompt=learner.version_config("v1")["surfaces"]["system_prompt"] + "\nBe concise.\n")
    assert out["status"] == "changes_requested"
    new = learner.store.get_candidate(out["new_candidate"])
    assert new.source == "human" and new.status == "proposed"
    # a human edit must pass the gate again (and fails it: no real improvement)
    assert learner.gate(new.id)["status"] == "gate_rejected"
    rej = learner._rejected_for_optimizer("")
    assert rej and rej[0]["reason"] == "drop the last rule"


def test_packet_side_by_side_shows_the_differing_samples(learner):
    from recursa.evals import _as_dict

    cid = _cycle_to_review(learner)
    _, packet = learner.packet(cid)
    cases = packet["side_by_side"]
    assert cases and {c["change"] for c in cases} <= {"fixed", "broke"}

    def correct(case, who):
        return _as_dict(case[f"{who}_output"]) is not None and all(
            str(_as_dict(case[f"{who}_output"]).get(k, "")).lower() == str(v).lower()
            for k, v in case["expected"].items())

    for case in cases:
        fixed = case["change"] == "fixed"
        assert correct(case, "baseline") is not fixed, case
        assert correct(case, "candidate") is fixed, case
        assert case["baseline_output"] != case["candidate_output"], case
        assert case["baseline_passes"][case["baseline_sample"]] is not fixed
        assert case["candidate_passes"][case["candidate_sample"]] is fixed
