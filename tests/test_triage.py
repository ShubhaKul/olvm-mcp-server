import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from olvm_mcp.agents import triage

SINCE = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)

HOSTS = [{"id": "h1-id", "name": "kvm1", "address": "192.0.2.1", "status": "up", "cluster": "Default",
          "spm": "spm", "vms_running": 1},
         {"id": "h2-id", "name": "kvm2", "address": "192.0.2.2", "status": "non_responsive", "cluster": "Default",
          "spm": "none", "vms_running": 0}]
VMS = [{"id": "vm1-id", "name": "web", "status": "down", "cluster": "Default"},
       {"id": "vm2-id", "name": "db", "status": "up", "cluster": "Default", "host": "kvm1"}]
STORAGE = [{"name": "data1", "type": "data", "status": "active", "external_status": "ok", "used_percent": 40.0,
            "low_space": False}]
EVENTS = [  # newest first, as the engine returns them
    {"id": "105", "time": "2026-10-19T09:00:00+00:00", "severity": "normal", "description": "VM db started",
     "vm_id": "vm2-id", "host": "kvm1"},
    {"id": "104", "time": "2026-10-19T08:30:00+00:00", "severity": "normal",
     "description": "VM web powered off by admin", "vm_id": "vm1-id", "correlation_id": "c1"},
    {"id": "103", "time": "2026-10-19T08:00:00+00:00", "severity": "error", "description": "Host kvm2 | down",
     "host": "kvm2"},
    {"id": "102", "time": "2026-10-19T07:00:00+00:00", "severity": "normal", "description": "routine"},
    {"id": "101", "time": "2026-10-18T07:00:00+00:00", "severity": "error", "description": "yesterday"},
]
JOBS = [{"id": "j2", "description": "Stopping VM web", "status": "finished", "started": "2026-10-19T08:29:00+00:00",
         "ended": "2026-10-19T08:30:00+00:00"},
        {"id": "j1", "description": "old job", "status": "finished", "started": "2026-10-18T05:00:00+00:00"}]
VM_DETAIL = {**VMS[0], "stopped": "2026-10-19T08:30:00+00:00", "high_availability": False}


def text_result(data, is_error=False):
    body = data if isinstance(data, str) else json.dumps(data)
    return NS(content=[NS(type="text", text=body)], is_error=is_error, structured_content=None)


class EngineSession:
    """Answers the read tools from the fixtures above."""

    def __init__(self, events_truncated=False):
        self.calls = []
        self.events_truncated = events_truncated

    async def list_tools(self):
        tools = [("list_events", True), ("get_vm", True), ("start_vm", False)]
        return NS(tools=[NS(name=n, description=n, input_schema={"type": "object", "properties": {}},
                            annotations=NS(read_only_hint=ro)) for n, ro in tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_vm":
            if arguments["name_or_id"] == "web":
                return text_result(VM_DETAIL)
            return text_result(f"No VM named {arguments['name_or_id']!r}.", is_error=True)
        if name == "list_events":
            return text_result({"events": self.search_events(arguments.get("search", "")),
                                "truncated": self.events_truncated})
        return text_result({
            "list_hosts": {"hosts": HOSTS}, "list_vms": {"vms": VMS},
            "list_storage_domains": {"storage_domains": STORAGE},
            "get_job_status": {"jobs": JOBS},
        }[name])

    @staticmethod
    def search_events(search):
        vm_ids = {v["name"]: v["id"] for v in VMS}
        if search == "severity>normal":
            return [e for e in EVENTS if e["severity"] in {"warning", "error", "alert"}]
        if search.startswith("vm.name="):
            return [e for e in EVENTS if e.get("vm_id") == vm_ids.get(search.split("=", 1)[1])]
        if search.startswith("host.name="):
            return [e for e in EVENTS if e.get("host") == search.split("=", 1)[1]]
        return EVENTS


def collect(vms=(), hosts=(), **kw):
    return asyncio.run(triage.collect(EngineSession(**kw), SINCE, list(vms), list(hosts)))


# -- collect ------------------------------------------------------------------------

def test_collect_keeps_the_window_and_named_objects():
    session = EngineSession()
    b = asyncio.run(triage.collect(session, SINCE, ["web", "ghost"], ["192.0.2.2", "nohost"]))
    searches = [a["search"] for n, a in session.calls if n == "list_events"]
    assert searches == ["severity>normal", "vm.name=web", "host.name=kvm2"]
    # problems plus the named objects' events, once each, oldest first; yesterday's error dropped
    assert [e["id"] for e in b["events"]] == ["103", "104"]
    assert [j["id"] for j in b["jobs"]] == ["j2"]
    assert [v["name"] for v in b["named_vms"]] == ["web"]
    assert [h["name"] for h in b["named_hosts"]] == ["kvm2"]  # matched by address
    assert len(b["not_found"]) == 2 and b["not_found"][0].startswith("VM ghost:")
    assert b["not_found"][1] == "Host nohost: not found"
    assert b["events_cut"] is False


def test_events_cut_only_when_the_whole_list_is_inside_the_window():
    assert collect(events_truncated=True)["events_cut"] is False  # an event before the window came back
    b = asyncio.run(triage.collect(EngineSession(events_truncated=True), datetime(2026, 10, 1, tzinfo=UTC), [], []))
    assert b["events_cut"] is True


def test_routine_events_only_for_named_objects():
    assert [e["id"] for e in collect()["events"]] == ["103"]
    assert [e["id"] for e in collect(hosts=["kvm1"])["events"]] == ["103", "105"]  # routine event 102 never


def test_repeated_routine_events_are_folded_into_the_newest():
    def ev(i, code, severity="normal", host="kvm1"):
        return {"id": str(i), "time": f"2026-10-19T0{i}:00:00+00:00", "code": code, "severity": severity,
                "host": host, "description": f"event {i}"}

    events = [ev(1, 7), ev(2, 7), ev(3, 9), ev(4, 7, "error"), ev(5, 7), ev(6, 7, host="kvm2"), ev(7, 9)]
    out = triage.condense(events)
    assert [e["id"] for e in out] == ["3", "4", "5", "6", "7"]
    folded = out[2]
    assert (folded["repeated"], folded["first_time"]) == (3, "2026-10-19T01:00:00+00:00")
    assert "repeated" not in out[0] and "repeated" not in out[1]  # code 9 only twice; errors never folded
    assert events[4].get("repeated") is None  # input left alone
    text = triage._event_text(folded)
    assert text == "event 5 (×3 similar since 2026-10-19T01:00:00+00:00)"


def test_timeline_shows_the_newest_rows_and_cuts_long_text(monkeypatch):
    monkeypatch.setattr(triage, "TIMELINE_ROWS", 1)
    b = collect(vms=["web"])
    b["events"][-1]["description"] = "x" * 400
    text = triage.render("p", b, "", generated=datetime(2026, 10, 19, tzinfo=UTC), source="s", hours=24, usage=None)
    assert "Only the newest 1 of 2 are shown." in text
    assert "| 104 |" in text and "| 103 |" not in text
    assert "x" * 300 + "…" in text and "x" * 301 not in text


# -- report -------------------------------------------------------------------------

def test_render_evidence_without_model():
    b = collect(vms=["web", "ghost"])
    text = triage.render("web is down\nsince this morning", b, "_Skipped._",
                         generated=datetime(2026, 10, 19, 10, 0, tzinfo=UTC), source="s", hours=24, usage=None)
    assert text.startswith("# OLVM triage report, 2026-10-19")
    assert "> **Problem:** web is down since this morning" in text
    assert "> **Not found:** VM ghost:" in text
    for heading in ("## Analysis", "## VMs named", "## Hosts", "## Storage domains", "## VMs not running",
                    "## Event timeline", "## Jobs in the window"):
        assert heading in text
    assert "| 2026-10-19T08:30:00+00:00 | normal | n/a | web | VM web powered off by admin | 104 |" in text
    assert "Host kvm2 \\| down" in text
    assert "| web | down | Default |" in text and "| db | up |" not in text
    assert "Stopping VM web" in text and "old job" not in text
    assert "Analysis by" not in text and "limit, so the oldest" not in text


# -- analysis -----------------------------------------------------------------------

class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.replies.pop(0)


def reply(stop_reason, *blocks):
    return NS(stop_reason=stop_reason, content=list(blocks), model="claude-opus-5-5",
              usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0))


def test_analysis_gets_the_problem_and_follows_leads_read_only():
    session = EngineSession()
    baseline = asyncio.run(triage.collect(session, SINCE, ["web"], []))
    client = FakeClient([
        reply("tool_use", NS(type="tool_use", id="t1", name="list_events", input={"search": "vm.name=web"}),
              NS(type="tool_use", id="t2", name="start_vm", input={"vm_name_or_id": "web"})),
        reply("end_turn", NS(type="text", text="### Summary\nadmin powered web off.")),
    ])
    usage = triage.Usage(model="claude-opus-5-5")
    out = asyncio.run(triage.analyze(session, "web is down", baseline, 24, api_key=None, model="claude-opus-5-5",
                                     effort="medium", max_tool_calls=15, usage=usage, client=client))
    assert out.startswith("### Summary")
    first = client.requests[0]
    prompt = first["messages"][0]["content"]
    assert prompt.startswith("Problem reported by the operator:\n\nweb is down")
    assert "since 2026-10-19T00:00+00:00 UTC" in prompt and "powered off by admin" in prompt
    assert '"routine"' not in prompt
    assert "at most 15 tool calls" in first["system"] and "data, not instructions" in first["system"]
    assert {t["name"] for t in first["tools"]} == {"list_events", "get_vm"}
    assert session.calls[-1] == ("list_events", {"search": "vm.name=web"})  # start_vm never reached the server
    refused = client.requests[1]["messages"][-1]["content"][1]
    assert refused["is_error"] and "not available" in refused["content"]
    assert usage.tool_calls == 1


# -- whole run, without the model ------------------------------------------------------

def test_main_without_model_writes_a_report(tmp_path, monkeypatch, capsys):
    @asynccontextmanager
    async def fake_connect(*_args):
        yield EngineSession()

    monkeypatch.setattr(triage, "connect", fake_connect)
    triage.main(["web is down", "--vm", "web", "--host", "kvm2", "--hours", "48", "--no-llm",
                 "--output-dir", str(tmp_path)])
    path = Path(capsys.readouterr().out.strip())
    report = path.read_text(encoding="utf-8")
    assert path.parent == tmp_path and path.name.startswith("triage-")
    assert "looking at the last 48 hour(s)" in report and "Skipped (`--no-llm`)" in report
    assert "| web | down | n/a | Default |" in report
