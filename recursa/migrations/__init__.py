"""Versioned schema migrations.

Each backend has a directory of numbered SQL files (``NNNN_name.sql``) that ship
with the package. Version numbers line up across backends: version N means the
same logical schema in SQLite and Postgres. ``schema_migrations`` records which
migrations have run and a checksum of each, so a database can be checked against
the code before anything is read or written:

- pending migrations: apply them (``recursa db migrate`` / ``recursa server
  migrate``, or automatically for a local SQLite store), or refuse to run;
- a database newer than this release: refuse, never guess at a downgrade;
- an applied migration whose file has since changed: refuse, because the history
  no longer describes the database.

Migrations are forward-only. Fixing a mistake means adding a new migration.
All pending migrations apply in one transaction under a lock, so concurrent
processes cannot both migrate and a failure leaves the database unchanged.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from typing import Any

from ..types import now

_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
BACKENDS = ("sqlite", "postgres")


class SchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def load(backend: str = "sqlite") -> list[Migration]:
    if backend not in BACKENDS:
        raise SchemaError(f"unknown backend {backend!r}")
    out = []
    for f in resources.files(__package__).joinpath(backend).iterdir():
        m = _NAME.match(f.name)
        if m:
            out.append(Migration(int(m.group(1)), m.group(2), f.read_text()))
    out.sort(key=lambda m: m.version)
    if [m.version for m in out] != list(range(1, len(out) + 1)):
        raise SchemaError(f"{backend} migrations must be numbered 0001, 0002, ... without gaps")
    return out


# -- drivers -------------------------------------------------------------------------------------
class _SQLite:
    ledger = "schema_migrations"
    create_ledger = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update BEFORE UPDATE ON schema_migrations
  BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete BEFORE DELETE ON schema_migrations
  BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        return list(self.db.execute(sql, args))

    def tables(self) -> set[str]:
        return {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}

    def has_ledger(self) -> bool:
        return self.ledger in self.tables()

    def ensure_ledger(self) -> None:
        self.db.executescript(self.create_ledger)

    def begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")  # one writer migrates; others wait, then see the result

    def commit(self) -> None:
        self.db.execute("COMMIT")

    def rollback(self) -> None:
        self.db.execute("ROLLBACK")

    def run_script(self, sql: str) -> None:
        for stmt in _statements(sql):  # executescript would commit the open transaction
            self.db.execute(stmt)

    def record(self, m: Migration) -> None:
        self.db.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (m.version, m.name, m.checksum, now()))

    def adopt_legacy(self, migrations: list[Migration], applied: dict) -> None:
        """Stores created by 0.1.0 before the ledger existed have tables but no ledger.
        0001 is idempotent and simply runs; later steps already present are recorded so
        they are not applied twice."""
        if applied or "runs" not in self.tables():
            return
        if "telemetry" in {r[1] for r in self.rows("PRAGMA table_info(runs)")}:
            self.record(migrations[1])


class _Postgres:
    ledger = "recursa.schema_migrations"
    create_ledger = """
CREATE SCHEMA IF NOT EXISTS recursa;
CREATE TABLE IF NOT EXISTS recursa.schema_migrations (
  version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL, applied_at double precision NOT NULL);
CREATE OR REPLACE FUNCTION recursa.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'schema_migrations_append_only') THEN
    CREATE TRIGGER schema_migrations_append_only BEFORE UPDATE OR DELETE ON recursa.schema_migrations
      FOR EACH ROW EXECUTE FUNCTION recursa.reject_change();
  END IF;
END $$;
"""
    LOCK_KEY = 7262817  # arbitrary, constant: serialises migrators on one database

    def __init__(self, conn: Any):
        self.db = conn

    def rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.db.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall()) if cur.description else []

    def has_ledger(self) -> bool:
        return bool(self.rows("SELECT to_regclass('recursa.schema_migrations')")[0][0])

    def ensure_ledger(self) -> None:
        self.db.execute("SELECT pg_advisory_lock(%s)", (self.LOCK_KEY,))
        try:
            self.db.execute(self.create_ledger)
            self.db.commit()
        finally:
            self.db.execute("SELECT pg_advisory_unlock(%s)", (self.LOCK_KEY,))
            self.db.commit()

    def begin(self) -> None:
        self.db.execute("SELECT pg_advisory_xact_lock(%s)", (self.LOCK_KEY,))

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def run_script(self, sql: str) -> None:
        self.db.execute(sql)  # no parameters: psycopg sends it as one multi-statement query

    def record(self, m: Migration) -> None:
        self.db.execute("INSERT INTO recursa.schema_migrations VALUES (%s,%s,%s,%s)",
                        (m.version, m.name, m.checksum, now()))

    def adopt_legacy(self, migrations: list[Migration], applied: dict) -> None:
        pass  # Postgres support started with versioned migrations


def _driver(db: Any, backend: str):
    return _SQLite(db) if backend == "sqlite" else _Postgres(db)


def _statements(sql: str) -> list[str]:
    """Split a SQLite script into statements; complete_statement keeps trigger bodies whole."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip() and not all(ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()):
        raise SchemaError(f"unterminated SQL statement: {buf.strip()[:80]}")
    return out


def _applied(d) -> dict[int, tuple[str, str]]:
    return {v: (n, c) for v, n, c in d.rows(f"SELECT version, name, checksum FROM {d.ledger}")}


# -- public API ----------------------------------------------------------------------------------
def status(db: Any, backend: str = "sqlite") -> dict:
    migrations = load(backend)
    known = {m.version: m for m in migrations}
    d = _driver(db, backend)
    applied = _applied(d) if d.has_ledger() else {}
    legacy = backend == "sqlite" and not applied and "runs" in d.tables()
    if backend == "postgres":
        db.rollback()  # end the read transaction psycopg opened
    return {
        "current": max(applied, default=0),
        "latest": migrations[-1].version,
        "pending": [m.label for m in migrations if m.version not in applied],
        "unknown": sorted(v for v in applied if v not in known),
        "modified": sorted(v for v, (_, c) in applied.items() if v in known and known[v].checksum != c),
        "legacy_unversioned": legacy,
    }


def migrate(db: Any, backend: str = "sqlite", apply: bool = True) -> list[str]:
    """Bring the database to the latest version, or raise SchemaError if it cannot or
    (with apply=False) would need to change. Returns the migrations applied."""
    migrations = load(backend)
    known = {m.version: m for m in migrations}
    d = _driver(db, backend)
    if apply or not d.has_ledger():
        if not apply and backend == "postgres":
            db.rollback()
            raise SchemaError("database has no Recursa schema; run `recursa server migrate`")
        d.ensure_ledger()
    d.begin()
    try:
        applied = _applied(d)
        d.adopt_legacy(migrations, applied)
        applied = _applied(d)
        newer = sorted(v for v in applied if v not in known)
        if newer:
            raise SchemaError(f"database is at schema version {max(applied)} but this Recursa release only knows "
                              f"up to {migrations[-1].version}; upgrade Recursa")
        for v, (name, checksum) in applied.items():
            if known[v].checksum != checksum:
                raise SchemaError(f"migration {v:04d}_{name} has changed since it was applied; migrations are "
                                  "forward-only, so add a new one instead of editing an old one")
        pending = [m for m in migrations if m.version not in applied]
        if pending and not apply:
            hint = "recursa db migrate" if backend == "sqlite" else "recursa server migrate"
            raise SchemaError(f"database schema is out of date ({len(pending)} pending: "
                              f"{', '.join(m.label for m in pending)}); run `{hint}`")
        for m in pending:
            d.run_script(m.sql)
            d.record(m)
        d.commit()
    except BaseException:
        d.rollback()
        raise
    return [m.label for m in pending]


def for_path(path: str, apply: bool, backend: str = "sqlite") -> dict:
    """Status of the SQLite database at ``path`` (applying pending migrations first if ``apply``)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        applied = migrate(db, backend) if apply else None
        out = status(db, backend)
        if applied is not None:
            out["applied"] = applied
        return out
    finally:
        db.close()
