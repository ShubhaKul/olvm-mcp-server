"""Write actions: start, graceful shutdown, snapshots and live migration of virtual machines.

These tools are registered only when OLVM_MODE=operator. Every call:
1. resolves the VM and checks the mode and OLVM_ALLOWED_CLUSTERS,
2. with dry_run=True, describes what it would do and stops there,
3. writes a "requested" audit record, and refuses to run if it can't,
4. sends the request with a correlation id, which the engine's events carry,
5. optionally waits for the VM or snapshot to reach the expected state,
6. writes an audit record with the outcome.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import OlvmClient, OlvmError, OlvmNotFound
from .config import OPERATOR
from .formatting import summarize_snapshot
from .safety import AuditError, AuditLog, new_correlation_id
from .server import _resolve_vm, get_client

POLL_INTERVAL_SECONDS = 3.0
MAX_WAIT_SECONDS = 900

START = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                        idempotent_hint=True, open_world_hint=False)
# A graceful shutdown doesn't delete anything, but it does stop whatever the VM is serving.
SHUTDOWN = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                           idempotent_hint=True, open_world_hint=False)
SNAPSHOT = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                           idempotent_hint=False, open_world_hint=False)
# Live migration keeps the VM running, but moves load between hosts.
MIGRATE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                          idempotent_hint=False, open_world_hint=False)

# Waits take (engine response) and return (result, extra fields for the tool output).
Wait = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]


@dataclass
class _Target:
    client: OlvmClient
    audit: AuditLog
    vm: dict[str, Any]
    cluster: str | None

    @property
    def id(self) -> str:
        return self.vm["id"]

    @property
    def name(self) -> str:
        return self.vm.get("name", self.vm["id"])

    @property
    def status(self) -> str | None:
        return self.vm.get("status")

    def describe(self, action: str) -> dict[str, Any]:
        return {"action": action, "vm": self.name, "vm_id": self.id, "cluster": self.cluster}


def _target(action: str, vm_name_or_id: str) -> _Target:
    """Resolve the VM and enforce the mode and the cluster allow-list."""
    client = get_client()
    settings = client.settings
    if settings.mode != OPERATOR:
        raise ToolError("Write actions are disabled because the server is in read_only mode. "
                        "Set OLVM_MODE=operator to enable them.")
    try:
        vm = _resolve_vm(client, vm_name_or_id.strip())
        clusters = client.names("clusters", "cluster")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    target = _Target(client, AuditLog(settings.audit_log, settings.username), vm,
                     clusters.get((vm.get("cluster") or {}).get("id")))

    if not settings.cluster_allowed(target.cluster):
        detail = f"cluster {target.cluster!r} is not in OLVM_ALLOWED_CLUSTERS"
        _audit_or_fail(target, **target.describe(action), outcome="denied", detail=detail)
        raise ToolError(f"Refusing to touch VM {target.name!r}: {detail} "
                        f"({', '.join(settings.allowed_clusters)}).")
    return target


def _audit_or_fail(target: _Target, **fields: Any) -> None:
    try:
        target.audit.record(**fields)
    except AuditError as e:
        raise ToolError(f"{e}. Write actions are refused until the audit log can be written.") from e


def _no_change(target: _Target, action: str, message: str, dry_run: bool) -> dict[str, Any]:
    return {**target.describe(action), "dry_run": dry_run, "result": "no_change",
            "status": target.status, "message": message}


def _dry_run(target: _Target, action: str, message: str, **extra: Any) -> dict[str, Any]:
    return {**target.describe(action), "dry_run": True, "result": "would_run",
            "status": target.status, **extra, "message": message}


def _execute(target: _Target, action: str, params: dict[str, Any],
             send: Callable[[str], dict[str, Any]], wait: Wait | None) -> dict[str, Any]:
    correlation_id = new_correlation_id()
    base = {**target.describe(action), "correlation_id": correlation_id}
    _audit_or_fail(target, **base, params=params, outcome="requested")

    try:
        response = send(correlation_id)
    except OlvmError as e:
        _audit_or_fail(target, **base, outcome="failed", detail=str(e))
        raise ToolError(f"{e} (correlation id {correlation_id}; list_events can show "
                        f"what the engine did)") from e

    result: dict[str, Any] = {**base, "dry_run": False}
    if job_id := (response.get("job") or {}).get("id"):
        result["job_id"] = job_id
    if wait is None:
        result["result"] = "submitted"
    else:
        try:
            outcome, extra = wait(response)
        except OlvmError as e:
            outcome, extra = "pending", {"message": f"The request was sent, but checking on it failed: {e}"}
        result.update(extra)
        result["result"] = outcome

    try:
        # The VM's state for power actions; the snapshot's state for snapshots.
        status = result.get("status") or (result.get("snapshot") or {}).get("status")
        target.audit.record(**base, outcome=result["result"], status=status)
    except AuditError as e:
        # The action already ran; report it rather than hide the result.
        result["audit_warning"] = str(e)
    return result


def _poll(fetch: Callable[[], dict[str, Any]], done: Callable[[dict[str, Any]], bool],
          timeout_seconds: float) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        current = fetch()
        if done(current):
            return True, current
        if time.monotonic() >= deadline:
            return False, current
        time.sleep(POLL_INTERVAL_SECONDS)


def _clamp_wait(timeout_seconds: int) -> int:
    return max(0, min(timeout_seconds, MAX_WAIT_SECONDS))


def _wait_for_vm_status(target: _Target, expected: str, timeout_seconds: int) -> Wait:
    def wait(_response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        reached, vm = _poll(lambda: target.client.get(f"vms/{target.id}"),
                            lambda v: v.get("status") == expected, _clamp_wait(timeout_seconds))
        status = vm.get("status")
        if reached:
            return "done", {"status": status}
        return "pending", {"status": status, "message": (
            f"VM is {status}, not {expected}, after {_clamp_wait(timeout_seconds)} seconds. "
            f"Check again with get_vm, or list_events for the correlation id.")}
    return wait


def start_vm(vm_name_or_id: str, dry_run: bool = False, wait: bool = True,
             timeout_seconds: int = 300) -> dict[str, Any]:
    """Start (power on) a virtual machine.

    Only available in operator mode, and only for VMs in OLVM_ALLOWED_CLUSTERS.
    Every call that reaches the engine is written to the audit log.

    Args:
        vm_name_or_id: The VM's exact name, or its UUID.
        dry_run: Describe what would happen without starting the VM. Use this
            first and confirm with the user.
        wait: Wait until the VM is up before returning.
        timeout_seconds: How long to wait (0-900, default 300). If the VM isn't
            up by then, the result is "pending", not an error.
    """
    target = _target("start_vm", vm_name_or_id)
    if target.status == "up":
        return _no_change(target, "start_vm", f"VM {target.name} is already up.", dry_run)
    if dry_run:
        return _dry_run(target, "start_vm",
                        f"Would start VM {target.name} (now {target.status}) in cluster {target.cluster}.")
    return _execute(target, "start_vm", {},
                    lambda cid: target.client.post(f"vms/{target.id}/start", {}, cid),
                    _wait_for_vm_status(target, "up", timeout_seconds) if wait else None)


def shutdown_vm(vm_name_or_id: str, dry_run: bool = False, wait: bool = True,
                timeout_seconds: int = 300) -> dict[str, Any]:
    """Shut down a virtual machine gracefully, through its guest OS.

    This is not a power off: the guest gets an ACPI or guest-agent shutdown
    request and may take a while, or ignore it. Only available in operator mode,
    and only for VMs in OLVM_ALLOWED_CLUSTERS. Every call that reaches the
    engine is written to the audit log.

    Args:
        vm_name_or_id: The VM's exact name, or its UUID.
        dry_run: Describe what would happen without shutting the VM down. Use
            this first and confirm with the user.
        wait: Wait until the VM is down before returning.
        timeout_seconds: How long to wait (0-900, default 300). If the VM isn't
            down by then, the result is "pending", not an error.
    """
    target = _target("shutdown_vm", vm_name_or_id)
    if target.status == "down":
        return _no_change(target, "shutdown_vm", f"VM {target.name} is already down.", dry_run)
    if dry_run:
        return _dry_run(target, "shutdown_vm",
                        f"Would ask the guest OS of VM {target.name} (now {target.status}) in cluster "
                        f"{target.cluster} to shut down. Anything running on it will stop.")
    return _execute(target, "shutdown_vm", {},
                    lambda cid: target.client.post(f"vms/{target.id}/shutdown", {}, cid),
                    _wait_for_vm_status(target, "down", timeout_seconds) if wait else None)


def create_snapshot(vm_name_or_id: str, description: str, include_memory: bool = False,
                    dry_run: bool = False, wait: bool = True, timeout_seconds: int = 600) -> dict[str, Any]:
    """Create a snapshot of a virtual machine's disks.

    Only available in operator mode, and only for VMs in OLVM_ALLOWED_CLUSTERS.
    Every call that reaches the engine is written to the audit log.

    Args:
        vm_name_or_id: The VM's exact name, or its UUID.
        description: What the snapshot is for, e.g. "before kernel upgrade".
        include_memory: Also save the running VM's memory, so a restore resumes
            it where it was. Only for running VMs; makes the snapshot larger and slower.
        dry_run: Describe what would happen without creating the snapshot.
        wait: Wait until the snapshot is ready (status ok) before returning.
        timeout_seconds: How long to wait (0-900, default 600). If it isn't
            ready by then, the result is "pending", not an error.
    """
    description = description.strip()
    if not description:
        raise ToolError("description is required, e.g. 'before kernel upgrade'")
    target = _target("create_snapshot", vm_name_or_id)
    if include_memory and target.status != "up":
        raise ToolError(f"include_memory needs a running VM; {target.name} is {target.status}.")
    if dry_run:
        return _dry_run(target, "create_snapshot",
                        f"Would snapshot VM {target.name} (now {target.status}) in cluster "
                        f"{target.cluster}{' including memory' if include_memory else ''}.",
                        description=description, include_memory=include_memory)

    def wait_for_snapshot(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        snap_id = response.get("id")
        if not snap_id:
            return "submitted", {"message": "The engine accepted the request but returned no snapshot id."}
        path = f"vms/{target.id}/snapshots/{snap_id}"
        try:
            ready, snap = _poll(lambda: target.client.get(path),
                                lambda s: s.get("snapshot_status") == "ok", _clamp_wait(timeout_seconds))
        except OlvmNotFound:
            return "failed", {"snapshot_id": snap_id, "message": (
                "The engine removed the snapshot, so creating it failed. list_events shows why.")}
        extra = {"snapshot": summarize_snapshot(snap)}
        if ready:
            return "done", extra
        return "pending", {**extra, "message": (
            f"Snapshot is still {snap.get('snapshot_status')} after {_clamp_wait(timeout_seconds)} "
            f"seconds. Check again with list_snapshots.")}

    return _execute(target, "create_snapshot",
                    {"description": description, "include_memory": include_memory},
                    lambda cid: target.client.post(f"vms/{target.id}/snapshots",
                                                   {"description": description,
                                                    "persist_memorystate": include_memory}, cid),
                    wait_for_snapshot if wait else None)


def _find_host(hosts: list[dict[str, Any]], name_or_id: str) -> dict[str, Any]:
    """Match a host by id, name or address (the portal shows either as its label)."""
    wanted = name_or_id.strip().lower()
    matches = [h for h in hosts
               if wanted in {str(h.get(k, "")).lower() for k in ("id", "name", "address")}]
    if not matches:
        known = ", ".join(sorted(h.get("name", h["id"]) for h in hosts))
        raise ToolError(f"No host matches {name_or_id!r}. Hosts: {known}. Use list_hosts for details.")
    if len(matches) > 1:
        raise ToolError(f"More than one host matches {name_or_id!r}; use the host id.")
    return matches[0]


def _memory_gib(value: Any) -> float:
    try:
        return int(value) / 1024**3
    except (TypeError, ValueError):
        return 0.0


def _wait_for_migration(target: _Target, source_id: str, dest_id: str | None,
                        host_names: dict[str, str], timeout_seconds: int) -> Wait:
    """Wait until the VM runs on another host, or the migration visibly ends without moving it."""
    def wait(_response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        seen_migrating = False

        def finished(vm: dict[str, Any]) -> bool:
            nonlocal seen_migrating
            status = vm.get("status")
            seen_migrating = seen_migrating or status == "migrating"
            host_id = (vm.get("host") or {}).get("id")
            moved = status == "up" and host_id not in (None, source_id)
            # Back to "up" on the source after migrating means the engine gave up.
            returned = seen_migrating and status == "up" and host_id == source_id
            return moved or returned or status not in ("up", "migrating")

        _, vm = _poll(lambda: target.client.get(f"vms/{target.id}"), finished,
                      _clamp_wait(timeout_seconds))
        status = vm.get("status")
        host_id = (vm.get("host") or {}).get("id")
        extra = {"status": status, "host": host_names.get(host_id, host_id)}
        if status == "up" and host_id not in (None, source_id):
            if dest_id and host_id != dest_id:
                extra["message"] = f"VM moved, but to {extra['host']} rather than the requested host."
            return "done", extra
        if status == "up" and host_id == source_id and seen_migrating:
            return "failed", {**extra, "message": (
                "The migration ended and the VM is still on its original host. "
                "list_events with the correlation id shows why.")}
        if status not in ("up", "migrating"):
            return "failed", {**extra, "message": f"VM is {status} after the migration request."}
        return "pending", {**extra, "message": (
            f"VM is {status} on {extra['host']} after {_clamp_wait(timeout_seconds)} seconds. "
            f"Check again with get_vm, or list_events for the correlation id.")}
    return wait


def migrate_vm(vm_name_or_id: str, target_host: str = "", dry_run: bool = False,
               wait: bool = True, timeout_seconds: int = 600) -> dict[str, Any]:
    """Live-migrate a running virtual machine to another host in its cluster.

    The VM keeps running while its memory is copied to the other host. Only
    available in operator mode, and only for VMs in OLVM_ALLOWED_CLUSTERS.
    Every call that reaches the engine is written to the audit log.

    Args:
        vm_name_or_id: The VM's exact name, or its UUID.
        target_host: Host to move to, by name, address or id. Leave empty to
            let the engine's scheduler choose a host.
        dry_run: Describe what would happen without migrating. Use this first
            and confirm with the user.
        wait: Wait until the VM is running on the new host before returning.
        timeout_seconds: How long to wait (0-900, default 600). If the move
            hasn't finished by then, the result is "pending", not an error.
    """
    target = _target("migrate_vm", vm_name_or_id)
    if target.status != "up":
        raise ToolError(f"Only running VMs can be live-migrated; {target.name} is {target.status}.")

    try:
        hosts = target.client.list("hosts", "host")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    host_names = {h["id"]: h.get("name", h["id"]) for h in hosts}
    source_id = (target.vm.get("host") or {}).get("id")
    source = host_names.get(source_id, source_id)
    cluster_id = (target.vm.get("cluster") or {}).get("id")

    dest: dict[str, Any] | None = None
    warnings: list[str] = []
    if target_host.strip():
        dest = _find_host(hosts, target_host)
        if dest["id"] == source_id:
            return _no_change(target, "migrate_vm", f"VM {target.name} already runs on {source}.", dry_run)
        if (dest.get("cluster") or {}).get("id") != cluster_id:
            raise ToolError(f"Host {dest.get('name')} is not in the VM's cluster {target.cluster}; "
                            f"this tool only migrates within a cluster.")
        if dest.get("status") != "up":
            raise ToolError(f"Host {dest.get('name')} is {dest.get('status')}; it must be up.")
        free = _memory_gib(dest.get("max_scheduling_memory"))
        needed = _memory_gib(target.vm.get("memory"))
        if free and needed > free:
            warnings.append(f"{dest.get('name')} can schedule {free:.1f} GiB but the VM has "
                            f"{needed:.1f} GiB; the engine will probably refuse.")
    else:
        others = [h for h in hosts if h["id"] != source_id and h.get("status") == "up"
                  and (h.get("cluster") or {}).get("id") == cluster_id]
        if not others:
            raise ToolError(f"No other host in cluster {target.cluster} is up, so {target.name} "
                            f"has nowhere to go.")

    dest_label = dest.get("name") if dest else "a host chosen by the engine"
    if dry_run:
        extra: dict[str, Any] = {"from_host": source, "to_host": dest.get("name") if dest else None}
        if warnings:
            extra["warnings"] = warnings
        return _dry_run(target, "migrate_vm",
                        f"Would live-migrate VM {target.name} in cluster {target.cluster} from "
                        f"{source} to {dest_label}. The VM keeps running during the move.", **extra)

    body = {"host": {"id": dest["id"]}} if dest else {}
    result = _execute(target, "migrate_vm", {"target_host": dest.get("name") if dest else None},
                      lambda cid: target.client.post(f"vms/{target.id}/migrate", body, cid),
                      _wait_for_migration(target, source_id, dest["id"] if dest else None,
                                          host_names, timeout_seconds) if wait else None)
    result["from_host"] = source
    return result


def register(mcp: MCPServer) -> None:
    """Add the write tools to the server. Called only in operator mode."""
    mcp.add_tool(start_vm, annotations=START)
    mcp.add_tool(shutdown_vm, annotations=SHUTDOWN)
    mcp.add_tool(create_snapshot, annotations=SNAPSHOT)
    mcp.add_tool(migrate_vm, annotations=MIGRATE)
