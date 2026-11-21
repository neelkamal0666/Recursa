"""API keys for Recursa Server.

A key is ``rk_<id>_<secret>``. Only a SHA-256 hash of the secret is stored; the
secret is shown once, at creation. Secrets are 256 random bits, so a fast hash is
enough (there is nothing to brute-force). Each key carries one or more scopes:

- ``ingest``:    append runs and feedback; read the live version (production agents)
- ``optimizer``: read and write train/dev evaluation runs, candidates and insights
- ``gate``:      everything on the sealed side; update candidates after gating
- ``reviewer``:  read packets and records; approve, promote, roll back
- ``admin``:     operate the server (bootstrap versions, read everything); cannot approve

Creating and revoking keys needs the schema owner's connection (the server's own
database role can only read keys and record when they were last used).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from ..types import now

SCOPES = ("ingest", "optimizer", "gate", "reviewer", "admin")
PREFIX = "rk_"
_TOUCH_EVERY_S = 60


@dataclass(frozen=True)
class Principal:
    key_id: str
    name: str
    scopes: frozenset[str]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse(token: str) -> tuple[str, str] | None:
    if not token.startswith(PREFIX):
        return None
    key_id, _, secret = token[len(PREFIX):].partition("_")
    return (key_id, secret) if key_id and secret else None


def create(conn: Any, name: str, scopes: list[str], expires_at: float | None = None) -> tuple[str, str]:
    """Create a key with the owner's connection. Returns (key_id, token); the token is not stored."""
    from psycopg.types.json import Jsonb

    bad = set(scopes) - set(SCOPES)
    if not scopes or bad:
        raise ValueError(f"scopes must be a non-empty subset of {', '.join(SCOPES)}; got {sorted(bad) or scopes}")
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    conn.execute("INSERT INTO recursa.api_keys (id, name, scopes_json, secret_hash, created_at, expires_at) "
                 "VALUES (%s,%s,%s,%s,%s,%s)", (key_id, name, Jsonb(sorted(set(scopes))), _hash(secret), now(),
                                                expires_at))
    conn.commit()
    return key_id, f"{PREFIX}{key_id}_{secret}"


def revoke(conn: Any, key_id: str) -> bool:
    n = conn.execute("UPDATE recursa.api_keys SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
                     (now(), key_id)).rowcount
    conn.commit()
    return n == 1


def list_keys(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT id, name, scopes_json, created_at, expires_at, revoked_at, last_used_at "
                       "FROM recursa.api_keys ORDER BY created_at")
    cols = [d.name for d in cur.description]
    out = []
    for row in cur.fetchall():
        r = dict(row) if isinstance(row, dict) else dict(zip(cols, row))
        r["scopes"] = r.pop("scopes_json")
        if isinstance(r["scopes"], str):
            r["scopes"] = json.loads(r["scopes"])
        out.append(r)
    return out


class Verifier:
    """Checks bearer tokens against the api_keys table using the server's pool."""

    def __init__(self, pool: Any):
        self.pool = pool
        self._touched: dict[str, float] = {}

    def verify(self, token: str) -> Principal | None:
        parsed = parse(token or "")
        if parsed is None:
            return None
        key_id, secret = parsed
        with self.pool.connection() as conn:
            row = conn.execute("SELECT name, scopes_json, secret_hash, expires_at, revoked_at FROM recursa.api_keys "
                               "WHERE id=%s", (key_id,)).fetchone()
            if row is None or not hmac.compare_digest(row["secret_hash"], _hash(secret)):
                return None
            t = now()
            if row["revoked_at"] is not None or (row["expires_at"] is not None and row["expires_at"] < t):
                return None
            if t - self._touched.get(key_id, 0) > _TOUCH_EVERY_S:
                conn.execute("UPDATE recursa.api_keys SET last_used_at=%s WHERE id=%s", (t, key_id))
                self._touched[key_id] = t
        return Principal(key_id, row["name"], frozenset(row["scopes_json"]))
