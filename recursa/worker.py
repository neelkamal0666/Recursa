"""Optimizer worker: runs reflect -> propose -> dev-evaluate in an isolated process.

Invoked by the learner as ``python -m recursa.worker``. The job arrives as JSON on
stdin and contains only train/dev data. Isolation is installed before any user
code (agent, optimizer plugin) is imported. The result is printed as JSON.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any


def _run(job: dict[str, Any]) -> dict[str, Any]:
    from . import optimize
    from .evals import run_eval
    from .project import load_project
    from .reflect import BatchErrorAnalysis, insight_to_dict
    from .store import Store
    from .types import Example

    project = load_project(job["factory"], job["root"])
    store = Store(job["worker_db"])
    train = [Example.from_dict(d) for d in job["train"]]
    dev = [Example.from_dict(d) for d in job["dev"]]
    base = job["baseline"]
    samples = int(job.get("dev_samples", 1))

    async def dev_eval(version: str, config: dict[str, Any]):
        return await run_eval(project.agent, version, config, dev, project.suite, store, job["dev_version"],
                              samples=samples)

    base_res = asyncio.run(dev_eval(base["version"], base["config"]))
    insights = BatchErrorAnalysis(**job.get("reflect", {}))(dev, base_res)

    opt_cfg = job.get("optimizer", {})
    if opt_cfg.get("plugin"):
        mod, _, attr = opt_cfg["plugin"].partition(":")
        optimizer = getattr(importlib.import_module(mod), attr)()
    else:
        optimizer = optimize.build(opt_cfg)
    proposals = optimizer.propose(base["config"]["surfaces"], insights, train, dev, job.get("rejected", []),
                                  int(opt_cfg.get("candidates", 4)))

    base_sum = base_res.summary()
    max_cost = 1 + float(job.get("max_cost_increase", 0.10))
    ranked = []
    for i, p in enumerate(proposals):
        cfg = {**base["config"], "surfaces": p.surfaces}
        res = asyncio.run(dev_eval(f"{base['version']}+dev{i}", cfg))
        s = res.summary()
        cost_ratio = s["mean_cost_usd"] / base_sum["mean_cost_usd"] if base_sum["mean_cost_usd"] else 1.0
        ranked.append({
            "surfaces": p.surfaces, "insight_ids": p.insight_ids, "rationale": p.rationale, "meta": p.meta,
            "dev": s, "dev_gain": s["success_rate"] - base_sum["success_rate"], "dev_cost_ratio": cost_ratio,
            "screen_pass": s["success_rate"] > base_sum["success_rate"] and cost_ratio <= max_cost
                           and s["safety_failures"] <= base_sum["safety_failures"],
        })
    # Best dev gain first; among near-ties prefer the cheaper (smaller) change.
    ranked.sort(key=lambda r: (not r["screen_pass"], -round(r["dev_gain"], 3), r["dev_cost_ratio"]))
    store.close()
    return {"baseline_dev": base_sum, "insights": [insight_to_dict(i) for i in insights], "proposals": ranked}


def main() -> None:
    job = json.loads(sys.stdin.read())
    from .isolation import install

    install(job["protected"])  # before any user code is imported
    try:
        out = {"ok": True, **_run(job)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sys.stdout.write("\n" + json.dumps(out, default=str) + "\n")


if __name__ == "__main__":
    main()
