"""Versioned schema migrations.

Each backend has a directory of numbered SQL files (``NNNN_name.sql``) that ship
with the package. ``schema_migrations`` records which ones have run and a checksum
of each, so a database can be checked against the code before anything is read or
written:

- pending migrations: apply them (``recursa db migrate``, or automatically for a
  local SQLite store), or refuse to run;
- a database newer than this release: refuse, never guess at a downgrade;
- an applied migration whose file has since changed: refuse, because the history
  no longer describes the database.

Migrations are forward-only. Fixing a mistake means adding a new migration.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

from ..types import now

_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update BEFORE UPDATE ON schema_migrations
  BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete BEFORE DELETE ON schema_migrations
  BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END;
"""


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


def load(backend: str = "sqlite") -> list[Migration]:
    out = []
    for f in resources.files(__package__).joinpath(backend).iterdir():
        m = _NAME.match(f.name)
        if m:
            out.append(Migration(int(m.group(1)), m.group(2), f.read_text()))
    out.sort(key=lambda m: m.version)
    if [m.version for m in out] != list(range(1, len(out) + 1)):
        raise SchemaError(f"{backend} migrations must be numbered 0001, 0002, ... without gaps")
    return out


def _statements(sql: str) -> list[str]:
    """Split a script into statements; complete_statement keeps trigger bodies whole."""
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


def _applied(db: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    return {v: (n, c) for v, n, c in db.execute("SELECT version, name, checksum FROM schema_migrations")}


def _adopt_legacy(db: sqlite3.Connection, migrations: list[Migration]) -> None:
    """Databases created before versioned migrations (0.1.0) have tables but no ledger.
    0001 is idempotent and simply runs; later steps already present are recorded so
    they are not applied twice."""
    if _applied(db):
        return
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "runs" not in tables:
        return
    if "telemetry" in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
        m = migrations[1]
        db.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (m.version, m.name, m.checksum, now()))


def status(db: sqlite3.Connection, backend: str = "sqlite") -> dict:
    migrations = load(backend)
    known = {m.version: m for m in migrations}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    applied = _applied(db) if "schema_migrations" in tables else {}
    legacy = not applied and "runs" in tables
    return {
        "current": max(applied, default=0),
        "latest": migrations[-1].version,
        "pending": [f"{m.version:04d}_{m.name}" for m in migrations if m.version not in applied],
        "unknown": sorted(v for v in applied if v not in known),
        "modified": sorted(v for v, (_, c) in applied.items() if v in known and known[v].checksum != c),
        "legacy_unversioned": legacy,
    }


def migrate(db: sqlite3.Connection, backend: str = "sqlite", apply: bool = True) -> list[str]:
    """Bring the database to the latest version, or raise SchemaError if it cannot or
    (with apply=False) would need to change. Returns the migrations applied."""
    migrations = load(backend)
    known = {m.version: m for m in migrations}
    db.executescript(LEDGER)
    db.execute("BEGIN IMMEDIATE")  # one writer migrates; others wait, then see the result
    try:
        _adopt_legacy(db, migrations)
        applied = _applied(db)
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
            raise SchemaError(f"database schema is out of date ({len(pending)} pending: "
                              f"{', '.join(f'{m.version:04d}_{m.name}' for m in pending)}); run `recursa db migrate`")
        for m in pending:
            for stmt in _statements(m.sql):
                db.execute(stmt)
            db.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (m.version, m.name, m.checksum, now()))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return [f"{m.version:04d}_{m.name}" for m in pending]
