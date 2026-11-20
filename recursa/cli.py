"""``recursa`` command-line interface."""
from __future__ import annotations

import argparse
import difflib
import getpass
import json
import os
import sys
import textwrap
from typing import Any

from .auth import Auth, AuthError, Reviewers, load_token, save_token
from .data import OpenSplits, Paths, SealedSets, load_jsonl, split_and_seal
from .project import load_config

CONFIG_TEMPLATE = """\
project: {project}
factory: {factory}          # module:function returning recursa.Project
home: .recursa
reviewers: reviewers.yaml
expire_after_days: 7
dev_samples: 1
gate:
  min_gain: 0.02            # +2 points absolute on task success
  alpha: 0.05
  budget: 20                # candidate evaluations per gate-set version
  min_examples: 150
  confirm_min_examples: 100
  samples: 3
  bootstrap: 10000
  max_cost_increase: 0.10
  max_latency_increase: 0.15
  max_safety_regressions: 0
optimizer:
  strategy: heuristic       # or: claude
  candidates: 4
  gate_top_k: 1
  model: claude-opus-5-5
budget:
  max_cost_usd_per_cycle: 5.0
"""

REVIEWERS_TEMPLATE = """\
reviewers:
  - id: {reviewer}
    name: {reviewer}
    surfaces: [system_prompt]
quorum:
  system_prompt: 1
  default: 1
"""


def _passphrase(prompt: str) -> str:
    return os.environ.get("RECURSA_PASSPHRASE") or getpass.getpass(prompt)


def _learner(args: argparse.Namespace):
    from .learner import Learner

    return Learner(load_config(args.config))


def _print(obj: Any, as_json: bool = False) -> None:
    if as_json or not isinstance(obj, str):
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def _maybe_remove(path: str, keep: bool) -> None:
    if not keep:
        os.remove(path)
        print(f"removed {path} so the sealed labels exist only in encrypted form (use --keep-source to keep it)")


# -- commands ------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> None:
    if not os.path.exists(args.config):
        if not (args.project and args.factory):
            sys.exit("no recursa.yaml: pass --project and --factory to scaffold one")
        with open(args.config, "w") as f:
            f.write(CONFIG_TEMPLATE.format(project=args.project, factory=args.factory))
        print(f"wrote {args.config}")
    cfg = load_config(args.config)
    if not os.path.exists(cfg.reviewers_path):
        with open(cfg.reviewers_path, "w") as f:
            f.write(REVIEWERS_TEMPLATE.format(reviewer=args.reviewer or getpass.getuser()))
        print(f"wrote {cfg.reviewers_path}")
    gi = os.path.join(cfg.root, ".gitignore")
    lines = open(gi).read().splitlines() if os.path.exists(gi) else []
    if cfg.home not in lines and f"{cfg.home}/" not in lines:
        with open(gi, "a") as f:
            f.write(f"{cfg.home}/\n")
    learner = _learner(args)
    vid = learner.bootstrap(args.reviewer or getpass.getuser())
    print(f"initialised {cfg.project}: current version {vid}")


def cmd_data(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    paths = Paths(cfg.home_path)
    if args.data_cmd == "split":
        out = split_and_seal(paths, load_jsonl(args.pool), seed=args.seed)
        _print(out)
        _maybe_remove(args.pool, args.keep_source)
    elif args.data_cmd == "seal":
        paths.ensure()
        version = SealedSets(paths).seal(args.kind, load_jsonl(args.file))
        print(f"sealed {args.kind} set {version}")
        _maybe_remove(args.file, args.keep_source)
    elif args.data_cmd == "rotate":
        sealed = SealedSets(paths)
        new_dev = sealed.retire(args.kind, OpenSplits(paths))
        version = sealed.seal(args.kind, load_jsonl(args.file))
        print(f"retired the previous {args.kind} set into dev (dev {new_dev}); sealed {version}")
        _maybe_remove(args.file, args.keep_source)
    elif args.data_cmd == "status":
        from .store import Store

        sealed = SealedSets(paths)
        m = sealed.manifest()
        ss = Store(paths.sealed_db)
        for entry in m.get("gate", []):
            entry["evaluations_used"] = ss.ledger_count(entry["version"], "gate")
            entry["budget"] = cfg.gate.budget
        _print({"open": OpenSplits(paths).manifest(), "sealed": m})


def cmd_auth(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    auth = Auth(Paths(cfg.home_path).auth)
    if args.auth_cmd == "add-reviewer":
        reviewers = Reviewers.load(cfg.reviewers_path)
        if args.reviewer not in reviewers.reviewers:
            sys.exit(f"{args.reviewer} is not listed in {cfg.reviewers}; add them there first")
        p1 = _passphrase(f"new passphrase for {args.reviewer}: ")
        if not os.environ.get("RECURSA_PASSPHRASE") and p1 != getpass.getpass("repeat: "):
            sys.exit("passphrases differ")
        auth.set_passphrase(args.reviewer, p1)
        print(f"set passphrase for {args.reviewer}")
    elif args.auth_cmd == "login":
        token = auth.issue_token(args.reviewer, _passphrase(f"passphrase for {args.reviewer}: "))
        path = save_token(cfg.project, token)
        print(f"logged in as {args.reviewer} for 15 minutes (token in {path})")
    elif args.auth_cmd == "whoami":
        body = auth.verify_token(load_token(cfg.project))
        print(f"{body['sub']} (expires in {int(body['exp'] - __import__('time').time())}s)")


def cmd_eval(args: argparse.Namespace) -> None:
    learner = _learner(args)
    res = learner.eval(args.version, args.split, args.samples, use_cache=not args.no_cache)
    _print(res.summary())


def cmd_cycle(args: argparse.Namespace) -> None:
    learner = _learner(args)
    _print(learner.cycle(actor=args.actor, resume=args.resume))


def cmd_gate(args: argparse.Namespace) -> None:
    learner = _learner(args)
    out = learner.confirm_pending(args.candidate) if args.confirm else learner.gate(args.candidate, actor="gate")
    _print(out)


def render_packet(h: str, p: dict[str, Any]) -> str:
    g, c = p["gate_report"], p["confirmation_report"]
    gp, cp = g["primary"], c["primary"]
    lines = [
        f"Candidate {p['candidate_id']}  (surface: {p['surface']}, baseline {p['baseline_version']}, "
        f"source: {p['source']})",
        f"Packet hash: {h}",
        "",
        "Rationale: " + p["rationale"],
        "",
        "Diff:", p["diff"] or "(no change)",
        "Gate set {gv} (n={n}, attempt {a}/{b} on this version, alpha={al:.4f}):".format(
            gv=g["gate_version"], n=g["n_examples"], a=g["attempts_on_version"], b=g["budget"], al=g["alpha"]),
        f"  success {gp['baseline']:.3f} -> {gp['candidate']:.3f}  gain {gp['gain']:+.3f}  "
        f"95% CI [{gp['ci95'][0]:+.3f}, {gp['ci95'][1]:+.3f}]  lower bound {gp['lower_bound']:+.3f}",
        "  cost ratio {:.3f} (max {:.2f}), p95 latency ratio {:.3f} (max {:.2f}), safety regressions {}".format(
            g["guardrails"]["cost"]["ratio"], g["guardrails"]["cost"]["max_ratio"],
            g["guardrails"]["latency_p95"]["ratio"], g["guardrails"]["latency_p95"]["max_ratio"],
            g["guardrails"]["safety"]["regressions"]),
        f"Confirmation set {c['confirm_version']} (n={c['n_examples']}): gain {cp['gain']:+.3f}, "
        f"lower bound {cp['lower_bound']:+.3f}, verdict {c['verdict']}",
        "",
        "Insights:",
    ]
    for i in p["insights"]:
        lines.append(f"  - {i['statement']} (x{i['frequency']}{'' if i['verified'] else ', UNVERIFIED'}; "
                     f"evidence {', '.join(i['evidence_run_ids'][:3])})")
    lines.append("")
    lines.append("Side by side (sampled gate cases; outputs shown are the samples that differ):")
    for case in p["side_by_side"]:
        lines.append(f"  [{case['change']}] {json.dumps(case['input'])[:110]}")
        lines.append(f"      expected  {json.dumps(case['expected'])}")
        for who in ("baseline", "candidate"):
            passes = case.get(f"{who}_passes")
            tag = ""
            if passes is not None:
                marks = "".join("✓" if x else "✗" for x in passes)
                tag = f"[{sum(passes)}/{len(passes)} {marks}, sample {case[f'{who}_sample'] + 1}] "
            lines.append(f"      {who:<9} {tag}{json.dumps(case[f'{who}_output'], default=str)[:100]}")
    return "\n".join(lines)


def cmd_review(args: argparse.Namespace) -> None:
    learner = _learner(args)
    if args.review_cmd == "list":
        q = learner.review_queue()
        if not q:
            print("no candidates awaiting review")
        for c in q:
            h, _ = learner.packet(c.id)
            print(f"{c.id}  baseline {c.baseline_version}  packet {h[:12]}  {c.rationale[:80]}")
    elif args.review_cmd == "show":
        h, p = learner.packet(args.candidate)
        if args.json:
            _print({"packet_hash": h, **p}, True)
        else:
            print(render_packet(h, p))


def _resolve_hash(learner, candidate: str, prefix: str) -> str:
    h, _ = learner.packet(candidate)
    if not prefix or not h.startswith(prefix) or len(prefix) < 12:
        raise AuthError("pass --packet with at least 12 characters of the packet hash you reviewed "
                        "(see `recursa review show`)")
    return h


def cmd_decide(args: argparse.Namespace) -> None:
    learner = _learner(args)
    token = load_token(learner.cfg.project)
    h = _resolve_hash(learner, args.candidate, args.packet)
    edited = open(args.prompt_file).read() if getattr(args, "prompt_file", None) else None
    decision = {"approve": "approve", "promote": "approve", "reject": "reject",
                "request-changes": "request_changes"}[args.cmd]
    _print(learner.decide(args.candidate, decision, token, h, reason=args.reason or "", edited_prompt=edited))


def cmd_rollback(args: argparse.Namespace) -> None:
    learner = _learner(args)
    _print(learner.rollback(args.version, load_token(learner.cfg.project), args.reason))


def cmd_versions(args: argparse.Namespace) -> None:
    learner = _learner(args)
    current = learner.store.current_version()
    for v in learner.store.list_versions():
        mark = "*" if v["id"] == current else " "
        print(f"{mark} {v['id']}  parent={v['parent']}  candidate={v['candidate_id']}  "
              f"approvers={','.join(v['approvers'])}  hash={v['config_hash'][:12]}")


def cmd_diff(args: argparse.Namespace) -> None:
    learner = _learner(args)
    a, b = learner.version_config(args.a), learner.version_config(args.b)
    for k in sorted(set(a) | set(b)):
        if a.get(k) == b.get(k):
            continue
        if k == "surfaces":
            for s in sorted(set(a[k]) | set(b[k])):
                print("".join(difflib.unified_diff(str(a[k].get(s, "")).splitlines(True),
                                                   str(b[k].get(s, "")).splitlines(True), f"{args.a}:{s}",
                                                   f"{args.b}:{s}")))
        else:
            print(f"{k}: {json.dumps(a.get(k))} -> {json.dumps(b.get(k))}")
    v = learner.store.get_version(args.b)
    if v and v["candidate_id"]:
        c = learner.store.get_candidate(v["candidate_id"])
        print(f"why: {c.rationale}\napproved by: {', '.join(v['approvers'])}")


def cmd_audit(args: argparse.Namespace) -> None:
    learner = _learner(args)
    if args.verify:
        ok = learner.store.verify_audit_chain()
        print("audit chain intact" if ok else "AUDIT CHAIN BROKEN")
        sys.exit(0 if ok else 1)
    for r in learner.store.audit_log()[-args.limit:]:
        print(f"{r['seq']:>5}  {r['actor']:<16} {r['action']:<24} {r['subject']}  {json.dumps(r['details'])[:100]}")


def cmd_ui(args: argparse.Namespace) -> None:
    from .ui import serve

    serve(_learner(args), host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recursa", description="Eval-gated, human-approved agent improvement.")
    p.add_argument("--config", default="recursa.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="scaffold config and create the initial agent version")
    s.add_argument("--project"); s.add_argument("--factory"); s.add_argument("--reviewer")
    s.set_defaults(fn=cmd_init)

    d = sub.add_parser("data", help="split, seal and rotate evaluation sets")
    dsub = d.add_subparsers(dest="data_cmd", required=True)
    x = dsub.add_parser("split"); x.add_argument("pool"); x.add_argument("--seed", default="recursa")
    x.add_argument("--keep-source", action="store_true")
    x = dsub.add_parser("seal"); x.add_argument("kind", choices=["gate", "confirm"]); x.add_argument("file")
    x.add_argument("--keep-source", action="store_true")
    x = dsub.add_parser("rotate"); x.add_argument("kind", choices=["gate", "confirm"]); x.add_argument("file")
    x.add_argument("--keep-source", action="store_true")
    dsub.add_parser("status")
    d.set_defaults(fn=cmd_data)

    a = sub.add_parser("auth", help="reviewer accounts and login")
    asub = a.add_subparsers(dest="auth_cmd", required=True)
    x = asub.add_parser("add-reviewer"); x.add_argument("reviewer")
    x = asub.add_parser("login"); x.add_argument("reviewer")
    asub.add_parser("whoami")
    a.set_defaults(fn=cmd_auth)

    s = sub.add_parser("eval", help="evaluate a version on train or dev")
    s.add_argument("--version"); s.add_argument("--split", default="dev", choices=["train", "dev"])
    s.add_argument("--samples", type=int); s.add_argument("--no-cache", action="store_true")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("cycle", help="run one learning cycle (proposes candidates for review)")
    s.add_argument("--resume"); s.add_argument("--actor", default="cli")
    s.set_defaults(fn=cmd_cycle)

    s = sub.add_parser("gate", help="gate one proposed candidate (e.g. a reviewer's edit)")
    s.add_argument("candidate"); s.add_argument("--confirm", action="store_true",
                                                help="run the pending confirmation step")
    s.set_defaults(fn=cmd_gate)

    r = sub.add_parser("review", help="list or show candidates awaiting approval")
    rsub = r.add_subparsers(dest="review_cmd", required=True)
    rsub.add_parser("list")
    x = rsub.add_parser("show"); x.add_argument("candidate"); x.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_review)

    for name in ("approve", "promote", "reject", "request-changes"):
        s = sub.add_parser(name, help=f"{name} a candidate (requires `recursa auth login`)")
        s.add_argument("candidate")
        s.add_argument("--packet", required=True, help="hash (prefix >= 12 chars) of the packet you reviewed")
        s.add_argument("--reason", default="")
        if name == "request-changes":
            s.add_argument("--prompt-file", help="your edited system prompt; becomes a new candidate to gate")
        s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("rollback", help="restore a previous human-approved version")
    s.add_argument("version"); s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_rollback)

    s = sub.add_parser("versions", help="list agent versions"); s.set_defaults(fn=cmd_versions)
    s = sub.add_parser("diff", help="show what changed between two versions and why")
    s.add_argument("a"); s.add_argument("b"); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("audit", help="show or verify the audit log")
    s.add_argument("--verify", action="store_true"); s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("ui", help="local review dashboard")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (AuthError, PermissionError) as e:
        sys.exit(f"denied: {e}")
    except (RuntimeError, KeyError, ValueError) as e:
        sys.exit(f"error: {textwrap.shorten(str(e), 400)}")


if __name__ == "__main__":
    main()
