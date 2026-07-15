"""Host-root router: ingest (single+batch), query, tree, live stream."""

from __future__ import annotations

import pytest
import pytest_asyncio

from kestrel_feature_observability.fleet.endpoints import get_router
from kestrel_feature_observability.fleet.store import FleetObservabilityStore

from conftest import make_event


@pytest_asyncio.fixture
async def client(db_url, tenant_id):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    store = await FleetObservabilityStore.open(db_url, tenant_id)
    app = fastapi.FastAPI()
    app.include_router(get_router(lambda: store))
    try:
        with TestClient(app) as c:
            yield c
    finally:
        await store.close()


def test_post_single_event(client):
    resp = client.post("/api/host/observability/events", json=make_event(session_id="s1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ingested"] == 1
    assert len(body["ids"]) == 1


def test_cookie_csrf_helper_marks_cookie_authentication(monkeypatch):
    import sys
    from types import ModuleType

    from kestrel_feature_observability.fleet.endpoints import _enforce_csrf

    seen = {}

    def fake_enforce(request, *, authed_via_cookie):
        seen["request"] = request
        seen["authed_via_cookie"] = authed_via_cookie

    sovereign = ModuleType("kestrel_sovereign")
    security = ModuleType("kestrel_sovereign.security")
    csrf = ModuleType("kestrel_sovereign.security.csrf")
    csrf.enforce_csrf = fake_enforce
    monkeypatch.setitem(sys.modules, "kestrel_sovereign", sovereign)
    monkeypatch.setitem(sys.modules, "kestrel_sovereign.security", security)
    monkeypatch.setitem(sys.modules, "kestrel_sovereign.security.csrf", csrf)
    request = object()
    _enforce_csrf(request)

    assert seen == {"request": request, "authed_via_cookie": True}


def test_host_api_does_not_claim_agent_observability_namespace(client):
    """Host and selected-agent observability remain distinct route scopes."""
    response = client.post(
        "/api/observability/events", json=make_event(session_id="legacy")
    )
    assert response.status_code == 404


def test_post_batch_events(client):
    payload = {"events": [make_event(session_id="b") for _ in range(3)]}
    resp = client.post("/api/host/observability/events", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 3


def test_post_unknown_event_type_returns_422(client):
    resp = client.post(
        "/api/host/observability/events", json=make_event(event_type="nope")
    )
    assert resp.status_code == 422


def test_post_missing_required_returns_422(client):
    resp = client.post("/api/host/observability/events", json=make_event(agent_name=""))
    assert resp.status_code == 422


def test_post_metadata_redacted(client):
    client.post(
        "/api/host/observability/events",
        json=make_event(session_id="red", metadata={"token": "t", "ok": 1}),
    )
    resp = client.get("/api/host/observability/events", params={"session_id": "red"})
    ev = resp.json()["events"][0]
    assert ev["metadata"]["token"] == "[REDACTED]"
    assert ev["metadata"]["ok"] == 1


def test_get_events_filtered(client):
    client.post(
        "/api/host/observability/events",
        json={
            "events": [
                make_event(agent_name="a1", orchestrator="o", session_id="q"),
                make_event(agent_name="a2", orchestrator="o", session_id="q"),
            ]
        },
    )
    resp = client.get("/api/host/observability/events", params={"orchestrator": "o"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_get_events_subtree(client):
    client.post(
        "/api/host/observability/events",
        json={
            "events": [
                make_event(agent_name="root", session_id="t"),
                make_event(agent_name="child", orchestrator="root", session_id="t"),
            ]
        },
    )
    resp = client.get(
        "/api/host/observability/events",
        params={"agent_name": "root", "subtree": "true"},
    )
    assert resp.json()["count"] == 2


def test_get_tree_has_direct_node(client):
    client.post(
        "/api/host/observability/events",
        json={
            "events": [
                make_event(agent_name="root", session_id="t"),
                make_event(agent_name="child", orchestrator="root", session_id="t"),
            ]
        },
    )
    resp = client.get("/api/host/observability/tree")
    assert resp.status_code == 200
    tree = resp.json()["tree"]
    assert any(n["is_direct"] and n["label"] == "Direct" for n in tree)


def test_get_runs_aggregates(client):
    client.post(
        "/api/host/observability/events",
        json={
            "events": [
                make_event(agent_name="orch", orchestrator="orch", session_id="s1",
                           event_type="subagent_call", workflow_run_id="R1", stage="plan"),
                make_event(agent_name="w", orchestrator="orch", session_id="s2",
                           event_type="agent_response", workflow_run_id="R1", stage="build"),
                # run-less → omitted
                make_event(agent_name="solo", session_id="s3"),
            ]
        },
    )
    resp = client.get("/api/host/observability/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    run = body["runs"][0]
    assert run["run_id"] == "R1"
    assert run["status"] == "completed"
    assert {s["stage"] for s in run["stages"]} == {"plan", "build"}


def test_get_run_detail(client):
    client.post(
        "/api/host/observability/events",
        json={
            "events": [
                make_event(agent_name="a", session_id="s", event_type="tool_call",
                           workflow_run_id="RD", stage="one"),
                make_event(agent_name="a", session_id="s", event_type="tool_response",
                           workflow_run_id="RD", stage="one"),
            ]
        },
    )
    resp = client.get("/api/host/observability/runs/RD")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["run_id"] == "RD"
    assert detail["event_count"] == 2
    assert len(detail["events"]) == 2


def test_get_run_detail_404_when_missing(client):
    resp = client.get("/api/host/observability/runs/does-not-exist")
    assert resp.status_code == 404


def test_get_events_workflow_run_id_filter(client):
    client.post(
        "/api/host/observability/events",
        json={
            "events": [
                make_event(session_id="s", workflow_run_id="WF1", event_type="metric"),
                make_event(session_id="s", workflow_run_id="WF2", event_type="metric"),
            ]
        },
    )
    resp = client.get(
        "/api/host/observability/events", params={"workflow_run_id": "WF1"}
    )
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert {e["workflow_run_id"] for e in events} == {"WF1"}


# NOTE: the HTTP-level `/stream` endpoint is intentionally NOT exercised via an
# HTTP client here. It is an *unbounded* SSE stream, and tearing down the client
# mid-stream deadlocks the test harness (sync TestClient's portal thread; and
# even httpx.AsyncClient's close blocks on the infinite server generator under
# some event-loop/Python combinations). The streaming contract — preamble frame,
# `retry:` line, and live event delivery — is covered directly against the real
# store + backplane by `test_stream_generator_delivers_live_event` below.


@pytest.mark.asyncio
async def test_stream_generator_delivers_live_event(store):
    from kestrel_feature_observability.fleet.endpoints import event_stream

    gen = event_stream(store, last_event_id=0, session_id="sess")
    # Preamble frames first.
    assert (await gen.__anext__()).startswith(": stream ")
    assert (await gen.__anext__()).startswith("retry:")
    # Ingest, then the next frame is the live SSE data frame.
    await store.ingest([make_event(session_id="live")])
    frame = await gen.__anext__()
    assert frame.startswith("id: ")
    assert '"session_id": "live"' in frame
    await gen.aclose()


@pytest.mark.asyncio
async def test_stream_generator_is_tenant_scoped(store):
    """A subscriber only sees frames for its resolved tenant (fail-closed)."""
    import uuid

    from kestrel_feature_observability.fleet.endpoints import event_stream

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    gen = event_stream(store, last_event_id=0, session_id="sess", tenant_id=tenant_a)
    assert (await gen.__anext__()).startswith(": stream ")
    assert (await gen.__anext__()).startswith("retry:")
    # Another tenant's event is published but must be filtered out; tenant A's is delivered.
    await store.ingest([make_event(session_id="b-only")], tenant_id=tenant_b)
    await store.ingest([make_event(session_id="a-only")], tenant_id=tenant_a)
    frame = await gen.__anext__()
    assert '"session_id": "a-only"' in frame
    assert "b-only" not in frame
    await gen.aclose()


@pytest.mark.asyncio
async def test_stream_generator_filters_by_workflow_run_id(store):
    from kestrel_feature_observability.fleet.endpoints import event_stream

    gen = event_stream(
        store, last_event_id=0, session_id="sess", workflow_run_id="RUN"
    )
    assert (await gen.__anext__()).startswith(": stream ")
    assert (await gen.__anext__()).startswith("retry:")
    # An event for a different run is filtered out; only the matching run flows.
    await store.ingest([make_event(session_id="other", workflow_run_id="NOPE")])
    await store.ingest([make_event(session_id="mine", workflow_run_id="RUN")])
    frame = await gen.__anext__()
    assert '"session_id": "mine"' in frame
    assert "other" not in frame
    await gen.aclose()


@pytest_asyncio.fixture
async def isolating_client(db_url, tenant_id):
    """Router wired with a header-driven resolver mapping callers to tenants."""
    import uuid

    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    def resolver(request):
        return tenant_a if request.headers.get("X-Caller") == "a" else tenant_b

    store = await FleetObservabilityStore.open(db_url, tenant_id)
    app = fastapi.FastAPI()
    app.include_router(get_router(lambda: store, resolver))
    try:
        with TestClient(app) as c:
            yield c
    finally:
        await store.close()


def test_resolver_isolates_two_callers(isolating_client):
    """A resolver returning distinct tenants isolates ingest+query per caller."""
    c = isolating_client
    c.post(
        "/api/host/observability/events",
        json=make_event(session_id="from-a"),
        headers={"X-Caller": "a"},
    )
    c.post(
        "/api/host/observability/events",
        json=make_event(session_id="from-b"),
        headers={"X-Caller": "b"},
    )
    a_events = c.get(
        "/api/host/observability/events", headers={"X-Caller": "a"}
    ).json()["events"]
    b_events = c.get(
        "/api/host/observability/events", headers={"X-Caller": "b"}
    ).json()["events"]
    assert {e["session_id"] for e in a_events} == {"from-a"}
    assert {e["session_id"] for e in b_events} == {"from-b"}
