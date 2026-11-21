"""Recursa Server: owns Postgres and serves the store API (``pip install recursa[server]``).

Configuration comes from the environment, so secrets never sit in a config file:

- ``RECURSA_DATABASE_URL``         records connection (a login role in ``recursa_app``). Required.
- ``RECURSA_SEALED_DATABASE_URL``  sealed connection (a login role in ``recursa_gate``).
                                   Defaults to the records URL; use a separate role in production.
- ``RECURSA_MIGRATE_DATABASE_URL`` schema owner, used only by ``migrate`` and ``keys``.
                                   Defaults to the records URL.

Commands (``recursa server ...``): ``run``, ``migrate``, ``status``, ``keys create|list|revoke``.
The server refuses to start unless the schema is exactly at this release's version.
"""
from __future__ import annotations

import os
from typing import Any

ENV_URL, ENV_SEALED_URL, ENV_MIGRATE_URL = ("RECURSA_DATABASE_URL", "RECURSA_SEALED_DATABASE_URL",
                                            "RECURSA_MIGRATE_DATABASE_URL")


def urls(env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    url = env.get(ENV_URL)
    if not url:
        raise RuntimeError(f"set {ENV_URL} to the server's Postgres URL")
    return {"records": url, "sealed": env.get(ENV_SEALED_URL) or url, "migrate": env.get(ENV_MIGRATE_URL) or url}


def migrate(migrate_url: str) -> dict[str, Any]:
    import psycopg

    from .. import migrations

    with psycopg.connect(migrate_url) as conn:
        applied = migrations.migrate(conn, "postgres")
        return {"applied": applied, **migrations.status(conn, "postgres")}


def status(url: str) -> dict[str, Any]:
    import psycopg

    from .. import migrations

    with psycopg.connect(url) as conn:
        return migrations.status(conn, "postgres")


def build(records_url: str, sealed_url: str):
    """Open both stores (checking the schema) and return the ASGI app."""
    from ..store.postgres import PostgresRecordStore, PostgresSealedStore, check_schema
    from .app import create_app
    from .keys import Verifier

    records = PostgresRecordStore(records_url)
    sealed = PostgresSealedStore(sealed_url)
    app = create_app(records, sealed, Verifier(records.pool), ready=lambda: {"schema": check_schema(records_url)})
    app.state.stores = (records, sealed)
    return app


def serve(host: str = "127.0.0.1", port: int = 8080, run_migrations: bool = False) -> None:
    import uvicorn

    u = urls()
    if run_migrations:
        migrate(u["migrate"])
    app = build(u["records"], u["sealed"])
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        for s in app.state.stores:
            s.close()
