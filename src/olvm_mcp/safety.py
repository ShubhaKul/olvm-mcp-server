"""Guard rails for write actions: correlation ids, the audit log and confirmation tokens."""

from __future__ import annotations

import getpass
import json
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

CONFIRM_TTL_SECONDS = 300


class AuditError(Exception):
    """The audit log could not be written. Write actions must not run without it."""


def new_correlation_id() -> str:
    """A short id sent to the engine with each write, so its events can be traced back."""
    return f"olvm-mcp-{uuid.uuid4().hex[:12]}"


def current_client() -> str:
    """Who is behind the current request, for the audit log.

    Over HTTP, the authenticated client: the name of its bearer token (later, the
    OAuth subject). Over stdio there is no authentication, so it is the operating
    system user running the server, as "local:<user>".
    """
    token = get_access_token()
    if token is not None:
        return token.subject or token.client_id
    try:
        return f"local:{getpass.getuser()}"
    except Exception:  # no user name available, e.g. an unnamed uid in a container
        return "local"


class AuditLog:
    """Append-only JSON Lines file: one record per write request and one per outcome.

    "user" is the engine account the server acts as; "client" is who asked
    (see current_client), so actions through a shared account stay attributable.
    """

    def __init__(self, path: Path, user: str):
        self._path = path
        self._user = user

    @property
    def path(self) -> Path:
        return self._path

    def record(self, **fields: Any) -> None:
        entry = {"time": datetime.now(tz=UTC).isoformat(timespec="seconds"), "user": self._user,
                 "client": current_client(), **fields}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=False) + "\n")
        except OSError as e:
            raise AuditError(f"Cannot write the audit log {str(self._path)!r}: {e}") from e


class ConfirmationError(Exception):
    """A confirmation token was missing, unknown, used, expired or for a different request."""


@dataclass(frozen=True)
class _Pending:
    action: str
    object_id: str
    params: dict[str, Any]
    fingerprint: str
    expires: float


class ConfirmationTokens:
    """One-time tokens that tie a destructive action to the preview the user saw.

    A preview issues a token for exactly one action, object and set of arguments,
    plus a fingerprint of the object's state. Redeeming it succeeds only once,
    within the time limit, for the same request, and while the state is unchanged.
    """

    def __init__(self, ttl_seconds: float = CONFIRM_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._pending: dict[str, _Pending] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def issue(self, action: str, object_id: str, params: dict[str, Any], fingerprint: str) -> str:
        now = self._clock()
        self._pending = {t: p for t, p in self._pending.items() if p.expires > now}
        token = f"confirm-{secrets.token_urlsafe(12)}"
        self._pending[token] = _Pending(action, object_id, dict(params), fingerprint, now + self._ttl)
        return token

    def redeem(self, token: str, action: str, object_id: str, params: dict[str, Any],
               fingerprint: str) -> None:
        # Popped first: a token is spent by any attempt, so a wrong guess can't be retried.
        pending = self._pending.pop(token.strip(), None)
        if pending is None:
            raise ConfirmationError("The confirmation token is unknown or was already used")
        if self._clock() > pending.expires:
            raise ConfirmationError(f"The confirmation token expired (tokens last {self._ttl:.0f} seconds)")
        if (pending.action, pending.object_id, pending.params) != (action, object_id, dict(params)):
            raise ConfirmationError("The confirmation token was issued for a different request")
        if pending.fingerprint != fingerprint:
            raise ConfirmationError(
                f"The target changed since the preview ({pending.fingerprint} -> {fingerprint})")
