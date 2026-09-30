"""MCP server exposing read-only OLVM / oVirt tools over stdio."""

from __future__ import annotations

import atexit
import logging
import re
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import OlvmClient, OlvmError, OlvmNotFound
from .config import OPERATOR, READ_ONLY, ConfigError, Settings, mode_from_env
from .formatting import (
    detail_vm,
    summarize_event,
    summarize_host,
    summarize_job,
    summarize_snapshot,
    summarize_storage_domain,
    summarize_vm,
)

log = logging.getLogger("olvm_mcp")
# httpx logs every request at INFO, which would echo engine URLs into client logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_RESULTS_LIMIT = 200
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="olvm",
    instructions=(
        "Access to an Oracle Linux Virtualization Manager (OLVM) / oVirt engine. "
        "Use list_vms, list_hosts and list_storage_domains to find objects (they accept oVirt "
        "search syntax), then get_vm or list_snapshots for one VM. Use list_events to see what "
        "happened recently, and get_job_status to follow long-running engine operations. "
        "In operator mode the server also offers start_vm, shutdown_vm, create_snapshot, "
        "migrate_vm and set_host_maintenance: "
        "call them with dry_run=true first, show the user what would change, and only run the "
        "action after the user confirms."
    ),
)

_client: OlvmClient | None = None


def get_client() -> OlvmClient:
    """Create the engine client on first use, so config errors surface as tool errors."""
    global _client
    if _client is None:
        try:
            _client = OlvmClient(Settings.from_env())
        except ConfigError as e:
            raise ToolError(f"Server is not configured: {e}") from e
        atexit.register(_client.close)
    return _client


def _clamp(max_results: int) -> int:
    return max(1, min(max_results, MAX_RESULTS_LIMIT))


def _list(collection: str, key: str, search: str, max_results: int) -> tuple[list[dict[str, Any]], bool]:
    """List up to max_results items, and whether more exist."""
    limit = _clamp(max_results)
    items = get_client().list(collection, key, search=search.strip() or None, max_results=limit + 1)
    return items[:limit], len(items) > limit


@mcp.tool(annotations=READ_ONLY)
def list_vms(search: str = "", max_results: int = 50) -> dict[str, Any]:
    """List virtual machines with their status, cluster, host, CPUs and memory.

    Args:
        search: Optional oVirt search query, for example `status=up`,
            `name=web*`, `cluster=Default and status=down`, or `host=kvm01`.
            Leave empty to list all VMs.
        max_results: Maximum number of VMs to return (1-200, default 50).
    """
    try:
        vms, truncated = _list("vms", "vm", search, max_results)
        client = get_client()
        clusters, hosts = client.names("clusters", "cluster"), client.names("hosts", "host")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    return {
        "count": len(vms),
        "truncated": truncated,
        "vms": [summarize_vm(vm, clusters, hosts) for vm in vms],
    }


@mcp.tool(annotations=READ_ONLY)
def get_vm(name_or_id: str) -> dict[str, Any]:
    """Get details of one virtual machine, including its disks and network interfaces.

    Args:
        name_or_id: The VM's exact name, or its UUID.
    """
    client = get_client()
    try:
        vm = _resolve_vm(client, name_or_id.strip())
        vm_path = f"vms/{vm['id']}"
        disks = client.get(f"{vm_path}/diskattachments", {"follow": "disk"}).get("disk_attachment", [])
        nics = client.list(f"{vm_path}/nics", "nic")
        clusters, hosts = client.names("clusters", "cluster"), client.names("hosts", "host")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    return detail_vm(vm, clusters, hosts, disks, nics)


def _resolve_vm(client: OlvmClient, name_or_id: str) -> dict[str, Any]:
    if not name_or_id:
        raise ToolError("name_or_id is required")
    if _UUID.match(name_or_id):
        try:
            return client.get(f"vms/{name_or_id}")
        except OlvmNotFound:
            raise ToolError(f"No VM with id {name_or_id}") from None

    quoted = name_or_id.replace('"', '\\"')
    matches = [vm for vm in client.list("vms", "vm", search=f'name="{quoted}"')
               if vm.get("name") == name_or_id]
    if not matches:
        raise ToolError(f"No VM named {name_or_id!r}. Use list_vms to see available VMs.")
    if len(matches) > 1:
        ids = ", ".join(vm["id"] for vm in matches)
        raise ToolError(f"More than one VM is named {name_or_id!r}; use one of these ids: {ids}")
    return matches[0]


@mcp.tool(annotations=READ_ONLY)
def list_hosts(search: str = "", max_results: int = 50) -> dict[str, Any]:
    """List KVM hosts with their status, cluster, CPU, memory and running VM count.

    Args:
        search: Optional oVirt search query, for example `status=up`,
            `cluster=Default`, or `name=kvm*`. Leave empty to list all hosts.
        max_results: Maximum number of hosts to return (1-200, default 50).
    """
    try:
        hosts, truncated = _list("hosts", "host", search, max_results)
        clusters = get_client().names("clusters", "cluster")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    return {
        "count": len(hosts),
        "truncated": truncated,
        "hosts": [summarize_host(h, clusters) for h in hosts],
    }


@mcp.tool(annotations=READ_ONLY)
def list_events(search: str = "", max_results: int = 50) -> dict[str, Any]:
    """List engine events (audit log entries), newest first.

    Args:
        search: Optional oVirt search query, for example `severity>normal`
            (warnings and errors only), `severity=error`, `vm.name=vm-test`, or
            `host.name=kvm*`. Leave empty for all events, including routine ones.
        max_results: Maximum number of events to return (1-200, default 50).
    """
    try:
        events, truncated = _list("events", "event", search, max_results)
        client = get_client()
        clusters, hosts = client.names("clusters", "cluster"), client.names("hosts", "host")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    return {
        "count": len(events),
        "truncated": truncated,
        "events": [summarize_event(e, clusters, hosts) for e in events],
    }


@mcp.tool(annotations=READ_ONLY)
def list_snapshots(vm_name_or_id: str) -> dict[str, Any]:
    """List the snapshots of one virtual machine.

    Args:
        vm_name_or_id: The VM's exact name, or its UUID.
    """
    client = get_client()
    try:
        vm = _resolve_vm(client, vm_name_or_id.strip())
        snaps = client.list(f"vms/{vm['id']}/snapshots", "snapshot")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    # Every VM has an "active" snapshot that stands for its current state; it isn't a real snapshot.
    snaps = [s for s in snaps if s.get("snapshot_type") != "active"]
    return {
        "vm": vm.get("name"),
        "count": len(snaps),
        "snapshots": [summarize_snapshot(s) for s in snaps],
    }


@mcp.tool(annotations=READ_ONLY)
def list_storage_domains(search: str = "", max_results: int = 50) -> dict[str, Any]:
    """List storage domains with their type, status, capacity and free space.

    Args:
        search: Optional oVirt search query, for example `type=data`,
            `name=nfs*`, or `status=unattached`. Leave empty to list all.
        max_results: Maximum number of storage domains to return (1-200, default 50).
    """
    try:
        domains, truncated = _list("storagedomains", "storage_domain", search, max_results)
    except OlvmError as e:
        raise ToolError(str(e)) from e
    return {
        "count": len(domains),
        "truncated": truncated,
        "storage_domains": [summarize_storage_domain(sd) for sd in domains],
    }


@mcp.tool(annotations=READ_ONLY)
def get_job_status(job_id: str = "", max_results: int = 20) -> dict[str, Any]:
    """Get the status of an engine job, or list recent jobs.

    Long-running operations (starting or migrating a VM, creating a snapshot)
    run as engine jobs. The engine clears finished jobs after a while.

    Args:
        job_id: A job's UUID. Returns that job with its steps. Leave empty to
            list recent jobs, newest first.
        max_results: When listing, the maximum number of jobs (1-200, default 20).
    """
    client = get_client()
    job_id = job_id.strip()
    try:
        if job_id:
            if not _UUID.match(job_id):
                raise ToolError(f"{job_id!r} is not a job id. Call get_job_status without a job_id to list jobs.")
            try:
                job = client.get(f"jobs/{job_id}")
            except OlvmNotFound:
                raise ToolError(f"No job with id {job_id}. The engine may have cleared it.") from None
            return summarize_job(job, client.list(f"jobs/{job_id}/steps", "step"))

        jobs = client.list("jobs", "job")
    except OlvmError as e:
        raise ToolError(str(e)) from e
    jobs.sort(key=lambda j: int(j.get("start_time") or 0), reverse=True)
    limit = _clamp(max_results)
    return {
        "count": min(len(jobs), limit),
        "truncated": len(jobs) > limit,
        "jobs": [summarize_job(j) for j in jobs[:limit]],
    }


def main() -> None:
    # stdout carries the MCP protocol; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        mode = mode_from_env()
    except ConfigError as e:
        log.error("%s; write tools stay disabled", e)
        mode = READ_ONLY
    if mode == OPERATOR:
        from . import actions  # imports this module, so load it only when needed
        actions.register(mcp)
        log.info("Operator mode: write tools enabled")
    mcp.run()


if __name__ == "__main__":
    main()
