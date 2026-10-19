"""Capacity figures computed from the engine's inventory. Pure functions, no I/O.

Every number in the capacity report comes from here, not from the model, so the
report's figures are exact and repeatable.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# VM states in which a VM holds memory and CPU on a host.
RUNNING_STATES = {"up", "powering_up", "powering_down", "migrating", "paused",
                  "reboot_in_progress", "saving_state", "restoring_state", "wait_for_launch"}

STORAGE_WARN_PCT = 80.0
STORAGE_CRITICAL_PCT = 90.0
HOST_MEMORY_WARN_PCT = 90.0
VCPU_RATIO_WARN = 4.0
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ratio(part: float, whole: float) -> float | None:
    return round(part / whole, 2) if whole else None


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def compute(inventory: dict[str, Any]) -> dict[str, Any]:
    """Turn {"hosts": [...], "vms": [...], "storage_domains": [...], "events": [...]}
    (the summaries returned by the MCP read tools) into capacity figures and findings."""
    hosts = inventory.get("hosts", [])
    vms = inventory.get("vms", [])
    domains = inventory.get("storage_domains", [])
    events = inventory.get("events", [])

    running = [vm for vm in vms if vm.get("status") in RUNNING_STATES]
    by_host: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for vm in running:
        if vm.get("host"):
            by_host[vm["host"]].append(vm)

    host_rows = []
    for h in hosts:
        on_host = by_host.get(h.get("name"), [])
        cpus, mem = _num(h.get("cpus")), _num(h.get("memory_gib"))
        vcpus = sum(_num(vm.get("cpus")) for vm in on_host)
        vm_mem = round(sum(_num(vm.get("memory_gib")) for vm in on_host), 2)
        host_rows.append({
            "name": h.get("name"), "status": h.get("status"), "cluster": h.get("cluster"),
            "cpus": int(cpus), "memory_gib": mem, "schedulable_gib": _num(h.get("schedulable_memory_gib")),
            "running_vms": sorted(vm.get("name") for vm in on_host),
            "vcpus_assigned": int(vcpus), "vcpu_ratio": _ratio(vcpus, cpus),
            "vm_memory_gib": vm_mem, "vm_memory_pct": _pct(vm_mem, mem),
            "spm": h.get("spm") == "spm",
        })

    clusters = []
    for name in sorted({h["cluster"] for h in host_rows if h["cluster"]}):
        members = [h for h in host_rows if h["cluster"] == name]
        up = [h for h in members if h["status"] == "up"]
        cpus = sum(h["cpus"] for h in up)
        mem = round(sum(h["memory_gib"] for h in up), 2)
        vcpus = sum(h["vcpus_assigned"] for h in up)
        vm_mem = round(sum(h["vm_memory_gib"] for h in up), 2)
        clusters.append({
            "name": name, "hosts_total": len(members), "hosts_up": len(up),
            "cpus": cpus, "memory_gib": mem, "vcpus_assigned": vcpus, "vcpu_ratio": _ratio(vcpus, cpus),
            "vm_memory_gib": vm_mem, "vm_memory_pct": _pct(vm_mem, mem),
            "n_plus_1": _n_plus_1(up),
        })

    storage_rows = []
    for d in domains:
        if d.get("type") != "data":
            continue
        total = _num(d.get("total_gib"))
        storage_rows.append({
            "name": d.get("name"), "status": d.get("status") or d.get("external_status"),
            "total_gib": total, "used_gib": _num(d.get("used_gib")),
            "available_gib": _num(d.get("available_gib")), "used_pct": _num(d.get("used_percent")),
            "committed_gib": _num(d.get("committed_gib")),
            "committed_pct": _pct(_num(d.get("committed_gib")), total),
            "low_space": bool(d.get("low_space")),
        })

    vm_rows = sorted(({"name": vm.get("name"), "status": vm.get("status"), "cluster": vm.get("cluster"),
                       "host": vm.get("host"), "cpus": int(_num(vm.get("cpus"))),
                       "memory_gib": _num(vm.get("memory_gib"))} for vm in vms),
                     key=lambda v: (v["status"] not in RUNNING_STATES, v["name"] or ""))

    totals = {
        "hosts": len(hosts), "hosts_up": sum(1 for h in host_rows if h["status"] == "up"),
        "vms": len(vms), "vms_running": len(running),
        "storage_domains": len(storage_rows),
        "warning_or_error_events": len(events),
    }
    metrics = {"totals": totals, "hosts": host_rows, "clusters": clusters,
               "storage": storage_rows, "vms": vm_rows, "events": events}
    metrics["findings"] = findings(metrics)
    return metrics


def _n_plus_1(up_hosts: list[dict[str, Any]]) -> dict[str, Any]:
    """Can the running VMs of any one host restart on the other up hosts?

    For each host, the memory of its running VMs is compared with the free
    schedulable memory of all the other up hosts in the cluster. The result is
    the tightest case: margin_gib is the smallest spare-minus-needed over all
    hosts (negative when some host's VMs wouldn't fit).
    """
    if not up_hosts:
        return {"ok": False, "reason": "no host is up"}
    if len(up_hosts) == 1:
        h = up_hosts[0]
        return {"ok": not h["running_vms"], "worst_host": h["name"], "needed_gib": h["vm_memory_gib"],
                "spare_gib": 0.0, "reason": "only one host is up"}
    worst = None
    for h in up_hosts:
        spare = round(sum(o["schedulable_gib"] for o in up_hosts if o is not h), 2)
        gap = h["vm_memory_gib"] - spare
        if worst is None or gap > worst["gap"]:
            worst = {"worst_host": h["name"], "needed_gib": h["vm_memory_gib"], "spare_gib": spare, "gap": gap}
    return {"ok": worst["gap"] <= 0, "worst_host": worst["worst_host"],
            "needed_gib": worst["needed_gib"], "spare_gib": worst["spare_gib"],
            "margin_gib": round(-worst["gap"], 2)}


def findings(m: dict[str, Any]) -> list[dict[str, str]]:
    """Rule-based findings, most severe first."""
    out: list[dict[str, str]] = []

    def add(severity: str, text: str) -> None:
        out.append({"severity": severity, "finding": text})

    for h in m["hosts"]:
        if h["status"] != "up":
            add("critical", f"Host {h['name']} is {h['status']}.")
        if h["vm_memory_pct"] is not None and h["vm_memory_pct"] >= HOST_MEMORY_WARN_PCT:
            add("warning", f"Host {h['name']}: running VMs use {h['vm_memory_pct']}% of its memory.")
        if h["vcpu_ratio"] is not None and h["vcpu_ratio"] > VCPU_RATIO_WARN:
            add("warning", f"Host {h['name']}: {h['vcpu_ratio']} vCPUs per CPU (above {VCPU_RATIO_WARN}).")
    for c in m["clusters"]:
        n1 = c["n_plus_1"]
        if not n1["ok"]:
            if n1.get("reason") == "only one host is up":
                add("warning", f"Cluster {c['name']}: only one host is up, so its running VMs have "
                               f"nowhere to restart if it fails.")
            elif n1.get("reason"):
                add("critical", f"Cluster {c['name']}: {n1['reason']}.")
            else:
                add("warning", f"Cluster {c['name']} is not N+1: if {n1['worst_host']} failed, its VMs "
                               f"need {n1['needed_gib']} GiB but the other hosts can schedule "
                               f"{n1['spare_gib']} GiB.")
    for s in m["storage"]:
        if s["used_pct"] >= STORAGE_CRITICAL_PCT or s["low_space"]:
            add("critical", f"Storage domain {s['name']} is {s['used_pct']}% used "
                            f"({s['available_gib']} GiB free).")
        elif s["used_pct"] >= STORAGE_WARN_PCT:
            add("warning", f"Storage domain {s['name']} is {s['used_pct']}% used.")
        if s["committed_pct"] is not None and s["committed_pct"] > 100:
            add("info", f"Storage domain {s['name']}: thin disks could grow to {s['committed_pct']}% "
                        f"of its size ({s['committed_gib']} of {s['total_gib']} GiB committed).")
    stopped = [v["name"] for v in m["vms"] if v["status"] not in RUNNING_STATES]
    if stopped:
        add("info", f"{len(stopped)} VM(s) not running: {', '.join(stopped)}.")
    if m["events"]:
        add("info", f"{len(m['events'])} warning or error event(s) in the reporting window.")
    return sorted(out, key=lambda f: SEVERITY_ORDER[f["severity"]])
