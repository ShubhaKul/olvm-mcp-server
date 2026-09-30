import pytest
import respx

from olvm_mcp import server
from olvm_mcp.client import OlvmClient
from olvm_mcp.config import Settings

BASE = "https://engine.test/ovirt-engine"

CLUSTERS = {"cluster": [{"id": "c1", "name": "Default"}]}
HOSTS = {
    "host": [{
        "id": "h1",
        "name": "kvm01.example.test",
        "address": "192.0.2.11",
        "status": "up",
        "cluster": {"id": "c1", "href": "/ovirt-engine/api/clusters/c1"},
        "cpu": {"name": "AMD EPYC", "topology": {"sockets": "1", "cores": "2", "threads": "2"}},
        "memory": str(16 * 1024**3),
        "max_scheduling_memory": str(12 * 1024**3),
        "summary": {"active": "1", "total": "2"},
        "os": {"type": "OL", "version": {"full_version": "8.10"}},
        "version": {"full_version": "vdsm-4.50.5"},
        "spm": {"status": "spm"},
    }]
}
VM_UP = {
    "id": "11111111-2222-3333-4444-555555555555",
    "name": "vm-test",
    "status": "up",
    "cluster": {"id": "c1"},
    "host": {"id": "h1"},
    "cpu": {"topology": {"sockets": "2", "cores": "1", "threads": "1"}},
    "memory": str(2 * 1024**3),
    "memory_policy": {"guaranteed": str(1024**3)},
    "os": {"type": "other_linux"},
    "high_availability": {"enabled": "false"},
    "stateless": "false",
    "creation_time": 1790000000000,
}
VM_DOWN = {**VM_UP, "id": "66666666-7777-8888-9999-000000000000", "name": "vm-2",
           "status": "down", "host": None}


@pytest.fixture
def settings():
    return Settings(url=BASE, username="mcp-reader@ovirt@internalsso", password="secret")


@pytest.fixture
def engine():
    """A mocked engine with a working token endpoint."""
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/sso/oauth/token", name="token").respond(200, json={"access_token": "tok-1"})
        mock.post("/services/sso-logout", name="logout").respond(200, json={})
        mock.get("/api/clusters", name="clusters").respond(200, json=CLUSTERS)
        mock.get("/api/hosts").respond(200, json=HOSTS)
        yield mock


@pytest.fixture
def client(settings, engine):
    c = OlvmClient(settings)
    yield c
    c.close()


@pytest.fixture
def tool_client(client, monkeypatch):
    """Point the MCP tools at the mocked engine."""
    monkeypatch.setattr(server, "_client", client)
    return client

