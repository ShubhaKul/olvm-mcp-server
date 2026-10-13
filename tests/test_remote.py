import asyncio

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from olvm_mcp import server
from olvm_mcp.config import ConfigError
from olvm_mcp.remote import (
    MCP_PATH,
    HttpSettings,
    StaticTokenVerifier,
    hash_token,
    load_token_hashes,
    transport_from_env,
)

TOKEN = "t" * 43
OTHER = "o" * 43
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "1"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def tokens_file(tmp_path):
    f = tmp_path / "tokens"
    f.write_text(f"# clients\nalice {hash_token(TOKEN)}\n\ncodex {hash_token(OTHER)}\n", encoding="utf-8")
    return f


def env(tokens_file, **extra):
    return {"OLVM_AUTH_TOKENS_FILE": str(tokens_file), **extra}


# -- settings -----------------------------------------------------------------

def test_transport_defaults_to_stdio():
    assert transport_from_env({}) == "stdio"
    assert transport_from_env({"OLVM_TRANSPORT": " HTTP "}) == "http"
    with pytest.raises(ConfigError, match="OLVM_TRANSPORT"):
        transport_from_env({"OLVM_TRANSPORT": "sse"})


def test_http_needs_a_tokens_file():
    with pytest.raises(ConfigError, match="never runs over HTTP without authentication"):
        HttpSettings.from_env({})


def test_http_defaults(tokens_file):
    s = HttpSettings.from_env(env(tokens_file))
    assert (s.host, s.port, s.public_url) == ("127.0.0.1", 8000, "http://127.0.0.1:8000")
    assert s.resource_url == "http://127.0.0.1:8000/mcp"
    assert set(s.token_hashes.values()) == {"alice", "codex"}


def test_public_url_sets_allowed_hosts(tokens_file):
    s = HttpSettings.from_env(env(tokens_file, OLVM_PUBLIC_URL="https://mcp.example.test/",
                                  OLVM_HTTP_PORT="9000", OLVM_HTTP_ALLOWED_HOSTS="10.1.2.3:9000"))
    assert s.public_url == "https://mcp.example.test"
    assert set(s.allowed_hosts) == {"mcp.example.test", "127.0.0.1:9000", "localhost:9000", "10.1.2.3:9000"}


@pytest.mark.parametrize("url, message", [
    ("http://mcp.example.test", "must use https"),
    ("https://mcp.example.test/mcp", "base URL"),
    ("mcp.example.test", "base URL"),
])
def test_bad_public_url(tokens_file, url, message):
    with pytest.raises(ConfigError, match=message):
        HttpSettings.from_env(env(tokens_file, OLVM_PUBLIC_URL=url))


@pytest.mark.parametrize("content, message", [
    ("alice not-a-hash\n", "expected '<name> sha256"),
    (f"alice {hash_token(TOKEN)} extra\n", "expected"),
    (f"bad name {hash_token(TOKEN)}\n", "expected"),
    (f"alice {hash_token(TOKEN)}\nalice {hash_token(OTHER)}\n", "duplicate"),
    (f"alice {hash_token(TOKEN)}\nbob {hash_token(TOKEN)}\n", "duplicate"),
    ("# only comments\n", "has no tokens"),
])
def test_bad_tokens_file(tmp_path, content, message):
    f = tmp_path / "tokens"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_token_hashes(f)


def test_missing_tokens_file(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_token_hashes(tmp_path / "nope")


def test_verifier_names_the_client(tokens_file):
    verifier = StaticTokenVerifier(load_token_hashes(tokens_file))
    alice = asyncio.run(verifier.verify_token(TOKEN))
    assert alice.client_id == "alice" and alice.subject == "alice"
    assert asyncio.run(verifier.verify_token("wrong")) is None
    assert asyncio.run(verifier.verify_token(hash_token(TOKEN))) is None  # the hash itself isn't a token


def test_verifier_binds_tokens_to_this_server(tokens_file):
    verifier = StaticTokenVerifier(load_token_hashes(tokens_file), resource="https://mcp.example.test/mcp")
    assert asyncio.run(verifier.verify_token(TOKEN)).resource == "https://mcp.example.test/mcp"


# -- the HTTP app ---------------------------------------------------------------

@pytest.fixture
def app_client(tokens_file):
    http = HttpSettings.from_env(env(tokens_file))
    app = server.create_server(http).streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(allowed_hosts=list(http.allowed_hosts),
                                                     allowed_origins=[http.public_url]))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


def test_request_without_token_is_rejected(app_client):
    r = app_client.post(MCP_PATH, json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].lower().startswith("bearer")


def test_request_with_wrong_token_is_rejected(app_client):
    r = app_client.post(MCP_PATH, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_request_with_valid_token_is_served(app_client):
    r = app_client.post(MCP_PATH, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert '"serverInfo"' in r.text and '"olvm"' in r.text


def test_unexpected_host_header_is_rejected(app_client):
    r = app_client.post(MCP_PATH, json=INITIALIZE,
                        headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}", "Host": "evil.example"})
    assert r.status_code in (403, 421)


def test_http_server_offers_the_read_tools(tokens_file):
    http = HttpSettings.from_env(env(tokens_file))
    names = {t.name for t in asyncio.run(server.create_server(http).list_tools())}
    assert names == {t.__name__ for t in server.READ_TOOLS}


# -- main() -------------------------------------------------------------------

def test_main_refuses_http_without_tokens(monkeypatch):
    monkeypatch.setenv("OLVM_TRANSPORT", "http")
    monkeypatch.delenv("OLVM_AUTH_TOKENS_FILE", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 2


def test_main_serves_http_with_operator_tools(monkeypatch, tokens_file):
    captured = {}

    def fake_run(self, transport="stdio", **kwargs):
        captured.update(server=self, transport=transport, **kwargs)

    monkeypatch.setattr(server.MCPServer, "run", fake_run)
    for k, v in {"OLVM_TRANSPORT": "http", "OLVM_AUTH_TOKENS_FILE": str(tokens_file),
                 "OLVM_HTTP_PORT": "9123", "OLVM_MODE": "operator", "OLVM_ALLOW_DESTRUCTIVE": ""}.items():
        monkeypatch.setenv(k, v)
    server.main()
    assert captured["transport"] == "streamable-http"
    assert (captured["host"], captured["port"], captured["streamable_http_path"]) == ("127.0.0.1", 9123, "/mcp")
    assert captured["server"] is not server.mcp  # a separate, authenticated server
    names = {t.name for t in asyncio.run(captured["server"].list_tools())}
    assert "start_vm" in names and "remove_vm" not in names
