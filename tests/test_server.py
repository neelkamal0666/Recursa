"""Recursa Server: authentication, scopes, database roles, health, and shipping runs
from the write-ahead log. Needs Postgres (see conftest.pg_admin_url) and recursa[server]."""
import http.client
import json
import os
import re
import uuid

import pytest

pytest.importorskip("fastapi")
psycopg = pytest.importorskip("psycopg")

from recursa.server import app as server_app  # noqa: E402
from recursa.server.keys import SCOPES, create, revoke  # noqa: E402


def _request(base: str, method: str, path: str, token: str | None, body=None):
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, (json.loads(raw) if raw else None)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


@pytest.fixture
def server(recursa_server):
    return recursa_server()


# -- authentication ------------------------------------------------------------------------------
def test_every_route_enforces_its_scopes(server):
    base, mint, _ = server
    tokens = {scope: mint(scope, name=scope) for scope in SCOPES}
    assert len(server_app.ROUTES) >= 40
    for (method, path), allowed in sorted(server_app.ROUTES.items()):
        target = _concrete(path)
        if "ledger/" in target:
            target += "?gate_version=g&kind=gate&candidate_id=c"
        body = {} if method in ("POST", "PUT", "PATCH") else None
        for scope, token in tokens.items():
            status, payload = _request(base, method, target, token, body)
            if scope in allowed:
                assert status not in (401, 403), (method, path, scope, status, payload)
            else:
                assert status == 403, (method, path, scope, status, payload)
        assert _request(base, method, target, None, body)[0] == 401, (method, path)


def test_scope_boundaries_that_matter(server):
    routes = server_app.ROUTES
    readers_of = lambda prefix: {s for (m, p), allowed in routes.items() if m == "GET" and p.startswith(prefix)  # noqa: E731
                                 for s in allowed}
    ingest_reads = {p for (m, p), allowed in routes.items() if m == "GET" and "ingest" in allowed}
    assert ingest_reads == {"/v1/versions", "/v1/versions/{vid}", "/v1/deployments",
                            "/v1/deployments/current"}  # the live config, nothing else
    sealed = {(m, p): a for (m, p), a in routes.items() if p.startswith("/v1/sealed")}
    assert all("optimizer" not in a for a in sealed.values())
    assert readers_of("/v1/sealed/runs") == {"gate"}
    assert {s for (m, p), a in routes.items() if p == "/v1/approvals" for s in a} == {"reviewer"}
    assert "admin" not in routes[("POST", "/v1/approvals")]


def test_invalid_revoked_and_expired_keys_are_rejected(server):
    base, mint, db_url = server
    good = mint("admin")
    assert _request(base, "GET", "/v1/versions", good)[0] == 200
    for bad in ("nonsense", "rk_", good[:-3] + "xyz", "rk_unknown_secret"):
        assert _request(base, "GET", "/v1/versions", bad)[0] == 401
    key_id = good.split("_")[1]
    with psycopg.connect(db_url) as conn:
        assert revoke(conn, key_id)
        _, expired = create(conn, "old", ["admin"], expires_at=1.0)
    assert _request(base, "GET", "/v1/versions", good)[0] == 401
    assert _request(base, "GET", "/v1/versions", expired)[0] == 401


def test_health_endpoints(server):
    base, _, _ = server
    assert _request(base, "GET", "/healthz", None) == (200, {"status": "ok"})
    status, body = _request(base, "GET", "/readyz", None)
    assert status == 200 and body["status"] == "ready" and body["schema"]["pending"] == []
    status, spec = _request(base, "GET", "/v1/openapi.json", None)
    assert status == 200 and "/v1/runs:batch" in spec["paths"]


def test_server_refuses_to_start_on_unmigrated_database(pg_admin_url):
    if pg_admin_url is None:
        pytest.skip("Postgres not available")
    from recursa.migrations import SchemaError
    from recursa.server import build

    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_admin_url, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    try:
        url = pg_admin_url.rsplit("/", 1)[0] + "/" + name
        with pytest.raises(SchemaError, match="recursa server migrate"):
            build(url, url)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as c:
            c.execute(f"DROP DATABASE {name} WITH (FORCE)")


# -- database roles --------------------------------------------------------------------------------
def test_database_roles_enforce_append_only_and_the_sealed_boundary(pg_database, pg_admin_url):
    from recursa.server import build
    from recursa.store.http import HttpRecordStore, HttpSealedStore

    owner_url = pg_database()
    tag = uuid.uuid4().hex[:8]
    app_role, gate_role = f"app_{tag}", f"gate_{tag}"
    with psycopg.connect(owner_url, autocommit=True) as c:
        c.execute(f"CREATE ROLE {app_role} LOGIN PASSWORD 'pw' IN ROLE recursa_app")
        c.execute(f"CREATE ROLE {gate_role} LOGIN PASSWORD 'pw' IN ROLE recursa_gate")
        c.execute("INSERT INTO recursa.runs (id) VALUES ('r1')")
        c.execute("INSERT INTO recursa.candidates (id, status) VALUES ('c1', 'proposed')")
    rest = owner_url.split("@", 1)[1]
    app_url, gate_url = f"postgresql://{app_role}:pw@{rest}", f"postgresql://{gate_role}:pw@{rest}"
    try:
        with psycopg.connect(app_url, autocommit=True) as c:
            for stmt in ("UPDATE recursa.runs SET mode='x'", "DELETE FROM recursa.runs",
                         "SELECT * FROM recursa_sealed.runs", "INSERT INTO recursa.api_keys (id) VALUES ('k')",
                         "INSERT INTO recursa.schema_migrations VALUES (99, 'x', 'x', 0)",
                         "CREATE TABLE recursa.evil (x int)"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(stmt)
            c.execute("UPDATE recursa.candidates SET status='gate_rejected' WHERE id='c1'")  # mutable state
            c.execute("INSERT INTO recursa.runs (id) VALUES ('r2')")
        with psycopg.connect(gate_url, autocommit=True) as c:
            for stmt in ("SELECT * FROM recursa.runs", "SELECT * FROM recursa.candidates",
                         "UPDATE recursa_sealed.gate_ledger SET kind='x'"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(stmt)
            c.execute("INSERT INTO recursa_sealed.gate_ledger VALUES ('g', 'c', 'gate', 0)")

        # The server runs with these least-privilege roles.
        import threading
        import time

        import uvicorn

        from conftest import _free_port

        app = build(app_url, gate_url)
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        while not srv.started:
            time.sleep(0.02)
        try:
            with psycopg.connect(owner_url) as c:
                token = create(c, "all", ["optimizer", "gate", "reviewer", "admin"])[1]
            base = f"http://127.0.0.1:{port}"
            records, sealed = HttpRecordStore(base, token), HttpSealedStore(base, token)
            records.put_version(None, {"surfaces": {}}, None, ["init"])
            records.audit("t", "boot", "v1")
            assert records.verify_audit_chain() and sealed.ledger_add("g2", "c", "gate")
            records.close()
            sealed.close()
        finally:
            srv.should_exit = True
            t.join(timeout=10)
            for s in app.state.stores:
                s.close()
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as c:
            for role in (app_role, gate_role):
                c.execute(f"DROP OWNED BY {role}")
                c.execute(f"DROP ROLE {role}")


# -- connection handling -------------------------------------------------------------------------------
def test_client_survives_server_closing_idle_connections(server, monkeypatch):
    import time

    import recursa.store.http as http_store
    from recursa.store.http import HttpRecordStore

    base, mint, _ = server
    records = HttpRecordStore(base, mint("reviewer", "admin"))
    records.audit("t", "first", "s")
    monkeypatch.setattr(http_store, "_MAX_IDLE_S", 60.0)  # reuse the connection after the server closed it
    time.sleep(1.5)  # server keep-alive is 1 s in tests
    records.audit("t", "second", "s")  # not retryable in general, but a dead idle connection never sent it
    monkeypatch.setattr(http_store, "_MAX_IDLE_S", 0.5)  # and normally idle connections are replaced first
    time.sleep(1.5)
    records.audit("t", "third", "s")
    assert [e["action"] for e in records.audit_log()] == ["first", "second", "third"]
    assert records.verify_audit_chain()
    records.close()


# -- write-ahead log shipping --------------------------------------------------------------------------
def test_runs_survive_a_server_outage_and_ship_exactly_once(server_learner):
    import asyncio

    from recursa.learner import Learner
    from recursa.project import load_config
    from recursa.store.http import HttpRecordStore

    srv = server_learner.server
    root = server_learner.cfg.root
    env = "RECURSA_TEST_INGEST_" + uuid.uuid4().hex[:6].upper()
    os.environ[env] = srv["mint"]("ingest", name="production")
    cfg_path = os.path.join(root, "recursa.yaml")
    text = open(cfg_path).read()
    key_line = next(line for line in text.splitlines() if "key_env" in line)
    open(cfg_path, "w").write(text.replace(key_line, f"  key_env: {env}"))
    try:
        prod = Learner(load_config(cfg_path))  # a production process holding only an ingest key
        tasks = [{"ticket": f"my invoice is wrong #{i}"} for i in range(5)]
        runs = [asyncio.run(prod.run(t)) for t in tasks[:3]]
        with pytest.raises(PermissionError):
            prod.store.get_run(runs[0].id)  # ingest keys cannot read runs back

        good_store, prod.store = prod.store, HttpRecordStore("http://127.0.0.1:9", os.environ[env], timeout=0.5)
        prod.store.c.retries = 0
        runs += [asyncio.run(prod.run(t)) for t in tasks[3:]]  # keeps serving on the last-known version
        assert prod.stale_version_serves == 2
        with pytest.raises(Exception):
            prod.flush()  # server unreachable: nothing lost
        assert prod.wal_bytes() > 0

        prod.store = good_store
        assert prod.flush() == 5 and prod.wal_bytes() == 0
        assert prod.flush() == 0
        admin = server_learner.store
        stored = [admin.get_run(r.id) for r in runs]
        assert all(s is not None and s["mode"] == "online" and s["received_at"] for s in stored)
        prod.close()
    finally:
        os.environ.pop(env, None)


def test_background_shipper(server_learner):
    import asyncio
    import time

    lr = server_learner
    run = asyncio.run(lr.run({"ticket": "refund please"}))
    stop = lr.start_shipping(interval_s=0.05)
    deadline = time.time() + 5
    while lr.store.get_run(run.id) is None and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    assert lr.store.get_run(run.id) is not None and lr.ship_errors == 0


# -- CLI -------------------------------------------------------------------------------------------------
def test_cli_keys_and_status(pg_database, capsys, monkeypatch):
    from recursa.cli import main

    url = pg_database()
    monkeypatch.setenv("RECURSA_DATABASE_URL", url)
    main(["server", "status"])
    assert json.loads(capsys.readouterr().out)["pending"] == []
    main(["server", "keys", "create", "ci", "--scope", "ingest", "--scope", "optimizer"])
    out = capsys.readouterr().out
    key_id = re.search(r"created key (\w+)", out).group(1)
    assert "rk_" + key_id + "_" in out
    main(["server", "keys", "list"])
    listed = json.loads(capsys.readouterr().out)
    assert [(k["id"], k["scopes"], k["revoked_at"]) for k in listed] == [(key_id, ["ingest", "optimizer"], None)]
    main(["server", "keys", "revoke", key_id])
    main(["server", "keys", "list"])
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])[0]["revoked_at"] is not None
    main(["server", "migrate"])
    assert json.loads(capsys.readouterr().out)["applied"] == []
