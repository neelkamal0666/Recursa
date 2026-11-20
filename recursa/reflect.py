"""Reflection: turn dev-set evidence into named, evidence-backed insights.

``BatchErrorAnalysis`` clusters failed runs by (field, expected, got), then finds
phrases that are common in the cluster and rare among examples with a different
expected value for that field. Insights without evidence runs are dropped.
Only train and dev data ever reach this module.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from .evals import EvalResult, _as_dict
from .types import Example, Insight, content_hash

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def _text(ex: Example) -> str:
    return " ".join(str(v) for v in ex.input.values()) if isinstance(ex.input, dict) else str(ex.input)


def _ngrams(text: str, n_max: int = 3) -> set[str]:
    words = _WORD.findall(text.lower())
    out = set()
    for n in range(1, n_max + 1):
        for i in range(len(words) - n + 1):
            out.add(" ".join(words[i:i + n]))
    return out


class BatchErrorAnalysis:
    name = "batch_error_analysis"

    def __init__(self, min_cluster: int = 3, min_share: float = 0.3, max_leak: float = 0.05,
                 max_cues_per_cluster: int = 4, top: int = 12):
        self.min_cluster = min_cluster
        self.min_share = min_share          # a cue must cover this share of the still-unexplained examples
        self.max_leak = max_leak            # ...and appear in at most this share of other-label examples
        self.max_cues = max_cues_per_cluster
        self.top = top

    def __call__(self, examples: list[Example], result: EvalResult) -> list[Insight]:
        by_id = {e.id: e for e in examples}
        grams = {e.id: _ngrams(_text(e)) for e in examples}
        clusters: dict[tuple[str, Any, Any], list[tuple[str, str]]] = defaultdict(list)
        for r in result.examples:
            ex = by_id[r.example_id]
            for success, out, run_id in zip(r.success, r.outputs, r.run_ids):
                if success >= 1.0:
                    continue
                got = _as_dict(out) or {}
                for k, want in ex.expected.items():
                    if str(got.get(k, "")).lower() != str(want).lower():
                        clusters[(k, want, got.get(k))].append((ex.id, run_id))
        insights: list[Insight] = []
        for (fieldname, want, got), members in clusters.items():
            ex_ids = sorted({m[0] for m in members})
            if len(ex_ids) < self.min_cluster:
                continue
            runs_by_ex: dict[str, list[str]] = defaultdict(list)
            for eid, rid in members:
                runs_by_ex[eid].append(rid)
            others = [e.id for e in examples if str(e.expected.get(fieldname)).lower() != str(want).lower()]
            other_counts = Counter(g for eid in others for g in grams[eid])

            def make(cue: str | None, covered: list[str]) -> Insight:
                share = len(covered) / len(ex_ids)
                statement = (f"Examples labelled {fieldname}={want!r} were answered {fieldname}={got!r}"
                             + (f"; {len(covered)} of {len(ex_ids)} mention \"{cue}\"" if cue else ""))
                return Insight(
                    id="ins_" + content_hash([fieldname, want, got, cue, covered])[:12],
                    statement=statement, failure_mode=f"{fieldname}:{got}->{want}",
                    evidence_run_ids=sorted({r for eid in covered for r in runs_by_ex[eid]})[:50],
                    severity=1.0, frequency=len(covered),
                    data={"field": fieldname, "expected": want, "got": got, "cue": cue, "cue_coverage": share,
                          "example_ids": covered[:50]})

            # Greedy cover: a cluster can have several causes (e.g. "production is down" and
            # "all of our users" both mean urgent). Pick the best cue, remove what it explains,
            # repeat on the remainder.
            remaining = set(ex_ids)
            found = 0
            while remaining and found < self.max_cues:
                counts = Counter(g for eid in remaining for g in grams[eid])
                best = None
                for g, c in counts.items():
                    leak = other_counts[g] / max(1, len(others))
                    if (c >= self.min_cluster and c / len(remaining) >= self.min_share
                            and leak <= self.max_leak and len(g) > 2):
                        # most examples explained, then lowest leak, then the most general phrase
                        key = (c, -leak, -len(g.split()), g)
                        if best is None or key > best:
                            best = key
                if best is None:
                    break
                cue = best[3]
                covered = sorted(eid for eid in remaining if cue in grams[eid])
                insights.append(make(cue, covered))
                remaining -= set(covered)
                found += 1
            if found == 0:  # no distinguishing phrase: keep the failure mode itself as evidence
                insights.append(make(None, ex_ids))
        insights = [i for i in insights if i.evidence_run_ids]
        insights.sort(key=lambda i: (i.frequency * i.severity, i.id), reverse=True)
        return insights[: self.top]


def insight_to_dict(i: Insight) -> dict[str, Any]:
    return json.loads(json.dumps(i.__dict__, default=str))
