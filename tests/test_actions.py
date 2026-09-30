import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from olvm_mcp import actions, server
from olvm_mcp.client import OlvmClient

from .conftest import VM_DOWN, VM_UP

VM_ID = VM_UP["id"]
SNAP_ID = "s-new"


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit" / "audit.jsonl"


@pytest.fixture
def operator(settings, engine, audit_path, monkeypatch):
    """Tools pointed at the mocked engine, in operator mode for cluster Default."""
    s = replace(settings, mode="operator", allowed_clusters=("Default",), audit_log=audit_path)
    c = OlvmClient(s)
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(actions, "POLL_INTERVAL_SECONDS", 0)
    yield c
    c.close()


def audit_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def by_name(engine, vm):
    engine.get("/api/vms", params={"search": f'name="{vm["name"]}"'}).respond(200, json={"vm": [vm]})


# -- guard rails ------------------------------------------------------------

def test_read_only_mode_refuses_writes(tool_client, engine):
    with pytest.raises(ToolError, match="read_only mode"):
        actions.start_vm("vm-test")


def test_cluster_outside_allow_list_is_refused_and_audited(operator, engine, audit_path, monkeypatch):
    monkeypatch.setattr(operator, "_settings", replace(operator.settings, allowed_clusters=("Prod",)))
    by_name(engine, VM_DOWN)
    start = engine.post(f"/api/vms/{VM_DOWN['id']}/start")
    with pytest.raises(ToolError, match="not in OLVM_ALLOWED_CLUSTERS"):
        actions.start_vm("vm-2")
    assert not start.called
    [record] = audit_records(audit_path)
    assert record["outcome"] == "denied" and record["vm"] == "vm-2" and record["cluster"] == "Default"


def test_unwritable_audit_log_blocks_the_action(operator, engine, tmp_path, monkeypatch):
    # A directory can't be opened for appending, so the audit write fails.
    monkeypatch.setattr(operator, "_settings", replace(operator.settings, audit_log=tmp_path))
    by_name(engine, VM_DOWN)
    start = engine.post(f"/api/vms/{VM_DOWN['id']}/start")
    with pytest.raises(ToolError, match="audit log"):
        actions.start_vm("vm-2")
    assert not start.called


def test_dry_run_sends_nothing(operator, engine, audit_path):
    by_name(engine, VM_DOWN)
    start = engine.post(f"/api/vms/{VM_DOWN['id']}/start")
    result = actions.start_vm("vm-2", dry_run=True)
    assert result["dry_run"] is True and result["result"] == "would_run"
    assert result["status"] == "down" and "Would start VM vm-2" in result["message"]
    assert not start.called
    assert not audit_path.exists()


# -- start / shutdown -------------------------------------------------------

def test_start_vm_waits_until_up_and_audits(operator, engine, audit_path):
    vm_id = VM_DOWN["id"]
    by_name(engine, VM_DOWN)
    start = engine.post(f"/api/vms/{vm_id}/start").respond(200, json={"status": "complete"})
    engine.get(f"/api/vms/{vm_id}").mock(side_effect=[
        httpx.Response(200, json={**VM_DOWN, "status": "wait_for_launch"}),
        httpx.Response(200, json={**VM_DOWN, "status": "up"}),
    ])
    result = actions.start_vm("vm-2")
    assert result["result"] == "done" and result["status"] == "up"

    request = start.calls.last.request
    assert json.loads(request.content) == {}
    assert request.headers["Correlation-Id"] == result["correlation_id"]
    assert result["correlation_id"].startswith("olvm-mcp-")

    requested, outcome = audit_records(audit_path)
    assert requested["outcome"] == "requested" and outcome["outcome"] == "done"
    assert requested["correlation_id"] == outcome["correlation_id"] == result["correlation_id"]
    assert requested["user"] == "mcp-reader@ovirt@internalsso" and requested["action"] == "start_vm"


def test_start_vm_already_up_is_no_change(operator, engine):
    by_name(engine, VM_UP)
    start = engine.post(f"/api/vms/{VM_ID}/start")
    assert actions.start_vm("vm-test")["result"] == "no_change"
    assert not start.called


def test_start_vm_timeout_is_pending_not_error(operator, engine, audit_path):
    vm_id = VM_DOWN["id"]
    by_name(engine, VM_DOWN)
    engine.post(f"/api/vms/{vm_id}/start").respond(200, json={})
    engine.get(f"/api/vms/{vm_id}").respond(200, json={**VM_DOWN, "status": "powering_up"})
    result = actions.start_vm("vm-2", timeout_seconds=0)
    assert result["result"] == "pending" and result["status"] == "powering_up"
    assert "get_vm" in result["message"]
    assert audit_records(audit_path)[-1]["outcome"] == "pending"


def test_start_vm_without_wait_is_submitted(operator, engine):
    by_name(engine, VM_DOWN)
    engine.post(f"/api/vms/{VM_DOWN['id']}/start").respond(200, json={"job": {"id": "j-1"}})
    result = actions.start_vm("vm-2", wait=False)
    assert result["result"] == "submitted" and result["job_id"] == "j-1"


def test_engine_rejection_is_audited_with_correlation_id(operator, engine, audit_path):
    by_name(engine, VM_DOWN)
    engine.post(f"/api/vms/{VM_DOWN['id']}/start").respond(
        409, json={"reason": "Operation Failed", "detail": "[Cannot run VM. Low disk space.]"})
    with pytest.raises(ToolError, match=r"Low disk space.*correlation id olvm-mcp-"):
        actions.start_vm("vm-2")
    requested, failed = audit_records(audit_path)
    assert failed["outcome"] == "failed" and "Low disk space" in failed["detail"]


def test_shutdown_vm_waits_until_down(operator, engine):
    by_name(engine, VM_UP)
    shutdown = engine.post(f"/api/vms/{VM_ID}/shutdown").respond(200, json={})
    engine.get(f"/api/vms/{VM_ID}").respond(200, json={**VM_UP, "status": "down"})
    result = actions.shutdown_vm("vm-test")
    assert shutdown.called and result["result"] == "done" and result["status"] == "down"


def test_shutdown_vm_already_down_is_no_change(operator, engine):
    by_name(engine, VM_DOWN)
    assert actions.shutdown_vm("vm-2")["result"] == "no_change"


def test_no_change_reports_the_dry_run_flag(operator, engine):
    by_name(engine, VM_DOWN)
    result = actions.shutdown_vm("vm-2", dry_run=True)
    assert result["result"] == "no_change" and result["dry_run"] is True


# -- snapshots --------------------------------------------------------------

def test_create_snapshot_waits_until_ok(operator, engine, audit_path):
    by_name(engine, VM_UP)
    create = engine.post(f"/api/vms/{VM_ID}/snapshots").respond(
        201, json={"id": SNAP_ID, "snapshot_status": "locked"})
    engine.get(f"/api/vms/{VM_ID}/snapshots/{SNAP_ID}").mock(side_effect=[
        httpx.Response(200, json={"id": SNAP_ID, "snapshot_status": "locked"}),
        httpx.Response(200, json={"id": SNAP_ID, "description": "before upgrade",
                                  "snapshot_status": "ok", "snapshot_type": "regular"}),
    ])
    result = actions.create_snapshot("vm-test", "  before upgrade ", include_memory=True)
    assert json.loads(create.calls.last.request.content) == {
        "description": "before upgrade", "persist_memorystate": True}
    assert result["result"] == "done"
    assert result["snapshot"] == {"id": SNAP_ID, "description": "before upgrade",
                                  "status": "ok", "type": "regular"}
    requested, outcome = audit_records(audit_path)
    assert requested["params"] == {"description": "before upgrade", "include_memory": True}
    assert outcome["outcome"] == "done" and outcome["status"] == "ok"


def test_create_snapshot_removed_by_engine_is_failed(operator, engine):
    by_name(engine, VM_UP)
    engine.post(f"/api/vms/{VM_ID}/snapshots").respond(201, json={"id": SNAP_ID})
    engine.get(f"/api/vms/{VM_ID}/snapshots/{SNAP_ID}").respond(404, json={})
    result = actions.create_snapshot("vm-test", "x")
    assert result["result"] == "failed" and "list_events" in result["message"]


def test_create_snapshot_memory_needs_running_vm(operator, engine):
    by_name(engine, VM_DOWN)
    with pytest.raises(ToolError, match="include_memory needs a running VM"):
        actions.create_snapshot("vm-2", "x", include_memory=True)


def test_create_snapshot_needs_description(operator):
    with pytest.raises(ToolError, match="description is required"):
        actions.create_snapshot("vm-test", "   ")


# -- registration -----------------------------------------------------------

def test_register_adds_write_tools_with_annotations():
    mcp = MCPServer(name="t")
    actions.register(mcp)
    tools = {t.name: t.annotations for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"start_vm", "shutdown_vm", "create_snapshot", "migrate_vm"}
    assert all(a.read_only_hint is False for a in tools.values())
    assert tools["shutdown_vm"].destructive_hint is True
    assert tools["create_snapshot"].idempotent_hint is False
    assert tools["migrate_vm"].destructive_hint is False


@pytest.mark.parametrize("mode, expected", [("operator", True), ("", False), ("bogus", False)])
def test_main_registers_write_tools_only_in_operator_mode(monkeypatch, mode, expected):
    fresh = MCPServer(name="t")
    monkeypatch.setattr(fresh, "run", lambda: None)
    monkeypatch.setattr(server, "mcp", fresh)
    monkeypatch.setenv("OLVM_MODE", mode)
    server.main()
    names = {t.name for t in asyncio.run(fresh.list_tools())}
    assert ("start_vm" in names) is expected


# -- migrate_vm -------------------------------------------------------------

H1 = {"id": "h1", "name": "kvm01.example.test", "address": "10.0.0.141", "status": "up",
      "cluster": {"id": "c1"}, "max_scheduling_memory": str(12 * 1024**3)}
H2 = {"id": "h2", "name": "kvm02.example.test", "address": "10.0.0.149", "status": "up",
      "cluster": {"id": "c1"}, "max_scheduling_memory": str(12 * 1024**3)}


def two_hosts(engine, *hosts):
    engine.get("/api/hosts").respond(200, json={"host": list(hosts or (H1, H2))})


def vm_states(engine, *states):
    """Successive GETs of the VM return these (status, host id) pairs."""
    engine.get(f"/api/vms/{VM_ID}").mock(side_effect=[
        httpx.Response(200, json={**VM_UP, "status": s, "host": {"id": h}}) for s, h in states])


def test_migrate_dry_run_names_both_hosts(operator, engine, audit_path):
    by_name(engine, VM_UP)
    two_hosts(engine)
    migrate = engine.post(f"/api/vms/{VM_ID}/migrate")
    result = actions.migrate_vm("vm-test", target_host="10.0.0.149", dry_run=True)
    assert result["result"] == "would_run"
    assert result["from_host"] == "kvm01.example.test" and result["to_host"] == "kvm02.example.test"
    assert "keeps running" in result["message"] and "warnings" not in result
    assert not migrate.called and not audit_path.exists()


def test_migrate_to_named_host_waits_until_it_runs_there(operator, engine, audit_path):
    by_name(engine, VM_UP)
    two_hosts(engine)
    migrate = engine.post(f"/api/vms/{VM_ID}/migrate").respond(200, json={"status": "complete"})
    vm_states(engine, ("up", "h1"), ("migrating", "h1"), ("up", "h2"))
    result = actions.migrate_vm("vm-test", target_host="kvm02.example.test")
    assert result["result"] == "done" and result["host"] == "kvm02.example.test"
    assert result["from_host"] == "kvm01.example.test"
    assert json.loads(migrate.calls.last.request.content) == {"host": {"id": "h2"}}
    assert migrate.calls.last.request.headers["Correlation-Id"] == result["correlation_id"]
    requested, done = audit_records(audit_path)
    assert requested["params"] == {"target_host": "kvm02.example.test"}
    assert done["outcome"] == "done" and done["status"] == "up"


def test_migrate_without_target_lets_the_engine_choose(operator, engine):
    by_name(engine, VM_UP)
    two_hosts(engine)
    migrate = engine.post(f"/api/vms/{VM_ID}/migrate").respond(200, json={})
    vm_states(engine, ("migrating", "h1"), ("up", "h2"))
    result = actions.migrate_vm("vm-test")
    assert result["result"] == "done"
    assert json.loads(migrate.calls.last.request.content) == {}


def test_migration_that_returns_to_the_source_is_a_failure(operator, engine, audit_path):
    by_name(engine, VM_UP)
    two_hosts(engine)
    engine.post(f"/api/vms/{VM_ID}/migrate").respond(200, json={})
    vm_states(engine, ("migrating", "h1"), ("up", "h1"))
    result = actions.migrate_vm("vm-test", target_host="h2")
    assert result["result"] == "failed" and result["host"] == "kvm01.example.test"
    assert "still on its original host" in result["message"]
    assert audit_records(audit_path)[-1]["outcome"] == "failed"


def test_migration_not_finished_in_time_is_pending(operator, engine):
    by_name(engine, VM_UP)
    two_hosts(engine)
    engine.post(f"/api/vms/{VM_ID}/migrate").respond(200, json={})
    vm_states(engine, ("migrating", "h1"))
    result = actions.migrate_vm("vm-test", target_host="h2", timeout_seconds=0)
    assert result["result"] == "pending" and result["status"] == "migrating"


def test_migrate_to_current_host_is_no_change(operator, engine):
    by_name(engine, VM_UP)
    two_hosts(engine)
    migrate = engine.post(f"/api/vms/{VM_ID}/migrate")
    result = actions.migrate_vm("vm-test", target_host="kvm01.example.test")
    assert result["result"] == "no_change" and not migrate.called


@pytest.mark.parametrize("host, message", [
    ("kvm99", "No host matches"),
    ("h3", "not in the VM's cluster"),
    ("h4", "must be up"),
])
def test_migrate_rejects_unusable_targets(operator, engine, host, message):
    by_name(engine, VM_UP)
    two_hosts(engine, H1, H2, {**H2, "id": "h3", "name": "other", "address": "x",
                               "cluster": {"id": "c9"}},
              {**H2, "id": "h4", "name": "sleepy", "address": "y", "status": "maintenance"})
    with pytest.raises(ToolError, match=message):
        actions.migrate_vm("vm-test", target_host=host)


def test_migrate_needs_a_running_vm(operator, engine):
    by_name(engine, VM_DOWN)
    with pytest.raises(ToolError, match="Only running VMs"):
        actions.migrate_vm("vm-2")


def test_migrate_needs_another_host_when_engine_chooses(operator, engine):
    by_name(engine, VM_UP)
    two_hosts(engine, H1, {**H2, "status": "maintenance"})
    with pytest.raises(ToolError, match="nowhere to go"):
        actions.migrate_vm("vm-test")


def test_migrate_dry_run_warns_when_target_lacks_memory(operator, engine):
    by_name(engine, VM_UP)
    two_hosts(engine, H1, {**H2, "max_scheduling_memory": str(1024**3)})
    result = actions.migrate_vm("vm-test", target_host="h2", dry_run=True)
    assert "probably refuse" in result["warnings"][0]
