import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from olvm_mcp.agents import capacity
from olvm_mcp.agents.capacity_metrics import compute

GIB = 1.0


def host(name, status="up", cpus=4, mem=24.0, sched=20.0, spm="none", cluster="Default"):
    return {"name": name, "status": status, "cluster": cluster, "cpus": cpus, "memory_gib": mem,
            "schedulable_memory_gib": sched, "spm": spm}


def vm(name, host_name=None, status="up", cpus=2, mem=4.0, cluster="Default"):
    return {"name": name, "status": status, "cluster": cluster, "host": host_name, "cpus": cpus, "memory_gib": mem}


def domain(name="data1", total=200.0, used=20.0, committed=50.0, low=False, dtype="data"):
    return {"name": name, "type": dtype, "total_gib": total, "used_gib": used, "available_gib": total - used,
            "used_percent": round(100 * used / total, 1), "committed_gib": committed, "low_space": low,
            "external_status": "ok"}


def inventory(hosts, vms, domains=(), events=()):
    return {"hosts": list(hosts), "vms": list(vms), "storage_domains": list(domains), "events": list(events)}


# -- figures --------------------------------------------------------------------

def test_host_and_cluster_figures():
    m = compute(inventory([host("h1", spm="spm"), host("h2")],
                          [vm("a", "h1", cpus=4, mem=8), vm("b", "h1", cpus=1, mem=1), vm("c", None, "down")],
                          [domain()]))
    h1 = next(h for h in m["hosts"] if h["name"] == "h1")
    assert (h1["vcpus_assigned"], h1["vm_memory_gib"], h1["vcpu_ratio"], h1["vm_memory_pct"]) == (5, 9.0, 1.25, 37.5)
    assert h1["running_vms"] == ["a", "b"] and h1["spm"] is True
    [c] = m["clusters"]
    assert (c["hosts_up"], c["cpus"], c["memory_gib"], c["vcpus_assigned"], c["vm_memory_gib"]) == (2, 8, 48.0, 5, 9.0)
    assert c["n_plus_1"] == {"ok": True, "worst_host": "h1", "needed_gib": 9.0, "spare_gib": 20.0,
                             "margin_gib": 11.0}
    assert m["totals"] == {"hosts": 2, "hosts_up": 2, "vms": 3, "vms_running": 2, "storage_domains": 1,
                           "warning_or_error_events": 0}
    assert [v["name"] for v in m["vms"]] == ["a", "b", "c"]  # running first


def test_cluster_that_is_not_n_plus_1():
    m = compute(inventory([host("h1"), host("h2", sched=3.0)], [vm("big", "h1", mem=10)]))
    n1 = m["clusters"][0]["n_plus_1"]
    assert n1 == {"ok": False, "worst_host": "h1", "needed_gib": 10.0, "spare_gib": 3.0, "margin_gib": -7.0}
    assert any("is not N+1" in f["finding"] and f["severity"] == "warning" for f in m["findings"])


def test_single_host_has_no_failover():
    m = compute(inventory([host("h1"), host("h2", status="maintenance")], [vm("a", "h1")]))
    assert m["clusters"][0]["n_plus_1"]["ok"] is False
    texts = [f["finding"] for f in m["findings"]]
    assert any("only one host is up" in t for t in texts)
    assert m["findings"][0] == {"severity": "critical", "finding": "Host h2 is maintenance."}  # most severe first


def test_storage_thresholds_and_thin_overcommit():
    m = compute(inventory([host("h1")], [], [domain("full", used=185), domain("busy", used=165),
                                             domain("thin", committed=300), domain("iso", dtype="iso")]))
    by_sev = {(f["severity"], f["finding"].split(":")[0].split(" is ")[0]) for f in m["findings"]}
    assert ("critical", "Storage domain full") in by_sev
    assert ("warning", "Storage domain busy") in by_sev
    assert ("info", "Storage domain thin") in by_sev
    assert [s["name"] for s in m["storage"]] == ["full", "busy", "thin"]  # only data domains


def test_host_limits_raise_warnings():
    m = compute(inventory([host("h1", cpus=2, mem=10)], [vm("a", "h1", cpus=10, mem=9.5)]))
    texts = " ".join(f["finding"] for f in m["findings"])
    assert "95.0% of its memory" in texts and "5.0 vCPUs per CPU" in texts


def test_events_outside_the_window_are_dropped():
    since = datetime(2026, 10, 12, tzinfo=UTC)
    events = [{"time": "2026-10-15T10:00:00+00:00", "description": "new"},
              {"time": "2026-10-01T10:00:00+00:00", "description": "old"},
              {"description": "no time"}]
    assert [e["description"] for e in capacity.recent(events, since)] == ["new", "no time"]


# -- report -----------------------------------------------------------------------

def test_render_without_model():
    m = compute(inventory([host("h1")], [vm("a", "h1")], [domain()],
                          [{"time": "2026-10-15T10:00:00+00:00", "severity": "error", "description": "x | y"}]))
    text = capacity.render(m, "_Skipped._", generated=datetime(2026, 10, 19, 18, 44, tzinfo=UTC),
                           source="the local MCP server (read-only)", usage=None, truncated=False, event_days=7)
    assert text.startswith("# OLVM capacity report, 2026-10-19")
    for heading in ("## Analysis", "## Findings (rule-based)", "## Clusters", "## Hosts", "## Storage",
                    "## VMs", "## Warnings and errors in the last 7 day(s)"):
        assert heading in text
    assert "x \\| y" in text  # pipes in cell text don't break the table
    assert "Analysis by" not in text
    assert "| not ok (only one host is up) |" in text


def test_render_shows_usage_and_cost():
    usage = capacity.Usage(model="claude-opus-5-5", requests=2, input_tokens=10_000, output_tokens=2_000,
                           tool_calls=1)
    text = capacity.render(compute(inventory([], [])), "ok", generated=datetime(2026, 10, 19, tzinfo=UTC),
                           source="s", usage=usage, truncated=True)
    assert "2 request(s), 1 tool call(s)" in text and "about $0.08" in text
    assert "200-item limit" in text


# -- analysis loop with a stand-in model and MCP session ---------------------------

def tool(name, read_only=True):
    return NS(name=name, description=f"{name} tool", input_schema={"type": "object", "properties": {}},
              annotations=NS(read_only_hint=read_only))


class FakeSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return NS(tools=[tool("list_events"), tool("get_vm"), tool("start_vm", read_only=False)])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return NS(content=[NS(type="text", text='{"events": []}')], is_error=False, structured_content=None)


def reply(stop_reason, *blocks, model="claude-opus-5-5"):
    return NS(stop_reason=stop_reason, content=list(blocks), model=model,
              usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0))


def tool_use(name, id_="t1", **input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def text(t):
    return NS(type="text", text=t)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.replies.pop(0)


def run_analysis(replies, max_tool_calls=8):
    session, client = FakeSession(), FakeClient(replies)
    usage = capacity.Usage(model="claude-opus-5-5")
    out = asyncio.run(capacity.analyze(session, {"findings": []}, api_key=None, model="claude-opus-5-5",
                                       effort="medium", max_tool_calls=max_tool_calls, usage=usage, client=client))
    return out, session, client, usage


def test_analysis_calls_read_tools_then_writes():
    out, session, client, usage = run_analysis([
        reply("tool_use", tool_use("list_events", search="host.name=h1")),
        reply("end_turn", text("### Summary\nAll good.")),
    ])
    assert out == "### Summary\nAll good."
    assert session.calls == [("list_events", {"search": "host.name=h1"})]
    assert (usage.requests, usage.tool_calls, usage.input_tokens) == (2, 1, 2000)
    first = client.requests[0]
    assert {t["name"] for t in first["tools"]} == {"list_events", "get_vm"}  # write tools never offered
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["thinking"] == {"type": "adaptive"} and first["output_config"] == {"effort": "medium"}
    # the tool result went back in the next request
    last = client.requests[1]["messages"][-1]["content"][0]
    assert last["type"] == "tool_result" and last["tool_use_id"] == "t1" and not last["is_error"]


def test_write_tools_are_refused_even_if_requested():
    out, session, client, _ = run_analysis([
        reply("tool_use", tool_use("start_vm", vm_name_or_id="vm-test")),
        reply("end_turn", text("done")),
    ])
    assert session.calls == []
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "not available" in result["content"]


def test_tool_call_limit():
    _, session, client, usage = run_analysis([
        reply("tool_use", tool_use("get_vm", "a"), tool_use("get_vm", "b")),
        reply("end_turn", text("done")),
    ], max_tool_calls=1)
    assert len(session.calls) == 1 and usage.tool_calls == 1
    second = client.requests[1]["messages"][-1]["content"][1]
    assert second["is_error"] and "limit reached" in second["content"]


def test_refusal_raises():
    with pytest.raises(RuntimeError, match="declined"):
        run_analysis([reply("refusal")])


def test_fallback_is_noted():
    _, _, _, usage = run_analysis([
        reply("end_turn", NS(type="fallback"), text("ok"), model="claude-opus-4-8"),
    ])
    assert any("claude-opus-4-8" in n for n in usage.notes)


# -- whole run, without the model ----------------------------------------------------

def test_run_without_model_writes_a_report(tmp_path, monkeypatch):
    class InventorySession(FakeSession):
        async def call_tool(self, name, arguments):
            data = {"list_hosts": {"hosts": [host("h1")]}, "list_vms": {"vms": [vm("a", "h1")]},
                    "list_storage_domains": {"storage_domains": [domain()]}, "list_events": {"events": []}}[name]
            import json
            return NS(content=[NS(type="text", text=json.dumps(data))], is_error=False, structured_content=None)

    @asynccontextmanager
    async def fake_connect(*_args):
        yield InventorySession()

    monkeypatch.setattr(capacity, "connect", fake_connect)
    args = capacity.argparse.Namespace(output_dir=str(tmp_path), no_llm=True, model="m", effort="medium",
                                       max_tool_calls=8, api_key_file=None, event_days=7, remote=None,
                                       remote_token_file=None, remote_ca_file=None)
    path = asyncio.run(capacity.run(args))
    report = path.read_text(encoding="utf-8")
    assert path.parent == tmp_path and path.name.startswith("capacity-")
    assert "Skipped (`--no-llm`)" in report and "| h1 | up | Default |" in report
