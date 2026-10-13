"""Remote access: the Streamable HTTP transport and bearer-token authentication.

Clients send `Authorization: Bearer <token>`. The server keeps only SHA-256
hashes of the tokens, one named client per line of OLVM_AUTH_TOKENS_FILE:

    # name   sha256 of the token
    alice    sha256:3f5a...
    codex    sha256:9b1c...

`olvm-mcp-token <name>` creates a token and prints both the token (for the
client) and the line for the file.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken

from .config import ConfigError

STDIO, HTTP = "stdio", "http"
TRANSPORTS = (STDIO, HTTP)
MCP_PATH = "/mcp"
_NAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def transport_from_env(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    transport = (env.get("OLVM_TRANSPORT") or STDIO).strip().lower()
    if transport not in TRANSPORTS:
        raise ConfigError(f"OLVM_TRANSPORT must be one of {', '.join(TRANSPORTS)} (got {transport!r})")
    return transport


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_token_hashes(path: Path) -> dict[str, str]:
    """Read the tokens file and return {hash: client name}."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise ConfigError(f"Cannot read OLVM_AUTH_TOKENS_FILE {str(path)!r}: {e}") from e
    hashes: dict[str, str] = {}
    names: set[str] = set()
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not _NAME.match(parts[0]) or not _HASH.match(parts[1]):
            raise ConfigError(f"{path}:{number}: expected '<name> sha256:<64 hex digits>' "
                              f"(create entries with olvm-mcp-token <name>)")
        name, digest = parts
        if name in names or digest in hashes:
            raise ConfigError(f"{path}:{number}: duplicate name or token")
        names.add(name)
        hashes[digest] = name
    if not hashes:
        raise ConfigError(f"OLVM_AUTH_TOKENS_FILE {str(path)!r} has no tokens")
    return hashes


class StaticTokenVerifier:
    """Accepts bearer tokens whose SHA-256 hash is listed in the tokens file."""

    def __init__(self, hashes: Mapping[str, str], resource: str | None = None):
        self._hashes = dict(hashes)
        self._resource = resource  # these tokens are only ever for this server

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hash_token(token)
        for known, name in self._hashes.items():
            if hmac.compare_digest(known, digest):
                return AccessToken(token=token, client_id=name, subject=name, scopes=[],
                                   resource=self._resource)
        return None


@dataclass(frozen=True)
class HttpSettings:
    host: str
    port: int
    public_url: str
    allowed_hosts: tuple[str, ...]
    token_hashes: dict[str, str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HttpSettings:
        env = os.environ if env is None else env
        host = (env.get("OLVM_HTTP_HOST") or "127.0.0.1").strip()
        try:
            port = int(env.get("OLVM_HTTP_PORT") or 8000)
        except ValueError as e:
            raise ConfigError("OLVM_HTTP_PORT must be a number") from e

        public_url = (env.get("OLVM_PUBLIC_URL") or f"http://127.0.0.1:{port}").strip().rstrip("/")
        parsed = urlparse(public_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
            raise ConfigError("OLVM_PUBLIC_URL must be the server's base URL, e.g. https://mcp.example.com")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ConfigError("OLVM_PUBLIC_URL must use https unless it is localhost; put a TLS "
                              "proxy in front of the server")

        tokens_file = env.get("OLVM_AUTH_TOKENS_FILE")
        if not tokens_file:
            raise ConfigError("OLVM_TRANSPORT=http needs OLVM_AUTH_TOKENS_FILE; the server never "
                              "runs over HTTP without authentication")

        # Requests must name this server in their Host header (DNS rebinding protection).
        allowed = {parsed.netloc, f"127.0.0.1:{port}", f"localhost:{port}"}
        allowed.update(h.strip() for h in (env.get("OLVM_HTTP_ALLOWED_HOSTS") or "").split(",") if h.strip())
        return cls(host=host, port=port, public_url=public_url, allowed_hosts=tuple(sorted(allowed)),
                   token_hashes=load_token_hashes(Path(tokens_file)))

    @property
    def resource_url(self) -> str:
        return self.public_url + MCP_PATH


def new_token_main() -> None:
    """`olvm-mcp-token <name>`: print a new token and its tokens-file line."""
    if len(sys.argv) != 2 or not _NAME.match(sys.argv[1]):
        sys.exit("usage: olvm-mcp-token <name>   (letters, digits, . _ @ -)")
    name = sys.argv[1]
    token = secrets.token_urlsafe(32)
    print(f"Token for {name} (give it to the client; it is shown only once):\n  {token}\n")
    print(f"Line for OLVM_AUTH_TOKENS_FILE:\n  {name} {hash_token(token)}")
