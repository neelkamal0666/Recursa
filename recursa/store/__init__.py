"""Storage for Recursa.

Code outside this package depends on the ``RecordStore`` and ``SealedStore``
protocols and gets an instance from a factory, never from a backend class:

    store = open_store(config)            # runs, versions, candidates, audit, ...
    sealed = open_sealed_store(config)    # gate ledger, gate checkpoints, packets

Today the only backend is ``LocalStore`` (SQLite). Setting ``store.url`` will select
Recursa Server once it exists (planned for 0.3); until then it is rejected when the
config is loaded.

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


def open_store(config: "Config", redactor: Redactor | None = None) -> RecordStore:
    return LocalStore(_paths(config).db, redactor=redactor, auto_migrate=_auto_migrate(config))


def open_sealed_store(config: "Config") -> SealedStore:
    return LocalStore(_paths(config).sealed_db, auto_migrate=_auto_migrate(config))


def open_scratch_store(path: str, redactor: Redactor | None = None) -> RecordStore:
    """A private, throwaway local store, e.g. the optimizer worker's results file.
    Always local: its contents reach the real store through ``import_records``."""
    return LocalStore(path, redactor=redactor)
