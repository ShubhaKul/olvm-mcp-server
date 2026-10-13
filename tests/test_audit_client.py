import json

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from olvm_mcp import actions, safety, server
from olvm_mcp.remote import MCP_PATH, HttpSettings, hash_token

from .conftest import VM_DOWN, VM_UP
from .test_actions import audit_records, by_name

TOKEN = "a" * 43
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture
def as_client():
    """Run code as if an authenticated HTTP request from the named client were in progress."""
    tokens = []

    def set_client(name):
        token = AccessToken(token="t", client_id=name, subject=name, scopes=[])
        tokens.append(auth_context_var.set(AuthenticatedUser(token)))

    yield set_client
    for t in reversed(tokens):
        auth_context_var.reset(t)


# -- current_client -------------------------------------------------------------

def test_stdio_client_is_the_local_os_user(monkeypatch):
    monkeypatch.setattr(safety.getpass, "getuser", lambda: "shubh")
    assert safety.current_client() == "local:shubh"


def test_stdio_client_without_a_user_name(monkeypatch):
    def no_user():
        raise KeyError("uid not found")
    monkeypatch.setattr(safety.getpass, "getuser", no_user)
    assert safety.current_client() == "local"


def test_http_client_is_the_token_name(as_client):
    as_client("shubha-laptop")
    assert safety.current_client() == "shubha-laptop"


def test_audit_record_names_the_client(tmp_path, as_client):
    log = safety.AuditLog(tmp_path / "audit.jsonl", "mcp-operator@internalsso")
    as_client("alice")
    log.record(action="start_vm", outcome="requested")
    [entry] = audit_records(tmp_path / "audit.jsonl")
    assert entry["user"] == "mcp-operator@internalsso" and entry["client"] == "alice"
    assert list(entry)[:4] == ["time", "user", "client", "action"]


# -- end to end: a write action through the authenticated HTTP server -------------

def _rpc(client, payload, session=None):
    headers = {**HEADERS, "Authorization": f"Bearer {TOKEN}"}
    if session:
        headers["Mcp-Session-Id"] = session
    return client.post(MCP_PATH, json=payload, headers=headers)


def _result(response):
    """The JSON-RPC message from a JSON or server-sent-events response."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])


def test_http_write_is_audited_with_the_token_name(operator, engine, audit_path, tmp_path):
    tokens = tmp_path / "tokens"
    tokens.write_text(f"alice {hash_token(TOKEN)}\n", encoding="utf-8")
    http = HttpSettings.from_env({"OLVM_AUTH_TOKENS_FILE": str(tokens)})
    srv = server.create_server(http)
    actions.register(srv)
    app = srv.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(allowed_hosts=list(http.allowed_hosts),
                                                     allowed_origins=[http.public_url]))

    by_name(engine, VM_DOWN)
    engine.post(f"/api/vms/{VM_DOWN['id']}/start").respond(200, json={})
    engine.get(f"/api/vms/{VM_DOWN['id']}").respond(200, json={**VM_UP, "id": VM_DOWN["id"], "name": "vm-2"})

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        init = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "1"}}})
        assert init.status_code == 200
        session = init.headers.get("mcp-session-id")
        _rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        call = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "start_vm", "arguments": {"vm_name_or_id": "vm-2"}}}, session)

    result = _result(call)["result"]
    assert not result.get("isError"), result
    records = audit_records(audit_path)
    assert [r["outcome"] for r in records] == ["requested", "done"]
    assert {r["client"] for r in records} == {"alice"}
