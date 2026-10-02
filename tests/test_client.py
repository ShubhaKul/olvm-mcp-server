import httpx
import pytest

from olvm_mcp.client import OlvmAuthError, OlvmClient, OlvmError, OlvmNotFound

from .conftest import CLUSTERS


def test_logs_in_and_sends_bearer_token(client, engine):
    route = engine["clusters"]
    assert client.list("clusters", "cluster") == CLUSTERS["cluster"]
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok-1"
    assert request.headers["Accept"] == "application/json"
    login = engine["token"].calls.last.request.content.decode()
    assert "scope=ovirt-app-api" in login and "grant_type=password" in login


def test_search_and_max_are_passed(client, engine):
    route = engine.get("/api/vms").respond(200, json={"vm": []})
    client.list("vms", "vm", search="status=up", max_results=5)
    assert route.calls.last.request.url.params == httpx.QueryParams({"search": "status=up", "max": "5"})


def test_empty_collection_returns_empty_list(client, engine):
    engine.get("/api/vms").respond(200, json={})
    assert client.list("vms", "vm") == []


def test_expired_token_is_refreshed_once(client, engine):
    engine.post("/sso/oauth/token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "tok-1"}),
        httpx.Response(200, json={"access_token": "tok-2"}),
    ])
    route = engine.get("/api/vms").mock(side_effect=[
        httpx.Response(401),
        httpx.Response(200, json={"vm": []}),
    ])
    assert client.list("vms", "vm") == []
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-2"


def test_post_sends_json_and_correlation_id(client, engine):
    route = engine.post("/api/vms/x/start").respond(200, json={"status": "complete"})
    assert client.post("vms/x/start", {}, correlation_id="olvm-mcp-abc") == {"status": "complete"}
    request = route.calls.last.request
    assert request.headers["Correlation-Id"] == "olvm-mcp-abc"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == "Bearer tok-1"


def test_failed_action_fault_detail_is_reported(client, engine):
    # The shape OLVM 4.5 returns when the scheduler refuses to start a VM.
    engine.post("/api/vms/x/start").respond(409, json={"status": "failed", "fault": {
        "reason": "Operation Failed",
        "detail": "[Cannot run VM. There is no host that satisfies current scheduling constraints.]"}})
    with pytest.raises(OlvmError, match="HTTP 409.*no host that satisfies"):
        client.post("vms/x/start", {})


def test_post_retries_once_on_expired_token(client, engine):
    engine.post("/sso/oauth/token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "tok-1"}),
        httpx.Response(200, json={"access_token": "tok-2"}),
    ])
    route = engine.post("/api/vms/x/start").mock(side_effect=[
        httpx.Response(401),
        httpx.Response(200, json={}),
    ])
    client.post("vms/x/start", {})
    assert route.call_count == 2
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-2"


def test_bad_credentials(client, engine):
    engine.post("/sso/oauth/token").respond(
        400, json={"error": "access_denied", "error_description": "Cannot authenticate user"})
    with pytest.raises(OlvmAuthError, match="Cannot authenticate user"):
        client.get("vms")


def test_html_error_page_explains_permissions(client, engine):
    engine.get("/api/vms").respond(200, html="<html>Unauthorized</html>")
    with pytest.raises(OlvmError, match="ReadOnlyAdmin"):
        client.get("vms")


def test_fault_detail_is_reported(client, engine):
    engine.get("/api/vms").respond(400, json={"reason": "Operation Failed", "detail": "bad search"})
    with pytest.raises(OlvmError, match="bad search"):
        client.get("vms")


def test_not_found(client, engine):
    engine.get("/api/vms/x").respond(404, json={})
    with pytest.raises(OlvmNotFound):
        client.get("vms/x")


def test_unreachable_engine_mentions_url(settings, engine):
    engine.post("/sso/oauth/token").mock(side_effect=httpx.ConnectError("refused"))
    c = OlvmClient(settings)
    with pytest.raises(OlvmError, match="Cannot reach the engine at https://engine.test"):
        c.get("vms")


def test_close_revokes_token_like_the_sdk(settings, engine):
    c = OlvmClient(settings)
    c.get("clusters")
    c.close()
    body = engine["logout"].calls.last.request.content.decode()
    assert "scope=ovirt-app-api" in body and "token=tok-1" in body


def test_names_are_cached(client, engine):
    route = engine["clusters"]
    assert client.names("clusters", "cluster") == {"c1": "Default"}
    client.names("clusters", "cluster")
    assert route.call_count == 1


def test_delete_sends_params_and_correlation_id(client, engine):
    route = engine.delete("/api/vms/x").respond(200)
    assert client.delete("vms/x", {"detach_only": "false"}, "cid-1") == {}
    request = route.calls.last.request
    assert request.url.params["detach_only"] == "false"
    assert request.headers["Correlation-Id"] == "cid-1"
