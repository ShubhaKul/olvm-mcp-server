import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from olvm_mcp import server

from .conftest import VM_DOWN, VM_UP


def test_tools_are_registered_read_only():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"list_vms", "get_vm", "list_hosts", "list_events", "list_snapshots",
                          "list_storage_domains", "get_job_status"}
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


def test_list_events_summarizes(tool_client, engine):
    route = engine.get("/api/events").respond(200, json={"event": [{
        "id": "812", "time": 1790000000000, "severity": "error", "code": "119",
        "description": "VM vm-test is down with error.", "cluster": {"id": "c1"},
        "host": {"id": "h1"}, "vm": {"id": VM_UP["id"]}, "correlation_id": "abc-123",
        "origin": "oVirt", "custom_id": "-1", "flood_rate": "30",
    }]})
    result = server.list_events(search="severity=error", max_results=10)
    assert route.calls.last.request.url.params["search"] == "severity=error"
    assert result["count"] == 1 and result["truncated"] is False
    assert result["events"][0] == {
        "id": "812", "time": "2026-09-21T14:13:20+00:00", "severity": "error", "code": 119,
        "description": "VM vm-test is down with error.", "cluster": "Default",
        "host": "kvm01.example.test", "vm_id": VM_UP["id"], "correlation_id": "abc-123",
    }


def test_list_events_redacts_session_ids(tool_client, engine):
    engine.get("/api/events").respond(200, json={"event": [{
        "id": "3155", "severity": "normal", "code": "30",
        "description": "User mcp-reader connecting from '::1' using session 'Mla0NnuZ3SJ0+hk==' logged in.",
    }]})
    description = server.list_events()["events"][0]["description"]
    assert description == "User mcp-reader connecting from '::1' using session '<redacted>' logged in."


def test_list_snapshots_skips_active_snapshot(tool_client, engine):
    engine.get("/api/vms").respond(200, json={"vm": [VM_UP]})
    engine.get(f"/api/vms/{VM_UP['id']}/snapshots").respond(200, json={"snapshot": [
        {"id": "s0", "description": "Active VM", "snapshot_type": "active", "snapshot_status": "ok"},
        {"id": "s1", "description": "before upgrade", "snapshot_type": "regular",
         "snapshot_status": "ok", "date": 1790000000000, "persist_memorystate": "false"},
    ]})
    result = server.list_snapshots("vm-test")
    assert result == {"vm": "vm-test", "count": 1, "snapshots": [{
        "id": "s1", "description": "before upgrade", "date": "2026-09-21T14:13:20+00:00",
        "status": "ok", "type": "regular", "includes_memory": False,
    }]}


def test_list_snapshots_unknown_vm(tool_client, engine):
    engine.get("/api/vms").respond(200, json={})
    with pytest.raises(ToolError, match="No VM named"):
        server.list_snapshots("missing")


def test_list_storage_domains_reports_capacity(tool_client, engine):
    engine.get("/api/storagedomains").respond(200, json={"storage_domain": [
        {"id": "sd1", "name": "data1", "type": "data", "storage": {"type": "nfs"},
         "external_status": "ok", "master": "true", "available": str(10 * 1024**3),
         "used": str(90 * 1024**3), "committed": str(120 * 1024**3),
         "warning_low_space_indicator": "15"},
        {"id": "sd2", "name": "iso", "type": "iso", "status": "unattached"},
    ]})
    result = server.list_storage_domains()
    assert result["storage_domains"][0] == {
        "id": "sd1", "name": "data1", "type": "data", "storage_type": "nfs",
        "external_status": "ok", "master": True, "total_gib": 100.0, "used_gib": 90.0,
        "available_gib": 10.0, "used_percent": 90.0, "committed_gib": 120.0, "low_space": True,
    }
    assert result["storage_domains"][1] == {"id": "sd2", "name": "iso", "type": "iso",
                                            "status": "unattached"}


JOB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_get_job_status_with_steps(tool_client, engine):
    engine.get(f"/api/jobs/{JOB_ID}").respond(200, json={
        "id": JOB_ID, "description": "Migrating VM vm-test", "status": "started",
        "start_time": 1790000000000, "last_updated": 1790000060000})
    engine.get(f"/api/jobs/{JOB_ID}/steps").respond(200, json={"step": [
        {"number": "1", "description": "Executing", "type": "executing", "status": "started",
         "progress": "40", "start_time": 1790000001000},
        {"number": "0", "description": "Validating", "type": "validating", "status": "finished",
         "start_time": 1790000000000, "end_time": 1790000001000},
    ]})
    job = server.get_job_status(JOB_ID)
    assert job["status"] == "started" and "ended" not in job
    assert [s["type"] for s in job["steps"]] == ["validating", "executing"]
    assert job["steps"][1]["progress"] == 40


def test_get_job_status_lists_newest_first(tool_client, engine):
    engine.get("/api/jobs").respond(200, json={"job": [
        {"id": "j1", "description": "old", "status": "finished", "start_time": 1790000000000},
        {"id": "j2", "description": "new", "status": "started", "start_time": 1790000500000},
        {"id": "j3", "description": "middle", "status": "failed", "start_time": 1790000200000},
    ]})
    result = server.get_job_status(max_results=2)
    assert [j["id"] for j in result["jobs"]] == ["j2", "j3"]
    assert result["count"] == 2 and result["truncated"] is True


def test_get_job_status_rejects_non_uuid(tool_client):
    with pytest.raises(ToolError, match="not a job id"):
        server.get_job_status("migrate vm-test")


def test_get_job_status_cleared_job(tool_client, engine):
    engine.get(f"/api/jobs/{JOB_ID}").respond(404, json={})
    with pytest.raises(ToolError, match="may have cleared it"):
        server.get_job_status(JOB_ID)


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
