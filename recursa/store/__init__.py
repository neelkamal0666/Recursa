"""Storage for Recursa.

Code outside this package depends on the ``RecordStore`` and ``SealedStore``
protocols and gets an instance from a factory, never from a backend class:

    store = open_store(config)            # runs, versions, candidates, audit, ...
    sealed = open_sealed_store(config)    # gate ledger, gate checkpoints, packets

Backends: ``LocalStore`` (SQLite, the default), and ``HttpRecordStore`` /
``HttpSealedStore`` when ``store.url`` points at Recursa Server. The server itself
uses the Postgres stores in ``recursa.store.postgres``.

``Store`` is kept as an alias of ``LocalStore`` so ``from recursa.store import Store``
from 0.1.x keeps working.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..redact import Redactor
from .base import RecordStore, SealedStore
from .local import LocalStore

if TYPE_CHECKING:
    from ..data import Paths
    from ..project import Config

Store = LocalStore  # backward compatibility with 0.1.x

__all__ = ["LocalStore", "RecordStore", "SealedStore", "Store", "open_scratch_store", "open_sealed_store",
           "open_store"]


def _paths(config: "Config") -> "Paths":
    from ..data import Paths

    return Paths(config.home_path)


def _auto_migrate(config: "Config") -> bool:
    return bool(config.store.get("auto_migrate", True))


def _key(config: "Config", setting: str) -> str:
    import os

    env = config.store.get(setting) or config.store.get("key_env") or "RECURSA_API_KEY"
    return os.environ.get(env, "")


def open_store(config: "Config", redactor: Redactor | None = None) -> RecordStore:
    if config.store.get("url"):
        from .http import HttpRecordStore

        return HttpRecordStore(config.store["url"], _key(config, "key_env"), float(config.store.get("timeout", 30)))
    return LocalStore(_paths(config).db, redactor=redactor, auto_migrate=_auto_migrate(config))


def open_sealed_store(config: "Config") -> SealedStore:
    if config.store.get("url"):
        from .http import HttpSealedStore

        return HttpSealedStore(config.store["url"], _key(config, "sealed_key_env"),
                               float(config.store.get("timeout", 30)))
    return LocalStore(_paths(config).sealed_db, auto_migrate=_auto_migrate(config))


def open_scratch_store(path: str, redactor: Redactor | None = None) -> RecordStore:
    """A private, throwaway local store, e.g. the optimizer worker's results file.
    Always local: its contents reach the real store through ``import_records``."""
    return LocalStore(path, redactor=redactor)
