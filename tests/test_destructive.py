import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from olvm_mcp import actions, server
from olvm_mcp.safety import ConfirmationTokens

from .conftest import VM_DOWN, VM_UP
from .test_actions import audit_records, by_name

DOWN_ID = VM_DOWN["id"]


@pytest.fixture
def destructive(operator, monkeypatch):
    """Operator mode with OLVM_ALLOW_DESTRUCTIVE, and a fresh token store per test."""
    monkeypatch.setattr(operator, "_settings", replace(operator.settings, allow_destructive=True))
    monkeypatch.setattr(actions, "_tokens", ConfirmationTokens())
    return operator


def confirm(tool, *args, **kwargs):
    """Run the preview, then confirm it with the token, as a client would after the user agrees."""
    preview = tool(*args, **kwargs)
    assert preview["result"] == "confirmation_required"
    return preview, tool(*args, **kwargs, confirm_token=preview["confirm_token"])


# -- guard rails shared by all destructive tools ------------------------------

@pytest.mark.parametrize("tool", [actions.stop_vm, actions.remove_vm])
def test_disabled_without_allow_destructive(operator, engine, tool):
    by_name(engine, VM_UP)
    with pytest.raises(ToolError, match="OLVM_ALLOW_DESTRUCTIVE"):
        tool("vm-test")


def test_disabled_in_read_only_mode(tool_client, engine):
    with pytest.raises(ToolError, match="read_only mode"):
        actions.stop_vm("vm-test")


def test_preview_sends_nothing_and_audits_nothing(destructive, engine, audit_path):
    by_name(engine, VM_UP)
    stop = engine.post(f"/api/vms/{VM_UP['id']}/stop")
    preview = actions.stop_vm("vm-test")
    assert preview["result"] == "confirmation_required" and preview["status"] == "up"
    assert preview["confirm_token"].startswith("confirm-") and preview["expires_in_seconds"] == 300
    assert "power off VM vm-test" in preview["message"] and "Nothing has changed yet" in preview["message"]
    assert not stop.called and not audit_path.exists()


def test_bad_token_is_refused_and_audited(destructive, engine, audit_path):
    by_name(engine, VM_UP)
    stop = engine.post(f"/api/vms/{VM_UP['id']}/stop")
    with pytest.raises(ToolError, match="unknown or was already used. Nothing was changed"):
        actions.stop_vm("vm-test", confirm_token="confirm-guessed")
    assert not stop.called
    [record] = audit_records(audit_path)
    assert record["outcome"] == "denied" and record["action"] == "stop_vm"


def test_token_for_another_vm_is_refused(destructive, engine):
    by_name(engine, VM_UP)
    by_name(engine, {**VM_UP, "id": "other-id", "name": "vm-other"})
    token = actions.stop_vm("vm-other")["confirm_token"]
    with pytest.raises(ToolError, match="different request"):
        actions.stop_vm("vm-test", confirm_token=token)


def test_target_changed_since_preview_is_refused(destructive, engine):
    route = engine.get("/api/vms", params={"search": 'name="vm-test"'})
    route.mock(side_effect=[httpx.Response(200, json={"vm": [VM_UP]}),
                            httpx.Response(200, json={"vm": [{**VM_UP, "status": "paused"}]})])
    token = actions.stop_vm("vm-test")["confirm_token"]
    with pytest.raises(ToolError, match="changed since the preview"):
        actions.stop_vm("vm-test", confirm_token=token)


def test_token_works_only_once(destructive, engine):
    by_name(engine, VM_UP)
    engine.post(f"/api/vms/{VM_UP['id']}/stop").respond(200, json={})
    preview = actions.stop_vm("vm-test")
    actions.stop_vm("vm-test", confirm_token=preview["confirm_token"], wait=False)
    with pytest.raises(ToolError, match="already used"):
        actions.stop_vm("vm-test", confirm_token=preview["confirm_token"], wait=False)


# -- stop_vm ------------------------------------------------------------------

def test_stop_vm_powers_off_after_confirmation(destructive, engine, audit_path):
    by_name(engine, VM_UP)
    stop = engine.post(f"/api/vms/{VM_UP['id']}/stop").respond(200, json={})
    engine.get(f"/api/vms/{VM_UP['id']}").mock(side_effect=[
        httpx.Response(200, json={**VM_UP, "status": "powering_down"}),
        httpx.Response(200, json={**VM_UP, "status": "down"})])
    _, result = confirm(actions.stop_vm, "vm-test")
    assert result["result"] == "done" and result["status"] == "down"
    assert stop.calls.last.request.headers["Correlation-Id"] == result["correlation_id"]
    requested, done = audit_records(audit_path)
    assert requested["outcome"] == "requested" and done["outcome"] == "done"


def test_stop_vm_on_down_vm_is_no_change(destructive, engine):
    by_name(engine, VM_DOWN)
    assert actions.stop_vm("vm-2")["result"] == "no_change"


# -- restore_snapshot ---------------------------------------------------------

SNAP_OLD = {"id": "s1", "description": "before upgrade", "date": 1790000000000,
            "snapshot_status": "ok", "snapshot_type": "regular", "persist_memorystate": "false"}
SNAP_NEW = {"id": "s2", "description": "after upgrade", "date": 1790000500000,
            "snapshot_status": "ok", "snapshot_type": "regular", "persist_memorystate": "true"}
ACTIVE = {"id": "s0", "description": "Active VM", "snapshot_status": "ok", "snapshot_type": "active"}


def snapshots(engine, *snaps):
    engine.get(f"/api/vms/{DOWN_ID}/snapshots").respond(200, json={"snapshot": list(snaps)})


def test_restore_preview_lists_snapshots_that_will_be_deleted(destructive, engine):
    by_name(engine, VM_DOWN)
    snapshots(engine, ACTIVE, SNAP_NEW, SNAP_OLD)  # unsorted on purpose
    preview = actions.restore_snapshot("vm-2", "before upgrade")
    assert preview["snapshot"]["id"] == "s1"
    assert [s["id"] for s in preview["snapshots_deleted"]] == ["s2"]
    assert "'after upgrade'" in preview["message"] and "everything written since is lost" in preview["message"]


def test_restore_runs_after_confirmation(destructive, engine, audit_path):
    by_name(engine, VM_DOWN)
    snapshots(engine, ACTIVE, SNAP_OLD, SNAP_NEW)
    restore = engine.post(f"/api/vms/{DOWN_ID}/snapshots/s1/restore").respond(200, json={})
    engine.get(f"/api/vms/{DOWN_ID}").mock(side_effect=[
        httpx.Response(200, json={**VM_DOWN, "status": "image_locked"}),
        httpx.Response(200, json=VM_DOWN)])
    _, result = confirm(actions.restore_snapshot, "vm-2", "s1")
    assert result["result"] == "done" and result["status"] == "down"
    assert json.loads(restore.calls.last.request.content) == {"restore_memory": False}
    assert audit_records(audit_path)[0]["params"] == {"snapshot_id": "s1", "restore_memory": False}


def test_restore_waits_while_snapshots_are_locked(destructive, engine):
    by_name(engine, VM_DOWN)
    ok, locked = {"snapshot": [SNAP_OLD]}, {"snapshot": [{**SNAP_OLD, "snapshot_status": "locked"}]}
    # preview check, confirmation check, then the engine locks it while restoring
    engine.get(f"/api/vms/{DOWN_ID}/snapshots").mock(side_effect=[
        httpx.Response(200, json=ok), httpx.Response(200, json=ok), httpx.Response(200, json=locked)])
    engine.post(f"/api/vms/{DOWN_ID}/snapshots/s1/restore").respond(200, json={})
    engine.get(f"/api/vms/{DOWN_ID}").respond(200, json=VM_DOWN)
    preview = actions.restore_snapshot("vm-2", "s1")
    result = actions.restore_snapshot("vm-2", "s1", confirm_token=preview["confirm_token"], timeout_seconds=0)
    assert result["result"] == "pending" and "still running" in result["message"]


def test_new_snapshot_after_preview_invalidates_the_token(destructive, engine):
    by_name(engine, VM_DOWN)
    snapshots(engine, SNAP_OLD)
    token = actions.restore_snapshot("vm-2", "s1")["confirm_token"]
    snapshots(engine, SNAP_OLD, SNAP_NEW)
    with pytest.raises(ToolError, match="changed since the preview"):
        actions.restore_snapshot("vm-2", "s1", confirm_token=token)


def test_restore_needs_a_stopped_vm(destructive, engine):
    by_name(engine, VM_UP)
    with pytest.raises(ToolError, match="only restores snapshots of stopped VMs"):
        actions.restore_snapshot("vm-test", "s1")


@pytest.mark.parametrize("snapshot, kwargs, message", [
    ("nope", {}, "No snapshot matches 'nope'"),
    ("Active VM", {}, "No snapshot matches"),                 # the active snapshot isn't restorable
    ("before upgrade", {"restore_memory": True}, "no saved memory"),
])
def test_restore_rejects_bad_snapshot_choices(destructive, engine, snapshot, kwargs, message):
    by_name(engine, VM_DOWN)
    snapshots(engine, ACTIVE, SNAP_OLD, SNAP_NEW)
    with pytest.raises(ToolError, match=message):
        actions.restore_snapshot("vm-2", snapshot, **kwargs)


def test_restore_rejects_snapshot_that_is_not_ok(destructive, engine):
    by_name(engine, VM_DOWN)
    snapshots(engine, {**SNAP_OLD, "snapshot_status": "locked"})
    with pytest.raises(ToolError, match="is locked"):
        actions.restore_snapshot("vm-2", "s1")


def test_restore_ambiguous_description(destructive, engine):
    by_name(engine, VM_DOWN)
    snapshots(engine, SNAP_OLD, {**SNAP_NEW, "description": "before upgrade"})
    with pytest.raises(ToolError, match="More than one snapshot"):
        actions.restore_snapshot("vm-2", "before upgrade")


# -- remove_vm ----------------------------------------------------------------

DISKS = {"disk_attachment": [{"disk": {"id": "d1", "alias": "vm-2_Disk1",
                                       "provisioned_size": str(10 * 1024**3)}}]}


def test_remove_preview_names_the_disks(destructive, engine):
    by_name(engine, VM_DOWN)
    engine.get(f"/api/vms/{DOWN_ID}/diskattachments").respond(200, json=DISKS)
    preview = actions.remove_vm("vm-2")
    assert preview["disks"] == ["vm-2_Disk1"]
    assert "deleted too: vm-2_Disk1 (10 GiB)" in preview["message"] and "can't be undone" in preview["message"]
    kept = actions.remove_vm("vm-2", remove_disks=False)
    assert "kept as unattached disks" in kept["message"]


@pytest.mark.parametrize("remove_disks, detach_only", [(True, "false"), (False, "true")])
def test_remove_deletes_after_confirmation(destructive, engine, audit_path, remove_disks, detach_only):
    by_name(engine, VM_DOWN)
    engine.get(f"/api/vms/{DOWN_ID}/diskattachments").respond(200, json=DISKS)
    delete = engine.delete(f"/api/vms/{DOWN_ID}").respond(200)
    engine.get(f"/api/vms/{DOWN_ID}").mock(side_effect=[
        httpx.Response(200, json={**VM_DOWN, "status": "image_locked"}),
        httpx.Response(404, json={})])
    _, result = confirm(actions.remove_vm, "vm-2", remove_disks=remove_disks)
    assert result["result"] == "done" and result["status"] == "removed"
    assert delete.calls.last.request.url.params["detach_only"] == detach_only
    assert audit_records(audit_path)[-1]["status"] == "removed"


def test_remove_token_is_bound_to_remove_disks(destructive, engine):
    by_name(engine, VM_DOWN)
    engine.get(f"/api/vms/{DOWN_ID}/diskattachments").respond(200, json=DISKS)
    token = actions.remove_vm("vm-2", remove_disks=False)["confirm_token"]
    with pytest.raises(ToolError, match="different request"):
        actions.remove_vm("vm-2", remove_disks=True, confirm_token=token)


def test_remove_refuses_running_vm(destructive, engine):
    by_name(engine, VM_UP)
    with pytest.raises(ToolError, match="only a stopped VM can be removed"):
        actions.remove_vm("vm-test")


def test_remove_refuses_delete_protected_vm(destructive, engine):
    by_name(engine, {**VM_DOWN, "delete_protected": "true"})
    delete = engine.delete(f"/api/vms/{DOWN_ID}")
    with pytest.raises(ToolError, match="delete protection"):
        actions.remove_vm("vm-2")
    assert not delete.called


# -- registration -------------------------------------------------------------

def test_destructive_tools_registered_only_on_request():
    plain, full = MCPServer(name="a"), MCPServer(name="b")
    actions.register(plain)
    actions.register(full, destructive=True)
    names = {t.name for t in asyncio.run(plain.list_tools())}
    tools = {t.name: t.annotations for t in asyncio.run(full.list_tools())}
    assert not names & {"stop_vm", "restore_snapshot", "remove_vm"}
    assert {"stop_vm", "restore_snapshot", "remove_vm"} <= set(tools)
    for name in ("stop_vm", "restore_snapshot", "remove_vm"):
        assert tools[name].destructive_hint is True and tools[name].read_only_hint is False


@pytest.mark.parametrize("mode, flag, expected", [
    ("operator", "true", True),
    ("operator", "", False),
    ("", "true", False),
])
def test_main_registers_destructive_tools_only_with_both_settings(monkeypatch, mode, flag, expected):
    fresh = MCPServer(name="t")
    monkeypatch.setattr(fresh, "run", lambda: None)
    monkeypatch.setattr(server, "mcp", fresh)
    monkeypatch.setenv("OLVM_MODE", mode)
    monkeypatch.setenv("OLVM_ALLOW_DESTRUCTIVE", flag)
    server.main()
    names = {t.name for t in asyncio.run(fresh.list_tools())}
    assert ("remove_vm" in names) is expected
