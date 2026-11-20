"""Dataset versioning and sealed evaluation sets.

The labelled pool is split into four disjoint sets by example group:

* train, dev   -> plain JSONL under ``<home>/data``; the optimizer reads these.
* gate, confirm -> AES-GCM encrypted under ``<home>/sealed``; the key lives in
  ``<home>/keys`` (0600). Only the gate process holds the key. The optimizer
  subprocess is additionally blocked from opening anything under ``sealed/``,
  ``keys/`` or ``auth/`` (see ``recursa.isolation``).

Every set version is immutable and named by its content hash. Rotation creates a
new version; a retired gate or confirmation version moves into dev.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .types import Example, canonical_json, content_hash

SPLIT_FRACTIONS = {"train": 0.15, "dev": 0.30, "gate": 0.35, "confirm": 0.20}
SEALED_KINDS = ("gate", "confirm")


class SealedAccessError(PermissionError):
    pass


@dataclass
class Paths:
    home: str

    @property
    def db(self) -> str:
        return os.path.join(self.home, "recursa.db")

    @property
    def data(self) -> str:
        return os.path.join(self.home, "data")

    @property
    def sealed(self) -> str:
        return os.path.join(self.home, "sealed")

    @property
    def sealed_db(self) -> str:
        return os.path.join(self.sealed, "gate.db")

    @property
    def keys(self) -> str:
        return os.path.join(self.home, "keys")

    @property
    def key_file(self) -> str:
        return os.path.join(self.keys, "gate.key")

    @property
    def auth(self) -> str:
        return os.path.join(self.home, "auth")

    @property
    def wal(self) -> str:
        return os.path.join(self.home, "wal")

    def protected_dirs(self) -> list[str]:
        """Everything the optimizer process must never read."""
        return [self.sealed, self.keys, self.auth, self.db, self.db + "-wal", self.db + "-shm"]

    def ensure(self) -> None:
        os.makedirs(self.data, exist_ok=True)
        for d in (self.sealed, self.keys, self.auth):
            os.makedirs(d, exist_ok=True)
            os.chmod(d, stat.S_IRWXU)  # 0700


def load_jsonl(path: str) -> list[Example]:
    with open(path) as f:
        return [Example.from_dict(json.loads(line)) for line in f if line.strip()]


def dump_jsonl(examples: Iterable[Example]) -> bytes:
    return "".join(canonical_json(e.to_dict()) + "\n" for e in examples).encode()


def assign_split(group: str, seed: str = "recursa") -> str:
    u = int(hashlib.sha256(f"{seed}:{group}".encode()).hexdigest()[:12], 16) / 16 ** 12
    acc = 0.0
    for name, frac in SPLIT_FRACTIONS.items():
        acc += frac
        if u < acc:
            return name
    return "confirm"


# -- keys & crypto -------------------------------------------------------------------
def create_key(paths: Paths) -> None:
    if os.path.exists(paths.key_file):
        return
    fd = os.open(paths.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(AESGCM.generate_key(bit_length=256))


def _load_key(paths: Paths) -> bytes:
    try:
        with open(paths.key_file, "rb") as f:
            return f.read()
    except PermissionError as e:
        raise SealedAccessError("this process may not read the gate key") from e


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)


# -- open splits (train / dev) -----------------------------------------------------------
class OpenSplits:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.manifest_path = os.path.join(paths.data, "manifest.json")

    def manifest(self) -> dict[str, Any]:
        if not os.path.exists(self.manifest_path):
            return {}
        with open(self.manifest_path) as f:
            return json.load(f)

    def write(self, name: str, examples: list[Example]) -> str:
        blob = dump_jsonl(sorted(examples, key=lambda e: e.id))
        version = content_hash(blob)[:16]
        path = os.path.join(self.paths.data, f"{name}-{version}.jsonl")
        with open(path, "wb") as f:
            f.write(blob)
        m = self.manifest()
        m[name] = {"version": version, "file": os.path.basename(path), "n": len(examples)}
        with open(self.manifest_path, "w") as f:
            json.dump(m, f, indent=2)
        return version

    def load(self, name: str) -> tuple[str, list[Example]]:
        entry = self.manifest().get(name)
        if not entry:
            return "", []
        return entry["version"], load_jsonl(os.path.join(self.paths.data, entry["file"]))


# -- sealed splits (gate / confirm) ------------------------------------------------------
class SealedSets:
    """Gate-side access to the sealed sets. Requires the key."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.manifest_path = os.path.join(paths.sealed, "manifest.json")

    def manifest(self) -> dict[str, list[dict[str, Any]]]:
        if not os.path.exists(self.manifest_path):
            return {k: [] for k in SEALED_KINDS}
        with open(self.manifest_path) as f:
            return json.load(f)

    def _save_manifest(self, m: dict[str, Any]) -> None:
        with open(self.manifest_path, "w") as f:
            json.dump(m, f, indent=2)

    def seal(self, kind: str, examples: list[Example]) -> str:
        assert kind in SEALED_KINDS
        if not examples:
            raise ValueError(f"cannot seal an empty {kind} set")
        key = _load_key(self.paths)
        blob = dump_jsonl(sorted(examples, key=lambda e: e.id))
        version = f"{kind}-{content_hash(blob)[:12]}"
        with open(os.path.join(self.paths.sealed, f"{version}.enc"), "wb") as f:
            f.write(_encrypt(key, blob, version.encode()))
        m = self.manifest()
        for entry in m[kind]:
            if entry["status"] == "active":
                raise RuntimeError(f"an active {kind} set already exists ({entry['version']}); retire it first")
        m[kind].append({"version": version, "n": len(examples), "status": "active"})
        self._save_manifest(m)
        return version

    def active(self, kind: str) -> str | None:
        for entry in self.manifest()[kind]:
            if entry["status"] == "active":
                return entry["version"]
        return None

    def load(self, version: str) -> list[Example]:
        key = _load_key(self.paths)
        with open(os.path.join(self.paths.sealed, f"{version}.enc"), "rb") as f:
            blob = _decrypt(key, f.read(), version.encode())
        return [Example.from_dict(json.loads(line)) for line in blob.decode().splitlines() if line.strip()]

    def retire(self, kind: str, open_splits: OpenSplits) -> str | None:
        """Retire the active version of ``kind`` into dev. Returns the new dev version."""
        version = self.active(kind)
        if version is None:
            return None
        retired = self.load(version)
        m = self.manifest()
        for entry in m[kind]:
            if entry["version"] == version:
                entry["status"] = "retired"
        self._save_manifest(m)
        _, dev = open_splits.load("dev")
        seen = {e.id for e in dev}
        return open_splits.write("dev", dev + [e for e in retired if e.id not in seen])


def split_and_seal(paths: Paths, pool: list[Example], seed: str = "recursa") -> dict[str, Any]:
    """Split a labelled pool into train/dev (open) and gate/confirm (sealed)."""
    paths.ensure()
    create_key(paths)
    ids = [e.id for e in pool]
    if len(ids) != len(set(ids)):
        raise ValueError("example ids must be unique")
    buckets: dict[str, list[Example]] = {k: [] for k in SPLIT_FRACTIONS}
    for e in pool:
        buckets[assign_split(e.group, seed)].append(e)
    open_splits = OpenSplits(paths)
    sealed = SealedSets(paths)
    out = {"train": open_splits.write("train", buckets["train"]), "dev": open_splits.write("dev", buckets["dev"])}
    for kind in SEALED_KINDS:
        out[kind] = sealed.seal(kind, buckets[kind])
    out["counts"] = {k: len(v) for k, v in buckets.items()}
    return out
