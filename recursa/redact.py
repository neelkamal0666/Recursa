"""PII redaction that runs before anything is persisted. Fails closed."""
from __future__ import annotations

import re
from typing import Any, Iterable

DEFAULT_PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "card": r"\b(?:\d[ -]?){13,19}\b",
    "us_ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "phone": r"(?<!\w)\+?\d{1,3}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\w)",
}


class RedactionError(RuntimeError):
    pass


class Redactor:
    def __init__(self, patterns: dict[str, str] | None = None, secrets: Iterable[str] = ()):
        self.patterns = {k: re.compile(v) for k, v in (patterns or DEFAULT_PATTERNS).items()}
        # Known secret values are masked by exact match.
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def text(self, s: str) -> str:
        for secret in self.secrets:
            s = s.replace(secret, "[SECRET]")
        for name, rx in self.patterns.items():
            s = rx.sub(f"[{name.upper()}]", s)
        return s

    def __call__(self, obj: Any) -> Any:
        try:
            return self._walk(obj)
        except Exception as e:  # noqa: BLE001
            raise RedactionError(str(e)) from e

    def _walk(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self._walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._walk(v) for v in obj]
        return obj
