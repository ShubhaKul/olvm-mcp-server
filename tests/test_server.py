import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from olvm_mcp import server

from .conftest import VM_DOWN, VM_UP


def test_tools_are_registered_read_only():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"list_vms", "get_vm", "list_hosts"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


def test_list_vms_summarizes(tool_client, engine):
    engine.get("/api/vms").respond(200, json={"vm": [VM_UP, VM_DOWN]})
    result = server.list_vms()
    assert result["count"] == 2 and result["truncated"] is False
    assert result["vms"][0] == {
        "id": VM_UP["id"], "name": "vm-test", "status": "up", "cluster": "Default",
        "host": "kvm01.example.test", "cpus": 2, "memory_gib": 2.0, "os": "other_linux",
    }
    assert "host" not in result["vms"][1]


def test_list_vms_reports_truncation(tool_client, engine):
    route = engine.get("/api/vms").respond(200, json={"vm": [VM_UP, VM_DOWN]})
    result = server.list_vms(search="status=up", max_results=1)
    assert result["count"] == 1 and result["truncated"] is True
    assert route.calls.last.request.url.params["max"] == "2"


def test_max_results_is_clamped(tool_client, engine):
    route = engine.get("/api/vms").respond(200, json={})
    server.list_vms(max_results=10_000)
    assert route.calls.last.request.url.params["max"] == str(server.MAX_RESULTS_LIMIT + 1)


def test_get_vm_by_name_includes_disks_and_nics(tool_client, engine):
    engine.get("/api/vms", params={"search": 'name="vm-test"'}).respond(200, json={"vm": [VM_UP]})
    engine.get(f"/api/vms/{VM_UP['id']}/diskattachments").respond(200, json={"disk_attachment": [{
        "interface": "virtio_scsi", "bootable": "true", "active": "true",
        "disk": {"alias": "vm-test_Disk1", "provisioned_size": str(10 * 1024**3),
                 "format": "cow", "status": "ok"},
    }]})
    engine.get(f"/api/vms/{VM_UP['id']}/nics").respond(200, json={"nic": [{
        "name": "nic1", "interface": "virtio", "mac": {"address": "56:6f:00:00:00:01"},
        "plugged": "true", "linked": "true",
    }]})
    vm = server.get_vm("vm-test")
    assert vm["name"] == "vm-test"
    assert vm["guaranteed_memory_gib"] == 1.0
    assert vm["high_availability"] is False
    assert vm["created"].startswith("2026-")
    assert vm["disks"] == [{"name": "vm-test_Disk1", "size_gib": 10.0, "format": "cow", "status": "ok",
                            "interface": "virtio_scsi", "bootable": True, "active": True}]
    assert vm["nics"][0]["mac"] == "56:6f:00:00:00:01"


def test_get_vm_by_id(tool_client, engine):
    engine.get(f"/api/vms/{VM_UP['id']}").respond(200, json=VM_UP)
    engine.get(f"/api/vms/{VM_UP['id']}/diskattachments").respond(200, json={})
    engine.get(f"/api/vms/{VM_UP['id']}/nics").respond(200, json={})
    assert server.get_vm(VM_UP["id"])["name"] == "vm-test"


def test_get_vm_not_found(tool_client, engine):
    engine.get("/api/vms").respond(200, json={})
    with pytest.raises(ToolError, match="No VM named 'missing'"):
        server.get_vm("missing")


def test_get_vm_ignores_partial_name_matches(tool_client, engine):
    engine.get("/api/vms").respond(200, json={"vm": [{**VM_UP, "name": "vm-test-2"}]})
    with pytest.raises(ToolError, match="No VM named"):
        server.get_vm("vm-test")


def test_get_vm_ambiguous_name(tool_client, engine):
    engine.get("/api/vms").respond(200, json={"vm": [VM_UP, {**VM_DOWN, "name": "vm-test"}]})
    with pytest.raises(ToolError, match="More than one VM"):
        server.get_vm("vm-test")


def test_list_hosts_summarizes(tool_client, engine):
    result = server.list_hosts()
    assert result["hosts"] == [{
        "id": "h1", "name": "kvm01.example.test", "address": "10.0.0.141", "status": "up",
        "cluster": "Default", "cpu_model": "AMD EPYC", "cpus": 4, "memory_gib": 16.0,
        "schedulable_memory_gib": 12.0, "vms_running": 1, "os": "OL 8.10",
        "vdsm_version": "vdsm-4.50.5", "spm": "spm",
    }]


def test_engine_errors_become_tool_errors(tool_client, engine):
    engine.get("/api/vms").respond(400, json={"detail": "bad search"})
    with pytest.raises(ToolError, match="bad search"):
        server.list_vms(search="((")


def test_missing_config_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    for var in ("OLVM_URL", "OLVM_USERNAME", "OLVM_PASSWORD", "OLVM_PASSWORD_FILE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ToolError, match="not configured"):
        server.list_hosts()
