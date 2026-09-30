"""Connection settings, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    """Raised when required settings are missing or invalid."""


READ_ONLY = "read_only"
OPERATOR = "operator"
MODES = (READ_ONLY, OPERATOR)
ALL_CLUSTERS = "*"
DEFAULT_AUDIT_LOG = Path.home() / ".olvm-mcp" / "audit.jsonl"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def mode_from_env(env: Mapping[str, str] | None = None) -> str:
    """The server mode from OLVM_MODE. Defaults to read_only."""
    env = os.environ if env is None else env
    mode = (env.get("OLVM_MODE") or READ_ONLY).strip().lower()
    if mode not in MODES:
        raise ConfigError(f"OLVM_MODE must be one of {', '.join(MODES)} (got {mode!r})")
    return mode


def _clusters(raw: str | None) -> tuple[str, ...]:
    return tuple(c.strip() for c in (raw or "").split(",") if c.strip())


def _normalize_url(raw: str) -> str:
    """Return the engine base URL, e.g. https://engine.example.com/ovirt-engine.

    Accepts the base URL or the API URL (…/ovirt-engine/api), with or without a
    trailing slash.
    """
    url = raw.strip().rstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(
            f"OLVM_URL must be an https URL such as "
            f"https://engine.example.com/ovirt-engine (got {raw!r})"
        )
    if not parsed.path:
        url += "/ovirt-engine"
    return url


@dataclass(frozen=True)
class Settings:
    url: str
    username: str
    password: str = field(repr=False)
    ca_file: str | None = None
    insecure: bool = False
    timeout: float = 30.0
    mode: str = READ_ONLY
    allowed_clusters: tuple[str, ...] = ()
    audit_log: Path = DEFAULT_AUDIT_LOG

    @property
    def verify(self) -> str | bool:
        """Value for httpx's `verify`: CA file path, system trust, or disabled."""
        if self.insecure:
            return False
        return self.ca_file or True

    def cluster_allowed(self, cluster: str | None) -> bool:
        """Whether write actions may touch objects in this cluster."""
        if ALL_CLUSTERS in self.allowed_clusters:
            return True
        return cluster is not None and cluster.lower() in {c.lower() for c in self.allowed_clusters}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        missing = [k for k in ("OLVM_URL", "OLVM_USERNAME") if not env.get(k)]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        password = env.get("OLVM_PASSWORD")
        password_file = env.get("OLVM_PASSWORD_FILE")
        if not password and password_file:
            try:
                password = Path(password_file).read_text(encoding="utf-8").strip()
            except OSError as e:
                raise ConfigError(f"Cannot read OLVM_PASSWORD_FILE {password_file!r}: {e}") from e
        if not password:
            raise ConfigError("Set OLVM_PASSWORD or OLVM_PASSWORD_FILE")

        ca_file = env.get("OLVM_CA_FILE") or None
        if ca_file and not Path(ca_file).is_file():
            raise ConfigError(f"OLVM_CA_FILE not found: {ca_file!r}")

        try:
            timeout = float(env.get("OLVM_TIMEOUT", "30"))
        except ValueError as e:
            raise ConfigError("OLVM_TIMEOUT must be a number of seconds") from e

        mode = mode_from_env(env)
        allowed_clusters = _clusters(env.get("OLVM_ALLOWED_CLUSTERS"))
        if mode == OPERATOR and not allowed_clusters:
            raise ConfigError(
                "OLVM_MODE=operator needs OLVM_ALLOWED_CLUSTERS: a comma-separated list of "
                "cluster names that write actions may touch, or * for all clusters"
            )

        return cls(
            url=_normalize_url(env["OLVM_URL"]),
            username=env["OLVM_USERNAME"],
            password=password,
            ca_file=ca_file,
            insecure=_truthy(env.get("OLVM_INSECURE")),
            timeout=timeout,
            mode=mode,
            allowed_clusters=allowed_clusters,
            audit_log=Path(env["OLVM_AUDIT_LOG"]) if env.get("OLVM_AUDIT_LOG") else DEFAULT_AUDIT_LOG,
        )
