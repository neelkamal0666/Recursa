"""Acceptance criterion 7: the optimizer process has no read access to gate or
confirmation data (or the key, the auth store, or the main store)."""
import json
import os
import textwrap

import pytest

EVIL = '''
import ctypes, glob, json, os, sqlite3, subprocess

HOME = os.path.abspath(os.path.join(os.getcwd(), ".."))  # worker cwd is <home>/worker

def attempt(name, fn, results):
    try:
        fn()
        results[name] = "ALLOWED"
    except PermissionError as e:
        results[name] = "denied"
    except Exception as e:
        results[name] = "error:" + type(e).__name__

class Evil:
    def propose(self, surfaces, insights, train, dev, rejected, n):
        r = {}
        sealed = os.path.join(HOME, "sealed")
        key = os.path.join(HOME, "keys", "gate.key")
        enc = os.path.join(sealed, "manifest.json")
        attempt("open_sealed_manifest", lambda: open(enc).read(), r)
        attempt("open_key", lambda: open(key, "rb").read(), r)
        attempt("os_open_key", lambda: os.open(key, os.O_RDONLY), r)
        attempt("listdir_sealed", lambda: os.listdir(sealed), r)
        attempt("scandir_sealed", lambda: list(os.scandir(sealed)), r)
        attempt("glob_sealed", lambda: glob.glob(os.path.join(sealed, "*")), r)
        attempt("sqlite_main_store", lambda: sqlite3.connect(os.path.join(HOME, "recursa.db")), r)
        attempt("sqlite_sealed_store", lambda: sqlite3.connect(os.path.join(sealed, "gate.db")), r)
        attempt("sqlite_uri", lambda: sqlite3.connect("file:" + os.path.join(HOME, "recursa.db") + "?mode=ro", uri=True), r)
        attempt("auth_credentials", lambda: open(os.path.join(HOME, "auth", "credentials.json")).read(), r)
        attempt("symlink_key", lambda: os.symlink(key, os.path.join(os.getcwd(), "k")), r)
        attempt("subprocess_cat", lambda: subprocess.run(["cat", key]), r)
        attempt("os_system", lambda: os.system("cat " + key), r)
        attempt("ctypes_libc", lambda: ctypes.CDLL("libc.dylib"), r)
        attempt("chmod_sealed", lambda: os.chmod(sealed, 0o777), r)
        attempt("sealed_api", lambda: __import__("recursa.data", fromlist=["x"]).SealedSets(
            __import__("recursa.data", fromlist=["x"]).Paths(HOME)).load("gate-x"), r)
        attempt("relative_path", lambda: open("../sealed/manifest.json").read(), r)
        r["env_has_recursa_vars"] = any(k.startswith("RECURSA_") for k in os.environ)
        r["dev_examples_visible"] = len(dev)
        with open(os.path.join(os.getcwd(), "evil_results.json"), "w") as f:  # allowed scratch path
            json.dump(r, f)
        return []
'''


def test_optimizer_cannot_read_sealed_data(learner, monkeypatch):
    with open(os.path.join(learner.cfg.root, "evil_opt.py"), "w") as f:
        f.write(EVIL)
    monkeypatch.setenv("RECURSA_TOKEN", "should-not-leak")
    learner.cfg.optimizer["plugin"] = "evil_opt:Evil"
    out = learner.cycle(actor="test")
    assert out["candidates"] == {}
    with open(os.path.join(learner.paths.home, "worker", "evil_results.json")) as f:
        r = json.load(f)
    assert r.pop("env_has_recursa_vars") is False
    assert r.pop("dev_examples_visible") > 0  # dev is readable, as designed
    allowed = {k: v for k, v in r.items() if v != "denied"}
    assert not allowed, f"optimizer escaped isolation: {allowed}"


def test_sealed_sets_are_ciphertext_at_rest(learner):
    gate_v = learner.sealed.active("gate")
    examples = learner.sealed.load(gate_v)
    raw = open(os.path.join(learner.paths.sealed, f"{gate_v}.enc"), "rb").read()
    for e in examples[:20]:
        assert e.id.encode() not in raw
        assert e.input["ticket"][:20].encode() not in raw
    assert oct(os.stat(learner.paths.key_file).st_mode & 0o777) == "0o600"
    for d in (learner.paths.sealed, learner.paths.keys, learner.paths.auth):
        assert oct(os.stat(d).st_mode & 0o777) == "0o700"


def test_open_splits_are_disjoint_from_sealed(learner):
    _, train = learner.open_splits.load("train")
    _, dev = learner.open_splits.load("dev")
    sealed = learner.sealed.load(learner.sealed.active("gate")) + learner.sealed.load(learner.sealed.active("confirm"))
    open_groups = {e.group for e in train + dev}
    assert not open_groups & {e.group for e in sealed}


def test_ad_hoc_eval_cannot_target_sealed_sets(learner):
    with pytest.raises(PermissionError):
        learner.eval("v1", "gate")
