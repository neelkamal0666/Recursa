"""Reviewer identity for v1: local passphrase accounts and short-lived signed tokens.

* ``reviewers.yaml`` (in the repo) names reviewers, their surfaces and the quorum.
* ``<home>/auth/credentials.json`` holds scrypt hashes of reviewer passphrases.
* ``<home>/auth/secret`` (0600) signs short-lived HMAC tokens (default 15 minutes).

OIDC/SSO is the post-v1 identity provider; the token format is the seam for it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

import yaml

TOKEN_TTL_S = 15 * 60


class AuthError(PermissionError):
    pass


@dataclass
class Reviewers:
    reviewers: dict[str, dict[str, Any]]
    quorum: dict[str, int]

    @classmethod
    def load(cls, path: str) -> "Reviewers":
        if not os.path.exists(path):
            raise AuthError(f"no reviewers file at {path}; every promotion needs a named human reviewer")
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        revs = {r["id"]: r for r in raw.get("reviewers", [])}
        if not revs:
            raise AuthError("reviewers.yaml lists no reviewers")
        quorum = {"default": 1, **{k: int(v) for k, v in (raw.get("quorum") or {}).items()}}
        return cls(revs, quorum)

    def can_review(self, reviewer: str, surface: str) -> bool:
        r = self.reviewers.get(reviewer)
        return bool(r) and (surface in r.get("surfaces", [surface]) or "*" in r.get("surfaces", []))

    def quorum_for(self, surface: str) -> int:
        return max(1, self.quorum.get(surface, self.quorum["default"]))


class Auth:
    def __init__(self, auth_dir: str):
        self.dir = auth_dir
        os.makedirs(auth_dir, mode=0o700, exist_ok=True)
        self.cred_path = os.path.join(auth_dir, "credentials.json")
        self.secret_path = os.path.join(auth_dir, "secret")
        if not os.path.exists(self.secret_path):
            fd = os.open(self.secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))

    def _secret(self) -> bytes:
        with open(self.secret_path, "rb") as f:
            return f.read()

    def _creds(self) -> dict[str, Any]:
        if not os.path.exists(self.cred_path):
            return {}
        with open(self.cred_path) as f:
            return json.load(f)

    def set_passphrase(self, reviewer: str, passphrase: str) -> None:
        if len(passphrase) < 10:
            raise AuthError("passphrase must be at least 10 characters")
        salt = secrets.token_bytes(16)
        h = hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 14, r=8, p=1)
        creds = self._creds()
        creds[reviewer] = {"salt": salt.hex(), "hash": h.hex()}
        fd = os.open(self.cred_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f)
        os.replace(self.cred_path + ".tmp", self.cred_path)

    def verify_passphrase(self, reviewer: str, passphrase: str) -> bool:
        c = self._creds().get(reviewer)
        if not c:
            return False
        h = hashlib.scrypt(passphrase.encode(), salt=bytes.fromhex(c["salt"]), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(h.hex(), c["hash"])

    def issue_token(self, reviewer: str, passphrase: str, channel: str = "cli", ttl_s: int = TOKEN_TTL_S) -> str:
        if not self.verify_passphrase(reviewer, passphrase):
            raise AuthError("invalid reviewer or passphrase")
        body = {"sub": reviewer, "ch": channel, "iat": int(time.time()), "exp": int(time.time()) + ttl_s,
                "n": secrets.token_hex(8)}
        raw = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode()).decode()
        sig = hmac.new(self._secret(), raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def verify_token(self, token: str | None) -> dict[str, Any]:
        if not token or "." not in token:
            raise AuthError("not logged in: run `recursa auth login <reviewer>`")
        raw, sig = token.rsplit(".", 1)
        want = hmac.new(self._secret(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            raise AuthError("invalid token signature")
        body = json.loads(base64.urlsafe_b64decode(raw.encode()))
        if body["exp"] < time.time():
            raise AuthError("token expired: log in again")
        return body


def token_file(project: str) -> str:
    override = os.environ.get("RECURSA_TOKEN_FILE")
    if override:
        return override
    return os.path.expanduser(os.path.join("~", ".recursa", "tokens", f"{project}.token"))


def save_token(project: str, token: str) -> str:
    path = token_file(project)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return path


def load_token(project: str) -> str | None:
    env = os.environ.get("RECURSA_TOKEN")
    if env:
        return env
    try:
        with open(token_file(project)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
