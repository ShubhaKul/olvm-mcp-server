"""Turn raw engine JSON into compact summaries for the model.

The engine's JSON encodes most numbers and booleans as strings ("4294967296",
"true"), so every field goes through a small converter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

GIB = 1024**3


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _gib(value: Any) -> float | None:
    n = _int(value)
    return round(n / GIB, 2) if n is not None else None


def _timestamp(value: Any) -> str | None:
    ms = _int(value)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


def _cpu_count(obj: dict[str, Any]) -> int | None:
    topo = (obj.get("cpu") or {}).get("topology") or {}
    parts = [_int(topo.get(k)) for k in ("sockets", "cores", "threads")]
    if any(p is None for p in parts):
        return None
    return parts[0] * parts[1] * parts[2]


def _ref_name(obj: dict[str, Any], field: str, names: dict[str, str]) -> str | None:
    ref = obj.get(field) or {}
    ref_id = ref.get("id")
    return names.get(ref_id, ref_id) if ref_id else None


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def summarize_vm(vm: dict[str, Any], clusters: dict[str, str], hosts: dict[str, str]) -> dict[str, Any]:
    return _drop_empty({
        "id": vm.get("id"),
        "name": vm.get("name"),
        "status": vm.get("status"),
        "cluster": _ref_name(vm, "cluster", clusters),
        "host": _ref_name(vm, "host", hosts),
        "cpus": _cpu_count(vm),
        "memory_gib": _gib(vm.get("memory")),
        "os": (vm.get("os") or {}).get("type"),
        "fqdn": vm.get("fqdn"),
        "description": vm.get("description"),
    })


def detail_vm(vm: dict[str, Any], clusters: dict[str, str], hosts: dict[str, str],
              disks: list[dict[str, Any]], nics: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_vm(vm, clusters, hosts)
    summary.update(_drop_empty({
        "comment": vm.get("comment"),
        "type": vm.get("type"),
        "guaranteed_memory_gib": _gib((vm.get("memory_policy") or {}).get("guaranteed")),
        "high_availability": _bool((vm.get("high_availability") or {}).get("enabled")),
        "stateless": _bool(vm.get("stateless")),
        "created": _timestamp(vm.get("creation_time")),
        "started": _timestamp(vm.get("start_time")),
        "stopped": _timestamp(vm.get("stop_time")),
        "disks": [_summarize_disk_attachment(d) for d in disks],
        "nics": [_summarize_nic(n) for n in nics],
    }))
    return summary


def _summarize_disk_attachment(att: dict[str, Any]) -> dict[str, Any]:
    disk = att.get("disk") or {}
    return _drop_empty({
        "name": disk.get("alias") or disk.get("name") or disk.get("id"),
        "size_gib": _gib(disk.get("provisioned_size")),
        "format": disk.get("format"),
        "status": disk.get("status"),
        "interface": att.get("interface"),
        "bootable": _bool(att.get("bootable")),
        "active": _bool(att.get("active")),
    })


def _summarize_nic(nic: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty({
        "name": nic.get("name"),
        "interface": nic.get("interface"),
        "mac": (nic.get("mac") or {}).get("address"),
        "plugged": _bool(nic.get("plugged")),
        "linked": _bool(nic.get("linked")),
    })


def summarize_host(host: dict[str, Any], clusters: dict[str, str]) -> dict[str, Any]:
    os_info = host.get("os") or {}
    os_version = (os_info.get("version") or {}).get("full_version")
    summary = host.get("summary") or {}
    return _drop_empty({
        "id": host.get("id"),
        "name": host.get("name"),
        "address": host.get("address"),
        "status": host.get("status"),
        "cluster": _ref_name(host, "cluster", clusters),
        "cpu_model": (host.get("cpu") or {}).get("name"),
        "cpus": _cpu_count(host),
        "memory_gib": _gib(host.get("memory")),
        "schedulable_memory_gib": _gib(host.get("max_scheduling_memory")),
        "vms_running": _int(summary.get("active")),
        "os": " ".join(p for p in (os_info.get("type"), os_version) if p) or None,
        "vdsm_version": (host.get("version") or {}).get("full_version"),
        "spm": (host.get("spm") or {}).get("status"),
    })
