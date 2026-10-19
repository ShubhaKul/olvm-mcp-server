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
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession

from .capacity_metrics import compute
from .mcp_link import ToolCallError, call_json, connect, result_text

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_EVENT_DAYS = 7
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# List prices per million tokens (input, output), for the cost line in the report.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}

SYSTEM_PROMPT = """You are a capacity analyst for an Oracle Linux Virtualization Manager (OLVM) / oVirt environment.

You receive capacity figures that code computed from the engine's live inventory, plus rule-based findings. Treat the figures as exact: never recompute or estimate numbers yourself, and quote them from the data when you cite them.

You may call the read-only tools to look closer, for example at recent events for a host near its limits or at one VM's details. Use them only when the answer could change your assessment; you have at most {max_tool_calls} tool calls.

Write Markdown with exactly these three sections and nothing before or after them:

### Summary
Two to four sentences on overall health and headroom.

### Risks
Ranked, most serious first. For each: severity (critical, warning or info), the evidence, and why it matters. Write "None found." if there are none.

### Recommendations
Concrete actions in OLVM terms, each tied to a risk above.

Be concise. If the environment is small or the data can't support a conclusion, say so instead of guessing."""


@dataclass
class Usage:
    model: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    notes: list[str] = field(default_factory=list)

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self) -> float | None:
        if self.model not in PRICES:
            return None
        price_in, price_out = PRICES[self.model]
        tokens_in = self.input_tokens + 1.25 * self.cache_write_tokens + 0.1 * self.cache_read_tokens
        return (tokens_in * price_in + self.output_tokens * price_out) / 1_000_000


# -- 1. collect -----------------------------------------------------------------

def recent(events: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    """Events at or after `since`. Events without a readable time are kept."""
    kept = []
    for e in events:
        try:
            when = datetime.fromisoformat(e["time"])
        except (KeyError, TypeError, ValueError):
            kept.append(e)
            continue
        if when >= since:
            kept.append(e)
    return kept


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

async def analysis_tools(session: ClientSession) -> list[dict[str, Any]]:
    """The MCP server's tools that are marked read-only, as Claude API tool definitions."""
    listed = await session.list_tools()
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed.tools if t.annotations is not None and t.annotations.read_only_hint]


async def analyze(session: ClientSession, metrics: dict[str, Any], *, api_key: str | None, model: str,
                  effort: str, max_tool_calls: int, usage: Usage, client: Any = None) -> str:
    """Run Claude over the figures, letting it call read-only tools. Returns Markdown.

    `client` is an AsyncAnthropic client; one is created from `api_key` (or the
    environment) when it isn't given.
    """
    if client is None:
        import anthropic  # optional dependency, only needed with the model

        client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
    tools = await analysis_tools(session)
    allowed = {t["name"] for t in tools}
    messages: list[dict[str, Any]] = [{"role": "user", "content": (
        "Here are the capacity figures and findings, as JSON:\n\n"
        + json.dumps(metrics, sort_keys=True, indent=1))}]

    while True:
        response = await client.beta.messages.create(
            model=model, max_tokens=16000, system=SYSTEM_PROMPT.format(max_tool_calls=max_tool_calls),
            tools=tools, messages=messages, thinking={"type": "adaptive"}, output_config={"effort": effort},
            betas=[FALLBACK_BETA], fallbacks="default",
        )
        usage.add(response.usage)
        if any(block.type == "fallback" for block in response.content):
            usage.notes.append(f"A safety check declined part of the request; it was finished by {response.model}.")
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to write the analysis")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                text += "\n\n_(The analysis was cut off at the output limit.)_"
            return text

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for tu in tool_uses:
            if tu.name not in allowed:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                "content": f"Tool {tu.name} is not available to this agent."})
            elif usage.tool_calls >= max_tool_calls:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                "content": "Tool-call limit reached. Write the report now from what you have."})
            else:
                usage.tool_calls += 1
                result = await session.call_tool(tu.name, dict(tu.input))
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": result_text(result) or "(empty)", "is_error": bool(result.is_error)})
        messages.append({"role": "user", "content": results})


# -- 4. report --------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(map(str, value)) or "none"
    return str(value)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_None._\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_fmt(c).replace("|", "\\|") for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


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
              _table(["Severity", "Finding"], [[f["severity"], f["finding"]] for f in metrics["findings"]]),
              "## Clusters", "",
              _table(["Cluster", "Hosts up", "CPUs", "vCPUs", "vCPU/CPU", "Memory GiB", "VM memory GiB",
                      "VM memory %", "N+1"],
                     [[c["name"], f"{c['hosts_up']}/{c['hosts_total']}", c["cpus"], c["vcpus_assigned"],
                       c["vcpu_ratio"], c["memory_gib"], c["vm_memory_gib"], c["vm_memory_pct"],
                       _n_plus_1_cell(c["n_plus_1"])] for c in metrics["clusters"]]),
              "## Hosts", "",
              _table(["Host", "Status", "Cluster", "CPUs", "vCPUs", "vCPU/CPU", "Memory GiB",
                      "Schedulable GiB", "VM memory GiB", "VM memory %", "SPM", "Running VMs"],
                     [[h["name"], h["status"], h["cluster"], h["cpus"], h["vcpus_assigned"], h["vcpu_ratio"],
                       h["memory_gib"], h["schedulable_gib"], h["vm_memory_gib"], h["vm_memory_pct"],
                       h["spm"], h["running_vms"]] for h in metrics["hosts"]]),
              "## Storage", "",
              _table(["Domain", "Status", "Total GiB", "Used GiB", "Free GiB", "Used %", "Committed GiB",
                      "Committed %"],
                     [[s["name"], s["status"], s["total_gib"], s["used_gib"], s["available_gib"], s["used_pct"],
                       s["committed_gib"], s["committed_pct"]] for s in metrics["storage"]]),
              "## VMs", "",
              _table(["VM", "Status", "Cluster", "Host", "vCPUs", "Memory GiB"],
                     [[v["name"], v["status"], v["cluster"], v["host"], v["cpus"], v["memory_gib"]]
                      for v in metrics["vms"]]),
              f"## Warnings and errors in the last {event_days} day(s)", "",
              _table(["Time", "Severity", "Event"],
                     [[e.get("time"), e.get("severity"), e.get("description")] for e in metrics["events"]])]
    if usage is not None and usage.requests:
        cost = usage.cost()
        parts += ["---", "",
                  f"_Analysis by `{usage.model}`: {usage.requests} request(s), {usage.tool_calls} tool call(s), "
                  f"{usage.input_tokens:,} input and {usage.output_tokens:,} output tokens"
                  + (f", about ${cost:.2f}" if cost is not None else "") + "._"]
        parts += [f"_{note}_" for note in usage.notes]
    return "\n".join(parts) + "\n"


# -- command line -----------------------------------------------------------------

def _read_secret(path: str | None) -> str | None:
    return Path(path).read_text(encoding="utf-8-sig").strip() if path else None


async def run(args: argparse.Namespace) -> Path:
    generated = datetime.now(tz=UTC)
    token = _read_secret(args.remote_token_file)
    source = f"the remote server {args.remote}" if args.remote else "the local MCP server (read-only)"
    async with connect(args.remote, token, args.remote_ca_file) as session:
        inventory = await collect(session, since=generated - timedelta(days=args.event_days))
        metrics = compute(inventory)
        usage = None
        if args.no_llm:
            analysis = "_Skipped (`--no-llm`): figures and rule-based findings only._"
        else:
            usage = Usage(model=args.model)
            try:
                analysis = await analyze(session, metrics, api_key=_read_secret(args.api_key_file),
                                         model=args.model, effort=args.effort,
                                         max_tool_calls=args.max_tool_calls, usage=usage)
            except Exception as e:  # the figures are still worth writing
                analysis = f"_Analysis unavailable: {type(e).__name__}: {e}_"
    report = render(metrics, analysis, generated=generated, source=source, usage=usage,
                    truncated=inventory["truncated"], event_days=args.event_days)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"capacity-{generated:%Y-%m-%d-%H%M}.md"
    path.write_text(report, encoding="utf-8")
    if usage is not None and usage.requests:
        cost = usage.cost()
        print(f"Model: {usage.requests} request(s), {usage.tool_calls} tool call(s)"
              + (f", about ${cost:.2f}" if cost is not None else ""), file=sys.stderr)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="olvm-capacity-report",
                                description="Write an OLVM capacity report from the olvm-mcp server.")
    p.add_argument("--output-dir", default="reports", help="where to write the report (default: reports)")
    p.add_argument("--no-llm", action="store_true", help="figures and rule-based findings only; no API call")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    p.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"],
                   help=f"how hard the model thinks (default: {DEFAULT_EFFORT})")
    p.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS,
                   help=f"read-only tool calls the model may make (default: {DEFAULT_MAX_TOOL_CALLS})")
    p.add_argument("--api-key-file", default=os.environ.get("OLVM_AGENT_API_KEY_FILE"),
                   help="file with the Anthropic API key (default: $OLVM_AGENT_API_KEY_FILE, "
                        "else ANTHROPIC_API_KEY)")
    p.add_argument("--event-days", type=int, default=DEFAULT_EVENT_DAYS,
                   help=f"include warning and error events from this many days (default: {DEFAULT_EVENT_DAYS})")
    p.add_argument("--remote", help="URL of a remote olvm-mcp server, e.g. https://host/mcp (default: local stdio)")
    p.add_argument("--remote-token-file", help="file with the bearer token for --remote")
    p.add_argument("--remote-ca-file", help="CA certificate that signed the remote server's certificate")
    args = p.parse_args(argv)
    try:
        path = asyncio.run(run(args))
    except (ToolCallError, OSError) as e:
        sys.exit(f"Error: {e}")
    print(path)


if __name__ == "__main__":
    main()
