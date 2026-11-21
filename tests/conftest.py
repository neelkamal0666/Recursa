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
  allow_self_reported_telemetry: {self_reported}
optimizer:
  strategy: heuristic
  candidates: 4
  gate_top_k: 1
budget:
  max_cost_usd_per_cycle: 5.0
{store}"""

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


def make_project(tmp_path, budget=20, self_reported=True, store=None):
    """A triage project with sealed sets, bootstrapped to v1. ``store`` is an optional
    dict for the config's ``store:`` section (e.g. url and key_env for Recursa Server)."""
    import generate  # examples/triage/generate.py

    from recursa.data import Paths, split_and_seal
    from recursa.learner import Learner
    from recursa.project import load_config
    from recursa.types import Example

    d = tmp_path / "proj"
    d.mkdir()
    shutil.copy(os.path.join(EXAMPLE, "triage_agent.py"), d / "triage_agent.py")
    store_yaml = "".join(f"  {k}: {v}\n" for k, v in (store or {}).items())
    (d / "recursa.yaml").write_text(CONFIG.format(budget=budget, self_reported=str(self_reported).lower(),
                                                  store=f"store:\n{store_yaml}" if store else ""))
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


# -- Postgres for backend tests ------------------------------------------------------------------
# Uses RECURSA_TEST_PG_URL (a superuser URL, as in CI) if set; otherwise starts a throwaway
# cluster when initdb is on PATH; otherwise Postgres-backed tests are skipped.
def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_admin_url(tmp_path_factory):
    url = os.environ.get("RECURSA_TEST_PG_URL")
    if url:
        yield url
        return
    try:
        import psycopg  # noqa: F401
    except ImportError:
        yield None
        return
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        yield None
        return
    import subprocess

    data = tmp_path_factory.mktemp("pgdata")
    port = _free_port()
    subprocess.run(["initdb", "-D", str(data), "-U", "postgres", "-A", "trust"], check=True, capture_output=True)
    opts = f"-p {port} -c unix_socket_directories='' -c listen_addresses=127.0.0.1 -c fsync=off"
    subprocess.run(["pg_ctl", "-D", str(data), "-o", opts, "-l", str(data / "log"), "-w", "start"], check=True,
                   capture_output=True)
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "stop"], capture_output=True)


@pytest.fixture
def pg_database(pg_admin_url):
    """Factory: a fresh database migrated to the latest schema; returns its URL."""
    if pg_admin_url is None:
        pytest.skip("Postgres not available (set RECURSA_TEST_PG_URL or install initdb)")
    import uuid

    import psycopg

    from recursa import migrations

    created = []

    def make() -> str:
        name = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(pg_admin_url, autocommit=True) as c:
            c.execute(f"CREATE DATABASE {name}")
        created.append(name)
        url = pg_admin_url.rsplit("/", 1)[0] + "/" + name
        with psycopg.connect(url) as c:
            migrations.migrate(c, "postgres")
        return url

    yield make
    with psycopg.connect(pg_admin_url, autocommit=True) as c:
        for name in created:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def recursa_server(pg_database):
    """Factory: start Recursa Server on a fresh database. Returns (base_url, mint) where
    mint(*scopes) creates an API key and returns its token."""
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    import threading
    import time

    import psycopg
    import uvicorn

    from recursa.server import build
    from recursa.server.keys import create

    running = []

    def start():
        db_url = pg_database()
        app = build(db_url, db_url)
        port = _free_port()
        # A short keep-alive makes clients meet server-closed idle connections, as in CI.
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                               timeout_keep_alive=1))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started:
            if time.time() > deadline:
                raise RuntimeError("Recursa Server did not start")
            time.sleep(0.02)
        running.append((server, thread, app))

        def mint(*scopes: str, name: str = "test") -> str:
            with psycopg.connect(db_url) as conn:
                return create(conn, name, list(scopes))[1]

        return f"http://127.0.0.1:{port}", mint, db_url

    yield start
    for server, thread, app in running:
        server.should_exit = True
        thread.join(timeout=10)
        for s in app.state.stores:
            s.close()


ALL_LEARNER_SCOPES = ("optimizer", "gate", "reviewer", "admin")


@pytest.fixture
def server_learner(tmp_path, recursa_server):
    """A bootstrapped project whose records and sealed side live on Recursa Server."""
    import uuid

    url, mint, db_url = recursa_server()
    env = "RECURSA_TEST_KEY_" + uuid.uuid4().hex[:8].upper()
    os.environ[env] = mint(*ALL_LEARNER_SCOPES, name="learner")
    lr = make_project(tmp_path, store={"url": url, "key_env": env})
    lr.server = {"url": url, "mint": mint, "db_url": db_url}
    yield lr
    lr.close()
    os.environ.pop(env, None)
