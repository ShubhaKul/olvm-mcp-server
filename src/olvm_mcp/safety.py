"""Guard rails for write actions: correlation ids and the audit log."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditError(Exception):
    """The audit log could not be written. Write actions must not run without it."""


def new_correlation_id() -> str:
    """A short id sent to the engine with each write, so its events can be traced back."""
    return f"olvm-mcp-{uuid.uuid4().hex[:12]}"


class AuditLog:
    """Append-only JSON Lines file: one record per write request and one per outcome."""

    def __init__(self, path: Path, user: str):
        self._path = path
        self._user = user

    @property
    def path(self) -> Path:
        return self._path

    def record(self, **fields: Any) -> None:
        entry = {"time": datetime.now(tz=UTC).isoformat(timespec="seconds"), "user": self._user, **fields}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=False) + "\n")
        except OSError as e:
            raise AuditError(f"Cannot write the audit log {str(self._path)!r}: {e}") from e
