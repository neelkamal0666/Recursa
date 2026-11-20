"""Optimizer: turns insights into system-prompt candidates (the only v1 surface).

Strategies:

* ``RuleRewriter`` (default, deterministic, offline): appends one compact rule per
  insight that has a distinguishing cue, producing nested candidates (top 1, top 2,
  ...) so dev can pick the smallest change that helps.
* ``ClaudeRewriter``: asks Claude to rewrite the prompt from the insights and
  failure samples.

The optimizer only ever sees train and dev data, plus the text of past human
rejections. It never sees gate or confirmation results.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .types import Example, Insight

RULES_HEADER = "Learned rules (apply these first):"
RULE_RX = re.compile(r'^- If the input mentions "(?P<cue>[^"]+)", set (?P<field>\w+) to "(?P<value>[^"]+)"\.$')


@dataclass
class Proposal:
    surfaces: dict[str, Any]
    insight_ids: list[str]
    rationale: str
    meta: dict[str, Any] = field(default_factory=dict)


def format_rule(cue: str, fieldname: str, value: Any) -> str:
    return f'- If the input mentions "{cue}", set {fieldname} to "{value}".'


def parse_rules(prompt: str) -> list[tuple[str, str, str]]:
    return [(m["cue"], m["field"], m["value"]) for line in prompt.splitlines() if (m := RULE_RX.match(line.strip()))]


def add_rules(prompt: str, rules: list[str]) -> str:
    existing = set(prompt.splitlines())
    new = [r for r in rules if r not in existing]
    if not new:
        return prompt
    if RULES_HEADER in prompt:
        return prompt.rstrip("\n") + "\n" + "\n".join(new) + "\n"
    return prompt.rstrip("\n") + "\n\n" + RULES_HEADER + "\n" + "\n".join(new) + "\n"


class RuleRewriter:
    name = "rule_rewriter"

    def propose(self, surfaces: dict[str, Any], insights: list[Insight], train: list[Example],
                dev: list[Example], rejected: list[dict[str, Any]], n: int) -> list[Proposal]:
        prompt = surfaces["system_prompt"]
        rejected_rules = {line for r in rejected for line in r.get("added_lines", [])}
        have = {(c.lower(), f) for c, f, _ in parse_rules(prompt)}
        usable: list[tuple[Insight, str]] = []
        seen_cues: set[tuple[str, str]] = set()
        for ins in insights:
            cue, fieldname, want = ins.data.get("cue"), ins.data.get("field"), ins.data.get("expected")
            if not cue or (cue.lower(), fieldname) in have or (cue.lower(), fieldname) in seen_cues:
                continue
            rule = format_rule(cue, fieldname, want)
            if rule in rejected_rules:
                continue
            seen_cues.add((cue.lower(), fieldname))
            usable.append((ins, rule))
        proposals = []
        for k in range(1, min(n, len(usable)) + 1):
            chosen = usable[:k]
            proposals.append(Proposal(
                surfaces={**surfaces, "system_prompt": add_rules(prompt, [r for _, r in chosen])},
                insight_ids=[i.id for i, _ in chosen],
                rationale="Adds rules for the most frequent failure modes on dev: "
                          + "; ".join(i.statement for i, _ in chosen),
                meta={"added_lines": [r for _, r in chosen]},
            ))
        return proposals


class ClaudeRewriter:
    name = "claude_rewriter"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high"):
        self.model = model
        self.effort = effort

    def propose(self, surfaces: dict[str, Any], insights: list[Insight], train: list[Example],
                dev: list[Example], rejected: list[dict[str, Any]], n: int) -> list[Proposal]:
        from .llm import complete

        by_id = {e.id: e for e in dev}
        failures = []
        for ins in insights[:8]:
            for eid in ins.data.get("example_ids", [])[:3]:
                if eid in by_id:
                    failures.append({"input": by_id[eid].input, "expected": by_id[eid].expected,
                                     "failure_mode": ins.failure_mode})
        brief = {
            "current_system_prompt": surfaces["system_prompt"],
            "failure_modes": [{"statement": i.statement, "frequency": i.frequency} for i in insights],
            "failure_samples": failures[:24],
            "rejected_by_reviewers": [{"diff": r.get("added_lines"), "reason": r.get("reason")} for r in rejected],
        }
        proposals = []
        for i in range(n):
            text, _usage = complete(
                system=("You improve system prompts for an LLM agent. Make the smallest change that fixes the "
                        "listed failure modes without breaking other behaviour, keep the output format "
                        "instructions intact, and keep the prompt about the same length. Do not repeat "
                        "changes reviewers rejected. Reply with only the full new system prompt."),
                prompt=json.dumps(brief, indent=2) + f"\n\nVariant {i + 1} of {n}: vary which fixes you include.",
                model=self.model, effort=self.effort,
            )
            new_prompt = text.strip()
            if new_prompt and new_prompt != surfaces["system_prompt"]:
                proposals.append(Proposal(surfaces={**surfaces, "system_prompt": new_prompt},
                                          insight_ids=[x.id for x in insights], rationale=f"Claude rewrite {i + 1}",
                                          meta={"model": self.model}))
        return proposals


def build(cfg: dict[str, Any]):
    strategy = cfg.get("strategy", "heuristic")
    if strategy in ("heuristic", "rules"):
        return RuleRewriter()
    if strategy == "claude":
        return ClaudeRewriter(model=cfg.get("model", "claude-opus-5-5"), effort=cfg.get("effort", "high"))
    raise ValueError(f"unknown optimizer strategy {strategy!r}")
