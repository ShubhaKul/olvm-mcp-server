"""Triage agent: investigate a reported problem with read-only tools and write a root-cause report.

    uv run olvm-triage "vm-test is down" --vm vm-test
    uv run olvm-triage "errors on host kvm1 since this morning" --host kvm1 --hours 12
    uv run olvm-triage "vm-test is down" --vm vm-test --no-llm     # evidence only: no API key, no cost

Steps:
1. Collect a baseline in code: hosts, VMs, storage domains, recent jobs, and the
   warnings and errors in the time window; for any VM or host named with
   --vm / --host, its details and all of its events in the window.
2. Ask Claude to investigate from the problem description and the baseline. It
   may follow leads with the read-only MCP tools, up to a limit. It never acts:
   write tools are not offered, and next steps are suggestions for a person.
3. Write a Markdown report: the analysis, then evidence tables built in code
   (objects involved, event timeline, jobs) to check the analysis against.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession

from .llm import (UNTRUSTED_DATA, Usage, add_common_args, as_json, read_secret, recent, run_model, source_label,
                  table, write_report)
from .mcp_link import ToolCallError, call_json, connect

DEFAULT_MAX_TOOL_CALLS = 15
DEFAULT_HOURS = 24
EVENT_LIMIT = 200
TIMELINE_ROWS = 60
REPEAT_THRESHOLD = 3  # routine events repeated this often are shown once, with a count

SYSTEM_PROMPT = """You are a triage engineer for an Oracle Linux Virtualization Manager (OLVM) / oVirt environment. An operator has reported a problem. Find the most likely cause and say what to do next.

You receive the operator's description and a baseline that code collected from the engine: hosts, VMs, storage domains, recent jobs and the warnings and errors in the time window, plus details and all events of any VM or host the operator named. Routine events of other objects aren't included; look them up if you need them. Routine events that repeat are folded into the newest one: `repeated` says how many there were and `first_time` when the first was.

Investigate with the read-only tools when the baseline isn't enough: events for one object (list_events with a search such as `vm.name=NAME` or `host.name=NAME`, or a wider window), get_vm for a VM's disks, NICs and start and stop times, list_snapshots, and get_job_status for a job's steps. Events that share a correlation_id belong to the same operation; event text often names the user who started it. Correlation ids that start with `olvm-mcp-` come from operations requested through the olvm-mcp server, whose audit log (JSON Lines, path set by OLVM_AUDIT_LOG) records the client that asked for each one. You have at most {max_tool_calls} tool calls; stop when further calls wouldn't change your conclusion.

You cannot change anything, and you must not claim to have done so. Your next steps are for a person to carry out.

""" + UNTRUSTED_DATA + """

Base every claim on the data. Cite events by id and time, for example "event 1234 at 2026-10-19T14:02". If the evidence is thin, say so and lower your confidence rather than guessing. When the cause is likely outside what the engine records (the guest OS, the network, storage hardware), say which logs to check, for example /var/log/vdsm/vdsm.log on the host or /var/log/ovirt-engine/engine.log on the engine.

Write Markdown with exactly these four sections and nothing before or after them:

### Summary
Two or three sentences: what happened and the most likely cause.

### Likely cause
The cause, a confidence level (high, medium or low) and the evidence for it.

### Other possibilities
Other explanations you considered, and why each is less likely or couldn't be ruled out. Write "None." if there are none.

### Suggested next steps
Numbered, most useful first. Concrete actions in OLVM terms (or commands to run on a host), each with what it would confirm or fix."""


# -- 1. collect -----------------------------------------------------------------

def _match_host(hosts: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    w = wanted.strip().lower()
    for h in hosts:
        if w in {str(h.get("name", "")).lower(), str(h.get("address", "")).lower(), str(h.get("id", "")).lower()}:
            return h
    return None


async def collect(session: ClientSession, since: datetime, vm_names: list[str],
                  host_names: list[str]) -> dict[str, Any]:
    hosts = (await call_json(session, "list_hosts", {"max_results": 200})).get("hosts", [])
    vms = (await call_json(session, "list_vms", {"max_results": 200})).get("vms", [])
    storage = (await call_json(session, "list_storage_domains", {"max_results": 200})).get("storage_domains", [])
    jobs = await call_json(session, "get_job_status", {"max_results": 50})

    named_vms, missing = [], []
    for name in vm_names:
        try:
            named_vms.append(await call_json(session, "get_vm", {"name_or_id": name}))
        except ToolCallError as e:
            missing.append(f"VM {name}: {e}")
    named_hosts = []
    for name in host_names:
        host = _match_host(hosts, name)
        if host is None:
            missing.append(f"Host {name}: not found")
        else:
            named_hosts.append(host)

    # Targeted searches rather than every event: hosts log dozens of routine
    # events a day (update checks), which would push older problems out.
    searches = (["severity>normal"] + [f"vm.name={v['name']}" for v in named_vms]
                + [f"host.name={h['name']}" for h in named_hosts])
    events: dict[str, dict[str, Any]] = {}
    events_cut = False
    for search in searches:
        found = await call_json(session, "list_events", {"search": search, "max_results": EVENT_LIMIT})
        listed = found.get("events", [])
        window = recent(listed, since)
        # Newest first: if the list hit the limit and even its oldest event is
        # inside the window, part of the window is missing.
        events_cut |= bool(found.get("truncated")) and len(window) == len(listed)
        events.update((e.get("id") or str(id(e)), e) for e in window)

    return {"since": since.isoformat(timespec="minutes"), "hosts": hosts, "vms": vms, "storage_domains": storage,
            "events": condense(sorted(events.values(), key=_event_order)), "events_cut": events_cut,
            "jobs": recent(jobs.get("jobs", []), since, key="started"),
            "named_vms": named_vms, "named_hosts": named_hosts, "not_found": missing}


def _event_order(e: dict[str, Any]) -> tuple[str, int]:
    """Oldest first; events logged in the same second by id."""
    eid = str(e.get("id") or "")
    return e.get("time") or "", int(eid) if eid.isdigit() else 0


def condense(events: list[dict[str, Any]], min_repeat: int = REPEAT_THRESHOLD) -> list[dict[str, Any]]:
    """Fold routine events that repeat (same code, host and VM) into their newest one.

    Hosts log dozens of normal-severity events a day for routine jobs such as
    update checks, which would bury the events that matter. The kept event
    gets `repeated` (how many there were) and `first_time`. Warnings, errors
    and events that don't repeat are kept as they are. Input and output are
    oldest first.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for e in events:
        if e.get("severity") == "normal" and e.get("code") is not None:
            groups.setdefault((e.get("code"), e.get("host"), e.get("vm_id")), []).append(e)
    folded = {id(e) for g in groups.values() if len(g) >= min_repeat for e in g[:-1]}
    out = []
    for e in events:
        if id(e) in folded:
            continue
        group = groups.get((e.get("code"), e.get("host"), e.get("vm_id"))) if e.get("severity") == "normal" else None
        if group and len(group) >= min_repeat:
            e = {**e, "repeated": len(group), "first_time": group[0].get("time")}
        out.append(e)
    return out


# -- 2. analyze -----------------------------------------------------------------

def model_input(problem: str, baseline: dict[str, Any], hours: int) -> str:
    return (f"Problem reported by the operator:\n\n{problem}\n\n"
            f"Time window: the last {hours} hour(s), since {baseline['since']} UTC.\n\n"
            f"Baseline collected from the engine, as JSON:\n\n{as_json(baseline)}")


async def analyze(session: ClientSession, problem: str, baseline: dict[str, Any], hours: int, *,
                  api_key: str | None, model: str, effort: str, max_tool_calls: int, usage: Usage,
                  client: Any = None) -> str:
    return await run_model(
        session, system=SYSTEM_PROMPT.format(max_tool_calls=max_tool_calls),
        prompt=model_input(problem, baseline, hours), api_key=api_key, model=model, effort=effort,
        max_tool_calls=max_tool_calls, usage=usage, client=client)


# -- 3. report --------------------------------------------------------------------

def _short(text: Any, limit: int = 300) -> Any:
    """Long event text (package lists, say) cut down for a table cell."""
    if isinstance(text, str) and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _event_text(e: dict[str, Any]) -> Any:
    text = _short(e.get("description"))
    if e.get("repeated"):
        text = f"{text} (×{e['repeated']} similar since {e.get('first_time')})"
    return text


def render(problem: str, baseline: dict[str, Any], analysis: str, *, generated: datetime, source: str,
           hours: int, usage: Usage | None) -> str:
    vm_names = {v.get("id"): v.get("name") for v in baseline["vms"]}
    events = baseline["events"]
    parts = [
        f"# OLVM triage report, {generated:%Y-%m-%d}",
        "",
        f"Generated {generated:%Y-%m-%d %H:%M} UTC from {source}, "
        f"looking at the last {hours} hour(s).",
        "",
        "> **Problem:** " + " ".join(problem.split()),
        "",
    ]
    for item in baseline["not_found"]:
        parts += [f"> **Not found:** {item}", ""]
    if baseline["events_cut"]:
        parts += [f"> **Note:** the engine returned its {EVENT_LIMIT}-event limit, so the oldest events "
                  "in the window are missing.", ""]
    parts += ["## Analysis", "", analysis.strip(), ""]

    if baseline["named_vms"]:
        parts += ["## VMs named", "",
                  table(["VM", "Status", "Host", "Cluster", "Started", "Stopped", "HA"],
                        [[v.get("name"), v.get("status"), v.get("host"), v.get("cluster"), v.get("started"),
                          v.get("stopped"), v.get("high_availability")] for v in baseline["named_vms"]])]
    parts += ["## Hosts", "",
              table(["Host", "Address", "Status", "Cluster", "SPM", "Running VMs"],
                    [[h.get("name"), h.get("address"), h.get("status"), h.get("cluster"), h.get("spm"),
                      h.get("vms_running")] for h in baseline["hosts"]]),
              "## Storage domains", "",
              table(["Domain", "Type", "Status", "External status", "Used %", "Low space"],
                    [[s.get("name"), s.get("type"), s.get("status"), s.get("external_status"),
                      s.get("used_percent"), s.get("low_space")] for s in baseline["storage_domains"]]),
              "## VMs not running", "",
              table(["VM", "Status", "Cluster"],
                    [[v.get("name"), v.get("status"), v.get("cluster")]
                     for v in baseline["vms"] if v.get("status") != "up"]),
              "## Event timeline", "",
              "Warnings and errors in the window, plus every event for a VM or host named above, oldest first. "
              "Routine events that repeat are shown once, with a count."
              + (f" Only the newest {TIMELINE_ROWS} of {len(events)} are shown." if len(events) > TIMELINE_ROWS
                 else ""),
              "",
              table(["Time", "Severity", "Host", "VM", "Event", "Id"],
                    [[e.get("time"), e.get("severity"), e.get("host"), vm_names.get(e.get("vm_id"), e.get("vm_id")),
                      _event_text(e), e.get("id")] for e in events[-TIMELINE_ROWS:]]),
              "## Jobs in the window", "",
              table(["Started", "Ended", "Status", "Job"],
                    [[j.get("started"), j.get("ended"), j.get("status"), j.get("description")]
                     for j in baseline["jobs"]])]
    if usage is not None:
        parts += usage.footer()
    return "\n".join(parts) + "\n"


# -- command line -----------------------------------------------------------------

async def run(args: argparse.Namespace) -> Path:
    generated = datetime.now(tz=UTC)
    token = read_secret(args.remote_token_file)
    async with connect(args.remote, token, args.remote_ca_file) as session:
        baseline = await collect(session, generated - timedelta(hours=args.hours), args.vm, args.host)
        usage = None
        if args.no_llm:
            analysis = "_Skipped (`--no-llm`): evidence only._"
        else:
            usage = Usage(model=args.model)
            try:
                analysis = await analyze(session, args.problem, baseline, args.hours,
                                         api_key=read_secret(args.api_key_file), model=args.model,
                                         effort=args.effort, max_tool_calls=args.max_tool_calls, usage=usage)
            except Exception as e:  # the evidence is still worth writing
                analysis = f"_Analysis unavailable: {type(e).__name__}: {e}_"
    report = render(args.problem, baseline, analysis, generated=generated, source=source_label(args),
                    hours=args.hours, usage=usage)
    path = write_report(args.output_dir, "triage", generated, report)
    if usage is not None and usage.requests:
        print(f"Model: {usage.summary()}", file=sys.stderr)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="olvm-triage",
                                description="Investigate an OLVM problem with read-only tools and write a report.")
    p.add_argument("problem", help='what is wrong, in your own words, e.g. "vm-test is down"')
    p.add_argument("--vm", action="append", default=[], help="a VM involved (name or id); repeat for several")
    p.add_argument("--host", action="append", default=[],
                   help="a host involved (name, address or id); repeat for several")
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                   help=f"how far back to look at events and jobs (default: {DEFAULT_HOURS})")
    add_common_args(p, max_tool_calls=DEFAULT_MAX_TOOL_CALLS)
    args = p.parse_args(argv)
    try:
        path = asyncio.run(run(args))
    except (ToolCallError, OSError) as e:
        sys.exit(f"Error: {e}")
    print(path)


if __name__ == "__main__":
    main()
