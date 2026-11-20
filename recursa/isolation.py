"""Read isolation for the optimizer process.

The optimizer runs in a child process (``python -m recursa.worker``) that:

1. never receives the gate key, the store path or any sealed data on stdin;
2. installs a CPython audit hook *before* importing any user code. Audit hooks
   cannot be removed once installed. The hook refuses to open, list, link, copy,
   chmod or connect (sqlite) to anything under the protected paths, and refuses
   to spawn subprocesses or load native libraries via ctypes (either would escape
   the hook).

Sealed sets are also encrypted at rest, so a raw read without the key yields only
ciphertext. For a stronger boundary in production, run the gate under a separate
OS user that alone owns ``sealed/`` and ``keys/`` (mode 0700).
"""
from __future__ import annotations

import os
import sys
import threading

_PATH_EVENTS = {
    "open", "os.listdir", "os.scandir", "os.chmod", "os.chown", "os.remove", "os.rename", "os.link",
    "os.symlink", "os.truncate", "os.utime", "shutil.copyfile", "shutil.copytree", "shutil.rmtree",
    "sqlite3.connect", "glob.glob", "os.chdir", "os.mkdir",
}
_FORBIDDEN_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    "ctypes.dlopen", "ctypes.dlsym", "sys.addaudithook",
}

_installed = False
_guard = threading.local()


def _norm(p: object) -> str | None:
    if isinstance(p, int):
        return None  # already-open fd; opening it required a path event first
    if isinstance(p, bytes):
        p = p.decode(errors="replace")
    if not isinstance(p, (str, os.PathLike)):
        return None
    p = os.fspath(p)
    if p.startswith("file:"):  # sqlite URI
        p = p[5:].split("?", 1)[0]
    return os.path.realpath(p)


def install(protected: list[str]) -> None:
    """Install the (irremovable) audit hook. Call once, before importing user code."""
    global _installed
    if _installed:
        raise RuntimeError("isolation already installed")
    roots = [os.path.realpath(p) for p in protected]
    try:  # load ctypes' own libraries now; later dlopen/dlsym calls are refused
        import ctypes  # noqa: F401
    except ImportError:
        pass

    def blocked(path: str) -> bool:
        return any(path == r or path.startswith(r + os.sep) for r in roots)

    def hook(event: str, args: tuple) -> None:
        if getattr(_guard, "busy", False):
            return
        if event in _FORBIDDEN_EVENTS:
            raise PermissionError(f"recursa isolation: '{event}' is not allowed in the optimizer process")
        if event in _PATH_EVENTS:
            _guard.busy = True
            try:
                for a in args[:2]:
                    p = _norm(a)
                    if p and blocked(p):
                        raise PermissionError(f"recursa isolation: optimizer may not access {p}")
            finally:
                _guard.busy = False

    sys.addaudithook(hook)
    _installed = True
