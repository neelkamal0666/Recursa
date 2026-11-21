"""The learner: owns the run store, the registry and the learning cycle.

Process boundaries:

* The **optimizer** (reflect -> propose -> dev eval) runs in an isolated child
  process (``recursa.worker``) that only receives train/dev data.
* The **gate** (this process) is the only code that holds the key to the sealed
  gate and confirmation sets, and it stores everything derived from them in the
  sealed store (``<home>/sealed/gate.db``).
* **Promotion** happens only through ``decide()`` with an authenticated reviewer
  whose approval is bound to the hash of the exact review packet.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from typing import Any

from .auth import Auth, AuthError, Reviewers
from .data import OpenSplits, Paths, SealedSets
from .evals import EvalResult, run_eval
from .project import Config, Project, load_project
from .stats import compare
from .types import Candidate, Run, Step, content_hash, new_id, now


class GateBudgetExhausted(RuntimeError):
    pass


class CycleAborted(RuntimeError):
    pass


def _diff(a: str, b: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), "baseline", "candidate"))


def _added_lines(a: str, b: str) -> list[str]:
    return [l[1:].rstrip("\n") for l in difflib.unified_diff(a.splitlines(True), b.splitlines(True))
            if l.startswith("+") and not l.startswith("+++")]


def _pick(res, want_pass: bool) -> int:
    """Index of the first sample whose outcome matches ``want_pass`` (falls back to 0)."""
    return next((i for i, s in enumerate(res.success) if (s >= 1.0) == want_pass), 0)


def _case(eid: str, delta: float, example, base, cand) -> dict[str, Any]:
    """A side-by-side case that shows the samples which actually differ.

    Outcomes are averaged over samples, so a case can be "broke" while the first
    samples of both versions agree. Show a passing baseline sample next to a failing
    candidate sample (or the reverse for "fixed"), plus every sample's outcome.
    """
    fixed = delta > 0
    bi, ci = _pick(base, want_pass=not fixed), _pick(cand, want_pass=fixed)
    return {
        "example_id": eid, "change": "fixed" if fixed else "broke", "delta": delta,
        "input": example.input, "expected": example.expected,
        "baseline_output": base.outputs[bi], "candidate_output": cand.outputs[ci],
        "baseline_sample": bi, "candidate_sample": ci,
        "baseline_passes": [s >= 1.0 for s in base.success],
        "candidate_passes": [s >= 1.0 for s in cand.success],
    }


class Learner:
    def __init__(self, config: Config, project: Project | None = None):
        from .store import Store  # local import keeps worker imports light

        self.cfg = config
        self.paths = Paths(config.home_path)
        self.paths.ensure()
        self.project = project or load_project(config.factory, config.root)
        self.agent = self.project.agent
        self.suite = self.project.suite
        self.store = Store(self.paths.db, auto_migrate=bool(config.store.get("auto_migrate", True)))
        self._sealed_store = None
        self.open_splits = OpenSplits(self.paths)
        self._wal_lock = threading.Lock()
        self.dropped_traces = 0
        os.makedirs(self.paths.wal, exist_ok=True)

    # -- sealed side (gate process only) ---------------------------------------------------
    @property
    def sealed_store(self):
        from .store import Store

        if self._sealed_store is None:
            self._sealed_store = Store(self.paths.sealed_db,
                                        auto_migrate=bool(self.cfg.store.get("auto_migrate", True)))
        return self._sealed_store

    @property
    def sealed(self) -> SealedSets:
        return SealedSets(self.paths)

    def close(self) -> None:
        self.store.close()
        if self._sealed_store is not None:
            self._sealed_store.close()

    # -- registry ----------------------------------------------------------------------------
    def bootstrap(self, actor: str) -> str:
        """Create v1 from the agent's current configuration (the human-written baseline)."""
        if self.store.current_version():
            return self.store.current_version()
        cfg = {**self.agent.default_config(), "suite_version": self.suite.version, "datasets": {}}
        vid = self.store.put_version(None, cfg, None, [f"bootstrap:{actor}"])
        self.store.deploy(vid, "init", actor, "initial human-written version")
        self.store.audit(actor, "init", vid, {"config_hash": content_hash(cfg)})
        return vid

    def current(self) -> tuple[str, dict[str, Any]]:
        vid = self.store.current_version()
        if not vid:
            raise RuntimeError("no agent version yet: run `recursa init`")
        return vid, self.store.get_version(vid)["config"]

    def version_config(self, vid: str) -> dict[str, Any]:
        v = self.store.get_version(vid)
        if not v:
            raise KeyError(f"unknown version {vid}")
        return v["config"]

    # -- online serving ----------------------------------------------------------------------
    async def run(self, task: dict[str, Any]) -> Run:
        """Serve one request with the current promoted version. The trace goes to a
        local write-ahead log; ``flush()`` moves it into the store (fail-open)."""
        vid, cfg = self.current()
        run = await self.agent.run(task, cfg, vid, mode="online")
        try:
            line = json.dumps(asdict(run), default=str)
            with self._wal_lock, open(os.path.join(self.paths.wal, "runs.jsonl"), "a") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            self.dropped_traces += 1
        return run

    def flush(self) -> int:
        path = os.path.join(self.paths.wal, "runs.jsonl")
        with self._wal_lock:
            if not os.path.exists(path):
                return 0
            staged = path + f".{int(time.time() * 1000)}"
            os.replace(path, staged)
        n = 0
        with open(staged) as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    d["steps"] = [Step(**s) for s in d["steps"]]
                    run = Run(**d)
                    if self.store.get_run(run.id) is None:
                        self.store.put_run(run)
                        n += 1
        os.remove(staged)
        return n

    def feedback(self, run_id: str, kind: str = "explicit", value: float | None = None, correction: Any = None,
                 author: str = "user", idempotency_key: str | None = None) -> str:
        if self.store.get_run(run_id) is None:
            self.flush()
        return self.store.put_feedback(run_id, kind, value, correction, author, idempotency_key)

    # -- evaluation on open splits -----------------------------------------------------------------
    def eval(self, vid: str | None = None, split: str = "dev", samples: int | None = None,
             use_cache: bool = True) -> EvalResult:
        if split not in ("train", "dev"):
            raise PermissionError("only train and dev can be evaluated ad hoc; gate and confirmation sets are "
                                  "read only by the promotion gate")
        vid = vid or self.current()[0]
        version, examples = self.open_splits.load(split)
        if not examples:
            raise RuntimeError(f"split {split} is empty: run `recursa data split`")
        return asyncio.run(run_eval(self.agent, vid, self.version_config(vid), examples, self.suite, self.store,
                                    f"{split}:{version}", samples=samples or self.cfg.gate.samples,
                                    use_cache=use_cache))

    # -- learning cycle --------------------------------------------------------------------------------
    def cycle(self, actor: str = "scheduler", resume: str | None = None) -> dict[str, Any]:
        state = self.store.load_cycle(resume) if resume else None
        if resume and state is None:
            raise KeyError(f"unknown cycle {resume}")
        if state is None:
            vid, _ = self.current()
            state = {"id": new_id("cyc"), "baseline": vid, "step": "started", "candidates": [], "cost_usd": 0.0,
                     "started_at": now()}
            self.store.save_cycle(state["id"], state)
            self.store.audit(actor, "cycle_started", state["id"], {"baseline": vid})
        if state["baseline"] != self.current()[0]:
            raise CycleAborted("baseline changed since the cycle started; start a new cycle")

        gate_version = self.sealed.active("gate")
        if gate_version is None:
            raise CycleAborted("no active gate set: seal one with `recursa data split` or `recursa data seal`")
        if self.sealed_store.ledger_count(gate_version, "gate") >= self.cfg.gate.budget:
            raise GateBudgetExhausted(f"gate set {gate_version} has spent its {self.cfg.gate.budget} evaluations; "
                                      "rotate it with `recursa data rotate gate <fresh.jsonl>`")

        if state["step"] == "started":
            result = self._run_worker(state)
            state["cost_usd"] += result["cost_usd"]
            for ins in result["insights"]:
                self.store.put_insight(state["id"], ins)
            top = [p for p in result["proposals"] if p["screen_pass"]][: int(self.cfg.optimizer.get("gate_top_k", 1))]
            _, base_cfg = self.current()
            for p in top:
                c = Candidate(id=new_id("cand"), surface="system_prompt", baseline_version=state["baseline"],
                              proposed=p["surfaces"], insight_ids=p["insight_ids"], status="proposed",
                              created_at=now(), dev_report={"dev": p["dev"], "dev_gain": p["dev_gain"],
                                                            "baseline_dev": result["baseline_dev"],
                                                            "dev_cost_ratio": p["dev_cost_ratio"]},
                              rationale=p["rationale"])
                self.store.put_candidate(c)
                self.store.audit("optimizer", "candidate_proposed", c.id,
                                 {"cycle": state["id"], "dev_gain": p["dev_gain"],
                                  "diff_hash": content_hash(c.proposed)})
                state["candidates"].append(c.id)
            state.update(step="proposed", baseline_dev=result["baseline_dev"], n_insights=len(result["insights"]),
                         n_proposals=len(result["proposals"]))
            self.store.save_cycle(state["id"], state)
            self._check_budget(state)

        outcomes = state.setdefault("outcomes", {})
        for cid in state["candidates"]:
            if cid in outcomes:
                continue
            outcomes[cid] = self.gate(cid, actor="gate")["status"]
            self.store.save_cycle(state["id"], state)
        state["step"] = "done"
        state["finished_at"] = now()
        self.store.save_cycle(state["id"], state)
        self.store.audit(actor, "cycle_finished", state["id"], {"candidates": len(state["candidates"])})
        return {"cycle": state["id"], "baseline": state["baseline"], "insights": state.get("n_insights", 0),
                "proposals": state.get("n_proposals", 0), "candidates": outcomes,
                "baseline_dev": state.get("baseline_dev")}

    def _check_budget(self, state: dict[str, Any]) -> None:
        limit = float(self.cfg.budget.get("max_cost_usd_per_cycle", float("inf")))
        if state["cost_usd"] > limit:
            state["step"] = "aborted_budget"
            self.store.save_cycle(state["id"], state)
            raise CycleAborted(f"cycle spent ${state['cost_usd']:.4f} > budget ${limit:.4f}")

    def _rejected_for_optimizer(self, baseline_prompt: str) -> list[dict[str, Any]]:
        """Human rejections (text only) so the optimizer stops re-proposing them.
        Gate outcomes are deliberately never included."""
        out = []
        for c in self.store.list_candidates("rejected") + self.store.list_candidates("changes_requested"):
            reasons = [a["reason"] for a in self.store.approvals_for(c.id) if a["decision"] != "approve"]
            base = self.version_config(c.baseline_version)["surfaces"]["system_prompt"]
            out.append({"added_lines": _added_lines(base, c.proposed["system_prompt"]), "reason": "; ".join(reasons)})
        return out

    def _run_worker(self, state: dict[str, Any]) -> dict[str, Any]:
        vid, cfg = self.current()
        train_v, train = self.open_splits.load("train")
        dev_v, dev = self.open_splits.load("dev")
        if not dev:
            raise CycleAborted("dev split is empty")
        worker_dir = os.path.join(self.paths.home, "worker")
        os.makedirs(worker_dir, exist_ok=True)
        worker_db = os.path.join(worker_dir, f"{state['id']}.db")
        job = {
            "protected": self.paths.protected_dirs(), "root": self.cfg.root, "factory": self.cfg.factory,
            "worker_db": worker_db, "baseline": {"version": vid, "config": cfg},
            "train": [e.to_dict() for e in train], "dev": [e.to_dict() for e in dev], "dev_version": f"dev:{dev_v}",
            "optimizer": self.cfg.optimizer, "reflect": self.cfg.reflect, "dev_samples": self.cfg.dev_samples,
            "max_cost_increase": self.cfg.gate.max_cost_increase,
            "rejected": self._rejected_for_optimizer(cfg["surfaces"]["system_prompt"]),
        }
        env = {k: v for k, v in os.environ.items() if not k.startswith("RECURSA_")}
        env["PYTHONPATH"] = os.pathsep.join([os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                             self.cfg.root, env.get("PYTHONPATH", "")])
        proc = subprocess.run([sys.executable, "-m", "recursa.worker"], input=json.dumps(job), text=True,
                              capture_output=True, env=env, cwd=worker_dir,
                              timeout=float(self.cfg.optimizer.get("timeout_s", 3600)))
        last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            result = json.loads(last)
        except json.JSONDecodeError:
            raise CycleAborted(f"optimizer worker failed (exit {proc.returncode}): {proc.stderr[-2000:]}")
        if not result.get("ok"):
            raise CycleAborted(f"optimizer worker error: {result.get('error')}")
        result["cost_usd"] = self._ingest_worker_runs(worker_db)
        return result

    def _ingest_worker_runs(self, worker_db: str) -> float:
        src = sqlite3.connect(worker_db)
        src.row_factory = sqlite3.Row
        cost = 0.0
        try:
            for r in src.execute("SELECT * FROM runs"):
                if not self.store._q("SELECT 1 FROM runs WHERE id=?", (r["id"],)):
                    self.store._x(f"INSERT INTO runs VALUES ({','.join('?' * len(r.keys()))})", tuple(r))
                    cost += r["cost_usd"] or 0.0
            for r in src.execute("SELECT * FROM scores"):
                self.store._x("INSERT OR IGNORE INTO scores VALUES (?,?,?,?,?,?)", tuple(r))
            for r in src.execute("SELECT * FROM eval_cache"):
                self.store.cache_put(r["key"], r["run_id"])
        finally:
            src.close()
        os.remove(worker_db)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(worker_db + suffix):
                os.remove(worker_db + suffix)
        return cost

    # -- gate & confirmation ---------------------------------------------------------------------------
    def _sealed_eval(self, vid: str, cfg: dict[str, Any], set_version: str) -> EvalResult:
        examples = self.sealed.load(set_version)
        return asyncio.run(run_eval(self.agent, vid, cfg, examples, self.suite, self.sealed_store, set_version,
                                    samples=self.cfg.gate.samples))

    def is_stale(self, c: Candidate) -> str | None:
        if c.baseline_version != self.current()[0]:
            return f"baseline changed ({c.baseline_version} -> {self.current()[0]})"
        if now() - c.created_at > self.cfg.expire_after_days * 86400:
            return f"older than {self.cfg.expire_after_days} days"
        return None

    def gate(self, candidate_id: str, actor: str = "gate") -> dict[str, Any]:
        """Evaluate one candidate once on the sealed gate set, then (if eligible) once on
        the confirmation set. Results stay on the sealed side; the main store only
        records the candidate's status."""
        c = self.store.get_candidate(candidate_id)
        if c is None:
            raise KeyError(candidate_id)
        if c.status != "proposed":
            raise RuntimeError(f"candidate {c.id} is {c.status}; only proposed candidates can be gated")
        stale = self.is_stale(c)
        if stale:
            self._set_status(c, "stale", actor, {"reason": stale})
            return {"status": "stale", "reason": stale}
        gcfg = self.cfg.gate
        gate_version = self.sealed.active("gate")
        if gate_version is None:
            raise CycleAborted("no active gate set")
        if self.sealed_store.ledger_has(gate_version, c.id, "gate"):
            raise RuntimeError("each candidate is evaluated on a gate-set version at most once")
        used = self.sealed_store.ledger_count(gate_version, "gate")
        if used >= gcfg.budget:
            raise GateBudgetExhausted(f"gate set {gate_version} has spent its {gcfg.budget} evaluations")
        self.sealed_store.ledger_add(gate_version, c.id, "gate")  # spend budget before looking

        base_vid = c.baseline_version
        base_cfg = self.version_config(base_vid)
        cand_cfg = {**base_cfg, "surfaces": c.proposed}
        alpha_adj = gcfg.alpha / gcfg.budget  # Bonferroni over the gate-set evaluation budget
        b = self._sealed_eval(base_vid, base_cfg, gate_version)
        k = self._sealed_eval(f"{base_vid}+{c.id}", cand_cfg, gate_version)
        gate_report = compare(b, k, gcfg, alpha_adj, gcfg.min_examples)
        gate_report.update(gate_version=gate_version, attempts_on_version=used + 1, budget=gcfg.budget)
        sealed_state = {"candidate": c.id, "gate": gate_report}
        if gate_report["verdict"] != "eligible":
            self.sealed_store.save_cycle(f"gate:{c.id}", sealed_state)
            self._set_status(c, "gate_rejected", actor, {"gate_version": gate_version})
            return {"status": "gate_rejected"}

        confirm_version = self.sealed.active("confirm")
        if confirm_version is None:
            self.sealed_store.save_cycle(f"gate:{c.id}", sealed_state)
            self.store.audit(actor, "candidate_awaiting_confirmation_data", c.id, {})
            return {"status": "awaiting_confirmation_data",
                    "next": f"seal a confirmation set (`recursa data seal confirm <file>`), then "
                            f"`recursa gate --confirm {c.id}`"}
        return self._confirm(c, gate_report, b, k, confirm_version, actor)

    def confirm_pending(self, candidate_id: str, actor: str = "gate") -> dict[str, Any]:
        """Run the confirmation step for a candidate that passed the gate earlier."""
        c = self.store.get_candidate(candidate_id)
        st = self.sealed_store.load_cycle(f"gate:{candidate_id}")
        if not c or not st or st["gate"]["verdict"] != "eligible" or c.status != "proposed":
            raise RuntimeError("candidate has no pending eligible gate result")
        confirm_version = self.sealed.active("confirm")
        if confirm_version is None:
            raise CycleAborted("no confirmation set sealed")
        base_cfg = self.version_config(c.baseline_version)
        gate_version = st["gate"]["gate_version"]
        b = self._sealed_eval(c.baseline_version, base_cfg, gate_version)
        k = self._sealed_eval(f"{c.baseline_version}+{c.id}", {**base_cfg, "surfaces": c.proposed}, gate_version)
        return self._confirm(c, st["gate"], b, k, confirm_version, actor)

    def _confirm(self, c: Candidate, gate_report: dict[str, Any], gate_base: EvalResult, gate_cand: EvalResult,
                 confirm_version: str, actor: str) -> dict[str, Any]:
        gcfg = self.cfg.gate
        self.sealed_store.ledger_add(confirm_version, c.id, "confirm")
        base_cfg = self.version_config(c.baseline_version)
        cb = self._sealed_eval(c.baseline_version, base_cfg, confirm_version)
        ck = self._sealed_eval(f"{c.baseline_version}+{c.id}", {**base_cfg, "surfaces": c.proposed}, confirm_version)
        confirm_report = compare(cb, ck, gcfg, gcfg.alpha, gcfg.confirm_min_examples)
        confirm_report["confirm_version"] = confirm_version
        # One confirmation set per promotion decision: retire it now, whatever the outcome.
        new_dev = self.sealed.retire("confirm", self.open_splits)
        self.store.audit(actor, "confirm_set_retired", confirm_version, {"new_dev_version": new_dev})
        self.sealed_store.save_cycle(f"gate:{c.id}", {"candidate": c.id, "gate": gate_report,
                                                      "confirm": confirm_report})
        if confirm_report["verdict"] != "eligible":
            self._set_status(c, "confirm_failed", actor, {"confirm_version": confirm_version})
            return {"status": "confirm_failed"}
        packet_hash = self.build_packet(c, gate_report, confirm_report, gate_base, gate_cand)
        self._set_status(c, "awaiting_review", actor, {"packet_hash": packet_hash})
        return {"status": "awaiting_review", "packet_hash": packet_hash}

    def _set_status(self, c: Candidate, status: str, actor: str, details: dict[str, Any]) -> None:
        self.store.set_candidate_status(c.id, status)
        self.store.audit(actor, f"candidate_{status}", c.id, details)

    # -- review packets ------------------------------------------------------------------------------------
    def build_packet(self, c: Candidate, gate_report: dict[str, Any], confirm_report: dict[str, Any],
                     base: EvalResult, cand: EvalResult, max_cases: int = 12) -> str:
        base_cfg = self.version_config(c.baseline_version)
        examples = {e.id: e for e in self.sealed.load(gate_report["gate_version"])}
        bb, kb = base.by_id(), cand.by_id()
        changed = []
        for eid in sorted(bb):
            delta = kb[eid].mean_success - bb[eid].mean_success
            if delta != 0:
                changed.append((delta, eid))
        changed.sort(key=lambda t: (t[0] > 0, abs(t[0])), reverse=True)  # fixes first, then breaks
        fixes = [x for x in changed if x[0] > 0][: max_cases // 2]
        breaks = [x for x in changed if x[0] < 0][: max_cases - len(fixes)]
        cases = [_case(eid, d, examples[eid], bb[eid], kb[eid]) for d, eid in fixes + breaks]
        insights = self.store.get_insights(c.insight_ids)
        packet = {
            "candidate_id": c.id, "surface": c.surface, "baseline_version": c.baseline_version,
            "created_at": c.created_at, "expires_at": c.created_at + self.cfg.expire_after_days * 86400,
            "source": c.source, "rationale": c.rationale,
            "diff": _diff(base_cfg["surfaces"]["system_prompt"], c.proposed["system_prompt"]),
            "gate_report": gate_report, "confirmation_report": confirm_report, "dev_report": c.dev_report,
            "insights": [{"id": i["id"], "statement": i["statement"], "frequency": i["frequency"],
                          "evidence_run_ids": i["evidence_run_ids"][:10],
                          "verified": i.get("verified", True)} for i in insights],
            "side_by_side": cases,
            "candidates_tried_on_gate_version": gate_report["attempts_on_version"],
            "live_tool_dependencies": [t["name"] for t in base_cfg.get("tools", [])
                                       if t["capability"] == "read_only"],
        }
        return self.sealed_store.put_packet(c.id, self.store.redactor(packet))

    def packet(self, candidate_id: str) -> tuple[str, dict[str, Any]]:
        p = self.sealed_store.latest_packet(candidate_id)
        if not p:
            raise KeyError(f"no review packet for {candidate_id}")
        return p

    def review_queue(self) -> list[Candidate]:
        out = []
        for c in self.store.list_candidates("awaiting_review"):
            reason = self.is_stale(c)
            if reason:
                self._set_status(c, "stale", "system", {"reason": reason})
            else:
                out.append(c)
        return out

    # -- human decisions ------------------------------------------------------------------------------
    def _reviewer(self, token: str | None) -> tuple[str, str]:
        body = Auth(self.paths.auth).verify_token(token)
        return body["sub"], body.get("ch", "cli")

    def decide(self, candidate_id: str, decision: str, token: str | None, packet_hash: str, reason: str = "",
               edited_prompt: str | None = None) -> dict[str, Any]:
        if decision not in ("approve", "reject", "request_changes"):
            raise ValueError("decision must be approve, reject or request_changes")
        reviewer, channel = self._reviewer(token)
        reviewers = Reviewers.load(self.cfg.reviewers_path)
        c = self.store.get_candidate(candidate_id)
        if c is None:
            raise KeyError(candidate_id)
        if not reviewers.can_review(reviewer, c.surface):
            raise AuthError(f"{reviewer} is not a reviewer for surface {c.surface}")
        if c.status != "awaiting_review":
            raise RuntimeError(f"candidate is {c.status}, not awaiting review")
        stale = self.is_stale(c)
        if stale:
            self._set_status(c, "stale", "system", {"reason": stale})
            raise RuntimeError(f"candidate is stale ({stale}); it must pass the gate again")
        current_hash, _ = self.packet(c.id)
        if packet_hash != current_hash:
            raise AuthError("approval is bound to a different review packet; re-open the current packet")
        if decision != "approve" and not reason.strip():
            raise ValueError("a reason is required to reject or request changes")
        prior = [a for a in self.store.approvals_for(c.id) if a["reviewer"] == reviewer and
                 a["packet_hash"] == packet_hash]
        if prior:
            raise RuntimeError(f"{reviewer} already decided on this packet")
        self.store.put_approval(c.id, reviewer, decision, reason, channel, packet_hash)
        self.store.audit(reviewer, f"review_{decision}", c.id, {"packet_hash": packet_hash, "channel": channel,
                                                                 "reason": reason})
        if decision == "reject":
            self._set_status(c, "rejected", reviewer, {"reason": reason})
            return {"status": "rejected"}
        if decision == "request_changes":
            self._set_status(c, "changes_requested", reviewer, {"reason": reason})
            if edited_prompt:
                nc = Candidate(id=new_id("cand"), surface=c.surface, baseline_version=c.baseline_version,
                               proposed={**c.proposed, "system_prompt": edited_prompt}, insight_ids=c.insight_ids,
                               status="proposed", created_at=now(), source="human", parent_candidate=c.id,
                               rationale=f"Edited by {reviewer}: {reason}")
                self.store.put_candidate(nc)
                self.store.audit(reviewer, "candidate_proposed", nc.id, {"parent": c.id, "source": "human"})
                return {"status": "changes_requested", "new_candidate": nc.id}
            return {"status": "changes_requested"}
        approvers = sorted({a["reviewer"] for a in self.store.approvals_for(c.id)
                            if a["decision"] == "approve" and a["packet_hash"] == packet_hash})
        need = reviewers.quorum_for(c.surface)
        if len(approvers) < need:
            return {"status": "awaiting_review", "approvals": len(approvers), "quorum": need}
        return self._promote(c, approvers, packet_hash)

    def _promote(self, c: Candidate, approvers: list[str], packet_hash: str) -> dict[str, Any]:
        base_cfg = self.version_config(c.baseline_version)
        _, packet = self.packet(c.id)
        cfg = {**base_cfg, "surfaces": c.proposed, "suite_version": self.suite.version,
               "datasets": {"gate": packet["gate_report"]["gate_version"],
                            "confirm": packet["confirmation_report"]["confirm_version"],
                            "dev": self.open_splits.manifest().get("dev", {}).get("version")}}
        vid = self.store.put_version(c.baseline_version, cfg, c.id, approvers)
        self.store.deploy(vid, "promote", ",".join(approvers), f"candidate {c.id}")
        self._set_status(c, "promoted", ",".join(approvers), {"version": vid, "packet_hash": packet_hash})
        self.store.audit(",".join(approvers), "promote", vid, {"candidate": c.id, "packet_hash": packet_hash,
                                                               "parent": c.baseline_version,
                                                               "diff": packet["diff"]})
        return {"status": "promoted", "version": vid}

    def rollback(self, target: str, token: str | None, reason: str) -> dict[str, Any]:
        reviewer, channel = self._reviewer(token)
        reviewers = Reviewers.load(self.cfg.reviewers_path)
        if reviewer not in reviewers.reviewers:
            raise AuthError(f"{reviewer} is not a listed reviewer")
        v = self.store.get_version(target)
        if v is None:
            raise KeyError(f"unknown version {target}")
        if not v["approvers"]:
            raise RuntimeError("can only roll back to a version a human approved")
        current = self.store.current_version()
        if current == target:
            raise RuntimeError(f"{target} is already current")
        self.store.deploy(target, "rollback", reviewer, reason)
        self.store.audit(reviewer, "rollback", target, {"from": current, "reason": reason, "channel": channel})
        return {"status": "rolled_back", "from": current, "to": target}
