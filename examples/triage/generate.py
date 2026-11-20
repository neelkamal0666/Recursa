"""Generate labelled triage tickets for the reference agent.

    python examples/triage/generate.py pool  --n 800 --out pool.jsonl   # initial labelled pool
    python examples/triage/generate.py fresh --n 250 --out fresh.jsonl  # new set for rotation

Each ticket mixes a category cue and a priority cue. "Plain" cues are covered by the
starting prompt; "hidden" cues are not, so the starting agent gets them wrong. Tickets
are grouped by customer (2-3 tickets each) so the gate's cluster bootstrap matters.
The generator stands in for human labelling: in a real deployment the gate and
confirmation sets come from human-labelled production tasks the optimizer cannot regenerate.
"""
from __future__ import annotations

import argparse
import json
import random

PLAIN = {
    "billing": ["my invoice shows the wrong amount", "the payment failed twice", "I was charged for the wrong plan"],
    "technical": ["the app shows an error on save", "the dashboard crashes on load", "I think I found a bug in export"],
    "account": ["I forgot my password", "I can't log in from my laptop", "please change my username"],
    "shipping": ["the delivery is three days late", "my package arrived damaged", "the shipment never left the depot"],
}
HIDDEN = {
    "billing": ["my bank opened a chargeback on the renewal", "the proration on my upgrade looks off"],
    "technical": ["our webhook calls are timing out", "we keep hitting the rate limit on the import endpoint"],
    "account": ["SSO stopped accepting our identity provider", "two-factor codes never arrive for my team"],
    "shipping": ["the parcel is stuck in a customs hold", "the tracking number shows as invalid"],
}
PRIORITY = [
    ("normal", "", 0.62),
    ("urgent", "This is urgent.", 0.10),
    ("urgent", "Please help ASAP.", 0.04),
    ("urgent", "Production is down for everyone.", 0.08),
    ("urgent", "All of our users are blocked.", 0.04),
    ("low", "No rush on this one.", 0.07),
    ("low", "Whenever you have time is fine.", 0.05),
]
OPENERS = ["Hi team,", "Hello,", "Hey support,", "Good morning,", ""]
CLOSERS = ["Thanks.", "Thank you!", "Cheers,", "", "Regards."]


def ticket(rng: random.Random, idx: int, prefix: str, group: str) -> dict:
    category = rng.choice(list(PLAIN))
    hidden = rng.random() < 0.32
    cue = rng.choice((HIDDEN if hidden else PLAIN)[category])
    r, acc = rng.random(), 0.0
    for priority, sentence, p in PRIORITY:
        acc += p
        if r < acc:
            break
    contact = f" You can reach me at {group}@example.com." if rng.random() < 0.25 else ""
    parts = [rng.choice(OPENERS), cue[0].upper() + cue[1:] + ".", sentence, contact.strip(), rng.choice(CLOSERS)]
    text = " ".join(p for p in parts if p)
    return {"id": f"{prefix}{idx:05d}", "input": {"ticket": text},
            "expected": {"category": category, "priority": priority}, "group": f"{prefix}{group}",
            "tags": ["hidden_cue" if hidden else "plain_cue", f"priority:{priority}"], "source": "synthetic-labelled"}


def generate(n: int, seed: int, prefix: str) -> list[dict]:
    rng = random.Random(seed)
    out, idx, g = [], 0, 0
    while idx < n:
        group = f"cust{g:04d}"
        for _ in range(rng.choice([1, 2, 2, 3])):
            if idx >= n:
                break
            out.append(ticket(rng, idx, prefix, group))
            idx += 1
        g += 1
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["pool", "fresh"])
    p.add_argument("--n", type=int, default=800)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    prefix = "t" if a.kind == "pool" else f"f{a.seed}-"
    rows = generate(a.n, a.seed, prefix)
    with open(a.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} tickets to {a.out}")


if __name__ == "__main__":
    main()
