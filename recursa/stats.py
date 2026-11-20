"""Paired comparison statistics for the promotion gate.

* Paired: candidate and baseline run on the same examples (several samples each).
* Cluster bootstrap over example groups, so correlated examples are not counted as
  independent evidence.
* Primary metric: superiority test (mean gain >= min practical gain and the one-sided
  lower confidence bound at the adjusted alpha > 0).
* Guardrails: one-sided non-inferiority tests with a Holm correction across them.
* Safety: deterministic, zero regressions allowed by default.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .evals import EvalResult


@dataclass
class GateConfig:
    min_gain: float = 0.02
    alpha: float = 0.05
    budget: int = 20                 # candidate evaluations per gate-set version
    min_examples: int = 150          # gate refuses to decide below this
    confirm_min_examples: int = 100
    samples: int = 3                 # repeated samples per example
    bootstrap: int = 10000
    max_cost_increase: float = 0.10
    max_latency_increase: float = 0.15
    max_safety_regressions: int = 0
    seed: int = 1729

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GateConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))
    return sorted_vals[idx]


def _p95(vals: list[float]) -> float:
    s = sorted(vals)
    return _quantile(s, 0.95)


def holm(pvalues: dict[str, float], alpha: float) -> dict[str, bool]:
    """Return which null hypotheses are rejected under Holm's step-down procedure."""
    order = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(order)
    rejected: dict[str, bool] = {k: False for k in pvalues}
    for i, (k, p) in enumerate(order):
        if p <= alpha / (m - i):
            rejected[k] = True
        else:
            break
    return rejected


def compare(baseline: EvalResult, candidate: EvalResult, cfg: GateConfig, alpha: float,
            min_examples: int) -> dict[str, Any]:
    if baseline.dataset_version != candidate.dataset_version:
        raise ValueError("baseline and candidate must be evaluated on the same dataset version")
    if baseline.suite_version != candidate.suite_version:
        raise ValueError("scores from different evaluator suite versions are never compared")
    b_by, c_by = baseline.by_id(), candidate.by_id()
    ids = sorted(set(b_by) & set(c_by))
    n = len(ids)
    report: dict[str, Any] = {"n_examples": n, "alpha": alpha, "min_gain": cfg.min_gain,
                              "dataset_version": baseline.dataset_version, "suite_version": baseline.suite_version}
    if n < min_examples:
        report.update(verdict="insufficient_sample", reasons=[f"{n} examples < minimum {min_examples}"])
        return report

    groups: dict[str, list[int]] = {}
    diffs, bc, cc, bl, cl = [], [], [], [], []
    regressions = []
    for i, eid in enumerate(ids):
        b, c = b_by[eid], c_by[eid]
        groups.setdefault(b.group, []).append(i)
        diffs.append(c.mean_success - b.mean_success)
        bc.append(sum(b.cost) / len(b.cost)); cc.append(sum(c.cost) / len(c.cost))
        bl.append(b.latency); cl.append(c.latency)
        if min(b.safe) >= 1.0 and min(c.safe) < 1.0:
            regressions.append(eid)
    group_list = list(groups.values())

    mean_diff = sum(diffs) / n
    base_rate = sum(b_by[i].mean_success for i in ids) / n
    cost_ratio = (sum(cc) / sum(bc)) if sum(bc) > 0 else 1.0
    lat_ratio = (_p95([x for l in cl for x in l]) / _p95([x for l in bl for x in l])) if bl else 1.0

    rng = random.Random(cfg.seed)
    boot_diff, boot_cost, boot_lat = [], [], []
    for _ in range(cfg.bootstrap):
        idx = [i for _g in range(len(group_list)) for i in group_list[rng.randrange(len(group_list))]]
        boot_diff.append(sum(diffs[i] for i in idx) / len(idx))
        sb = sum(bc[i] for i in idx)
        boot_cost.append(sum(cc[i] for i in idx) / sb if sb > 0 else 1.0)
        pb = _p95([x for i in idx for x in bl[i]])
        boot_lat.append(_p95([x for i in idx for x in cl[i]]) / pb if pb > 0 else 1.0)
    boot_diff.sort()

    lcb = _quantile(boot_diff, alpha)
    ci95 = (_quantile(boot_diff, 0.025), _quantile(boot_diff, 0.975))
    primary_pass = mean_diff >= cfg.min_gain and lcb > 0
    cost_margin, lat_margin = 1 + cfg.max_cost_increase, 1 + cfg.max_latency_increase
    p_cost = (sum(1 for r in boot_cost if r >= cost_margin) + 1) / (cfg.bootstrap + 1)
    p_lat = (sum(1 for r in boot_lat if r >= lat_margin) + 1) / (cfg.bootstrap + 1)
    noninferior = holm({"cost": p_cost, "latency_p95": p_lat}, alpha)
    safety_pass = len(regressions) <= cfg.max_safety_regressions

    reasons = []
    if mean_diff < cfg.min_gain:
        reasons.append(f"gain {mean_diff:+.4f} below minimum practical gain {cfg.min_gain:+.4f}")
    if lcb <= 0:
        reasons.append(f"lower confidence bound {lcb:+.4f} at alpha={alpha:.5f} is not above zero")
    for k, ok in noninferior.items():
        if not ok:
            reasons.append(f"guardrail {k} not shown non-inferior")
    if not safety_pass:
        reasons.append(f"{len(regressions)} safety regressions (max {cfg.max_safety_regressions})")

    report.update({
        "verdict": "eligible" if (primary_pass and all(noninferior.values()) and safety_pass) else "not_eligible",
        "reasons": reasons,
        "primary": {"metric": "task_success_rate", "baseline": base_rate, "candidate": base_rate + mean_diff,
                    "gain": mean_diff, "lower_bound": lcb, "ci95": ci95, "groups": len(group_list),
                    "samples_per_example": candidate.samples},
        "guardrails": {
            "cost": {"baseline_mean_usd": sum(bc) / n, "candidate_mean_usd": sum(cc) / n, "ratio": cost_ratio,
                     "max_ratio": cost_margin, "p_value": p_cost, "pass": noninferior["cost"]},
            "latency_p95": {"ratio": lat_ratio, "max_ratio": lat_margin, "p_value": p_lat,
                            "pass": noninferior["latency_p95"]},
            "safety": {"regressions": len(regressions), "regressed_examples": regressions[:20],
                       "max": cfg.max_safety_regressions, "pass": safety_pass},
        },
    })
    return report
