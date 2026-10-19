"""Capacity report agent: inventory through MCP, exact figures in code, analysis by Claude.

    uv run olvm-capacity-report                 # local MCP server over stdio (needs OLVM_* settings)
    uv run olvm-capacity-report --no-llm        # figures only: no API key, no cost

Steps:
1. Collect the inventory with the MCP server's read tools.
2. Compute capacity figures and rule-based findings (capacity_metrics.py).
3. Ask Claude for a summary, ranked risks and recommendations. Claude sees the
   figures and may call the read-only MCP tools to look closer, up to a limit.
4. Write a Markdown report. The tables come from code, never from the model.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession

from .capacity_metrics import compute
from .llm import (UNTRUSTED_DATA, Usage, add_common_args, as_json, read_secret, recent, run_model, source_label,
                  table, write_report)
from .mcp_link import ToolCallError, call_json, connect

DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_EVENT_DAYS = 7

SYSTEM_PROMPT = """You are a capacity analyst for an Oracle Linux Virtualization Manager (OLVM) / oVirt environment.

You receive capacity figures that code computed from the engine's live inventory, plus rule-based findings. Treat the figures as exact: never recompute or estimate numbers yourself, and quote them from the data when you cite them.

You may call the read-only tools to look closer, for example at recent events for a host near its limits or at one VM's details. Use them only when the answer could change your assessment; you have at most {max_tool_calls} tool calls.

""" + UNTRUSTED_DATA + """

Write Markdown with exactly these three sections and nothing before or after them:

### Summary
Two to four sentences on overall health and headroom.

### Risks
Ranked, most serious first. For each: severity (critical, warning or info), the evidence, and why it matters. Write "None found." if there are none.

### Recommendations
Concrete actions in OLVM terms, each tied to a risk above.

Be concise. If the environment is small or the data can't support a conclusion, say so instead of guessing."""


# -- 1. collect -----------------------------------------------------------------

async def collect(session: ClientSession, since: datetime) -> dict[str, Any]:
    hosts = await call_json(session, "list_hosts", {"max_results": 200})
    vms = await call_json(session, "list_vms", {"max_results": 200})
    storage = await call_json(session, "list_storage_domains", {"max_results": 200})
    # Newest first; the window below drops anything older than `since`.
    events = await call_json(session, "list_events", {"search": "severity>normal", "max_results": 100})
    return {"hosts": hosts.get("hosts", []), "vms": vms.get("vms", []),
            "storage_domains": storage.get("storage_domains", []),
            "events": recent(events.get("events", []), since),
            "truncated": any(r.get("truncated") for r in (hosts, vms, storage))}


# -- 3. analyze -----------------------------------------------------------------

async def analyze(session: ClientSession, metrics: dict[str, Any], *, api_key: str | None, model: str,
                  effort: str, max_tool_calls: int, usage: Usage, client: Any = None) -> str:
    """Run Claude over the figures, letting it call read-only tools. Returns Markdown."""
    return await run_model(
        session, system=SYSTEM_PROMPT.format(max_tool_calls=max_tool_calls),
        prompt="Here are the capacity figures and findings, as JSON:\n\n" + as_json(metrics),
        api_key=api_key, model=model, effort=effort, max_tool_calls=max_tool_calls, usage=usage, client=client)


# -- 4. report --------------------------------------------------------------------

def _n_plus_1_cell(n1: dict[str, Any]) -> str:
    if "margin_gib" in n1:
        return f"{'ok' if n1['ok'] else 'not ok'} (margin {n1['margin_gib']} GiB)"
    return f"not ok ({n1.get('reason', 'unknown')})"


def render(metrics: dict[str, Any], analysis: str, *, generated: datetime, source: str,
           usage: Usage | None, truncated: bool, event_days: int = DEFAULT_EVENT_DAYS) -> str:
    t = metrics["totals"]
    parts = [
        f"# OLVM capacity report, {generated:%Y-%m-%d}",
        "",
        f"Generated {generated:%Y-%m-%d %H:%M} UTC from {source}. "
        f"{t['hosts_up']} of {t['hosts']} hosts up, {t['vms_running']} of {t['vms']} VMs running, "
        f"{t['storage_domains']} data storage domain(s).",
        "",
    ]
    if truncated:
        parts += ["> **Note:** the inventory hit the 200-item limit, so some objects are missing.", ""]
    parts += ["## Analysis", "", analysis.strip(), "",
              "## Findings (rule-based)", "",
              table(["Severity", "Finding"], [[f["severity"], f["finding"]] for f in metrics["findings"]]),
              "## Clusters", "",
              table(["Cluster", "Hosts up", "CPUs", "vCPUs", "vCPU/CPU", "Memory GiB", "VM memory GiB",
                     "VM memory %", "N+1"],
                    [[c["name"], f"{c['hosts_up']}/{c['hosts_total']}", c["cpus"], c["vcpus_assigned"],
                      c["vcpu_ratio"], c["memory_gib"], c["vm_memory_gib"], c["vm_memory_pct"],
                      _n_plus_1_cell(c["n_plus_1"])] for c in metrics["clusters"]]),
              "## Hosts", "",
              table(["Host", "Status", "Cluster", "CPUs", "vCPUs", "vCPU/CPU", "Memory GiB",
                     "Schedulable GiB", "VM memory GiB", "VM memory %", "SPM", "Running VMs"],
                    [[h["name"], h["status"], h["cluster"], h["cpus"], h["vcpus_assigned"], h["vcpu_ratio"],
                      h["memory_gib"], h["schedulable_gib"], h["vm_memory_gib"], h["vm_memory_pct"],
                      h["spm"], h["running_vms"]] for h in metrics["hosts"]]),
              "## Storage", "",
              table(["Domain", "Status", "Total GiB", "Used GiB", "Free GiB", "Used %", "Committed GiB",
                     "Committed %"],
                    [[s["name"], s["status"], s["total_gib"], s["used_gib"], s["available_gib"], s["used_pct"],
                      s["committed_gib"], s["committed_pct"]] for s in metrics["storage"]]),
              "## VMs", "",
              table(["VM", "Status", "Cluster", "Host", "vCPUs", "Memory GiB"],
                    [[v["name"], v["status"], v["cluster"], v["host"], v["cpus"], v["memory_gib"]]
                     for v in metrics["vms"]]),
              f"## Warnings and errors in the last {event_days} day(s)", "",
              table(["Time", "Severity", "Event"],
                    [[e.get("time"), e.get("severity"), e.get("description")] for e in metrics["events"]])]
    if usage is not None:
        parts += usage.footer()
    return "\n".join(parts) + "\n"


# -- command line -----------------------------------------------------------------

async def run(args: argparse.Namespace) -> Path:
    generated = datetime.now(tz=UTC)
    token = read_secret(args.remote_token_file)
    async with connect(args.remote, token, args.remote_ca_file) as session:
        inventory = await collect(session, since=generated - timedelta(days=args.event_days))
        metrics = compute(inventory)
        usage = None
        if args.no_llm:
            analysis = "_Skipped (`--no-llm`): figures and rule-based findings only._"
        else:
            usage = Usage(model=args.model)
            try:
                analysis = await analyze(session, metrics, api_key=read_secret(args.api_key_file),
                                         model=args.model, effort=args.effort,
                                         max_tool_calls=args.max_tool_calls, usage=usage)
            except Exception as e:  # the figures are still worth writing
                analysis = f"_Analysis unavailable: {type(e).__name__}: {e}_"
    report = render(metrics, analysis, generated=generated, source=source_label(args), usage=usage,
                    truncated=inventory["truncated"], event_days=args.event_days)
    path = write_report(args.output_dir, "capacity", generated, report)
    if usage is not None and usage.requests:
        print(f"Model: {usage.summary()}", file=sys.stderr)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="olvm-capacity-report",
                                description="Write an OLVM capacity report from the olvm-mcp server.")
    add_common_args(p, max_tool_calls=DEFAULT_MAX_TOOL_CALLS)
    p.add_argument("--event-days", type=int, default=DEFAULT_EVENT_DAYS,
                   help=f"include warning and error events from this many days (default: {DEFAULT_EVENT_DAYS})")
    args = p.parse_args(argv)
    try:
        path = asyncio.run(run(args))
    except (ToolCallError, OSError) as e:
        sys.exit(f"Error: {e}")
    print(path)


if __name__ == "__main__":
    main()
