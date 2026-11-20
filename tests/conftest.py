import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "triage")
sys.path.insert(0, EXAMPLE)

CONFIG = """\
project: triage-test
factory: triage_agent:build
home: .recursa
reviewers: reviewers.yaml
gate:
  min_gain: 0.02
  alpha: 0.05
  budget: {budget}
  min_examples: 150
  confirm_min_examples: 100
  samples: 3
  bootstrap: 2000
optimizer:
  strategy: heuristic
  candidates: 4
  gate_top_k: 1
budget:
  max_cost_usd_per_cycle: 5.0
"""

REVIEWERS = """\
reviewers:
  - id: alice
    surfaces: [system_prompt]
  - id: bob
    surfaces: [tools]
quorum:
  default: 1
"""

PASS = "correct-horse-battery"


def make_project(tmp_path, budget=20):
    import generate  # examples/triage/generate.py

    from recursa.data import Paths, split_and_seal
    from recursa.learner import Learner
    from recursa.project import load_config
    from recursa.types import Example

    d = tmp_path / "proj"
    d.mkdir()
    shutil.copy(os.path.join(EXAMPLE, "triage_agent.py"), d / "triage_agent.py")
    (d / "recursa.yaml").write_text(CONFIG.format(budget=budget))
    (d / "reviewers.yaml").write_text(REVIEWERS)
    cfg = load_config(str(d / "recursa.yaml"))
    split_and_seal(Paths(cfg.home_path), [Example.from_dict(r) for r in generate.generate(800, 7, "t")])
    learner = Learner(cfg)
    learner.bootstrap("tester")
    from recursa.auth import Auth

    auth = Auth(learner.paths.auth)
    auth.set_passphrase("alice", PASS)
    auth.set_passphrase("bob", PASS)
    auth.set_passphrase("mallory", PASS)  # has credentials but is not in reviewers.yaml
    return learner


@pytest.fixture
def learner(tmp_path):
    lr = make_project(tmp_path)
    yield lr
    lr.close()


@pytest.fixture
def token(learner):
    from recursa.auth import Auth

    return Auth(learner.paths.auth).issue_token("alice", PASS)
