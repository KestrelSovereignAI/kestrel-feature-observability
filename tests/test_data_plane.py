"""Tests for issue #10 data plane: external ingest, structured events,
parent lineage, and agent-tree / subtree query.

Covers:
1. POST /events ingest — single + batch, event_type → store.log_* mapping,
   metadata redaction, lineage capture, 422 on unknown/missing, round-trip.
2. Hook structured emission — tool_call/tool_response + subagent_*, pairing.
3. Lineage capture on hook events.
4. Agent-tree + subtree events query.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kestrel_feature_observability.endpoints import (
    IngestError,
    build_agent_tree,
    collect_subtree_dids,
    get_router,
    ingest_event,
    ingest_events,
    query_subtree_events,
    redact_metadata,
)
from kestrel_feature_observability.hook import ObservabilityHook


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _make_store():
    store = AsyncMock()
    store.log_tool_call = AsyncMock(return_value="call-1")
    store.log_tool_response = AsyncMock(return_value="resp-1")
    store.log_error = AsyncMock(return_value="err-1")
    store.log_metric = AsyncMock(return_value="metric-1")
    store.agent_response = AsyncMock(return_value="agentresp-1")
    store.query_events = AsyncMock(return_value=[])
    # No host-provided redactor by default (avoid AsyncMock auto-vivification).
    store.redact_metadata = None
    return store


def _make_input(event_name="PreToolUse", **overrides):
    from kestrel_sdk.hooks.base import HookInput

    defaults = {"session_id": "sess-1", "hook_event_name": event_name}
    defaults.update(overrides)
    return HookInput(**defaults)


def _make_hook_agent():
    agent = MagicMock()
    agent.agent_name = "test-agent"
    agent.observability_store = _make_store()
    # Don't let MagicMock auto-vivify lineage attrs to truthy mocks.
    agent.parent_agent = None
    agent.parent_did = None
    agent.parent_session_id = None
    return agent


# ---------------------------------------------------------------------------
# 1. External ingest
# ---------------------------------------------------------------------------

class TestIngestEvent:
    @pytest.mark.asyncio
    async def test_tool_call_maps_to_log_tool_call(self):
        store = _make_store()
        eid = await ingest_event(
            store,
            {"event_type": "tool_call", "agent_name": "a", "session_id": "s",
             "tool_name": "web_search"},
        )
        assert eid == "call-1"
        store.log_tool_call.assert_awaited_once()
        assert store.log_tool_call.call_args.kwargs["tool_name"] == "web_search"

    @pytest.mark.asyncio
    async def test_error_maps_to_log_error(self):
        store = _make_store()
        eid = await ingest_event(
            store,
            {"event_type": "error", "agent_name": "a", "session_id": "s",
             "error_message": "boom"},
        )
        assert eid == "err-1"
        assert store.log_error.call_args.kwargs["error_message"] == "boom"

    @pytest.mark.asyncio
    async def test_metric_maps_to_log_metric(self):
        store = _make_store()
        await ingest_event(
            store,
            {"event_type": "metric", "agent_name": "a", "session_id": "s",
             "metric_name": "latency", "metric_value": 5},
        )
        assert store.log_metric.call_args.kwargs["metric_name"] == "latency"
        assert store.log_metric.call_args.kwargs["metric_value"] == 5

    @pytest.mark.asyncio
    async def test_agent_response_maps_to_agent_response(self):
        store = _make_store()
        eid = await ingest_event(
            store,
            {"event_type": "agent_response", "agent_name": "a", "session_id": "s"},
        )
        assert eid == "agentresp-1"
        store.agent_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subagent_call_routes_to_log_tool_call(self):
        store = _make_store()
        await ingest_event(
            store,
            {"event_type": "subagent_call", "agent_name": "a", "session_id": "s"},
        )
        store.log_tool_call.assert_awaited_once()
        assert store.log_tool_call.call_args.kwargs["metadata"]["event_category"] == "subagent_call"

    @pytest.mark.asyncio
    async def test_tool_response_paired_when_event_id_present(self):
        store = _make_store()
        await ingest_event(
            store,
            {"event_type": "tool_response", "agent_name": "a", "session_id": "s",
             "event_id": "call-1", "success": True},
        )
        assert store.log_tool_response.call_args.kwargs["event_id"] == "call-1"

    @pytest.mark.asyncio
    async def test_tool_response_standalone_when_no_event_id(self):
        store = _make_store()
        await ingest_event(
            store,
            {"event_type": "tool_response", "agent_name": "a", "session_id": "s"},
        )
        assert store.log_tool_response.call_args.kwargs["event_id"] is None

    @pytest.mark.asyncio
    async def test_unknown_event_type_raises(self):
        store = _make_store()
        with pytest.raises(IngestError):
            await ingest_event(
                store,
                {"event_type": "nonsense", "agent_name": "a", "session_id": "s"},
            )

    @pytest.mark.asyncio
    async def test_missing_agent_name_raises(self):
        store = _make_store()
        with pytest.raises(IngestError):
            await ingest_event(store, {"event_type": "metric", "session_id": "s"})

    @pytest.mark.asyncio
    async def test_missing_session_id_raises(self):
        store = _make_store()
        with pytest.raises(IngestError):
            await ingest_event(store, {"event_type": "metric", "agent_name": "a"})

    @pytest.mark.asyncio
    async def test_metadata_redacted(self):
        store = _make_store()
        await ingest_event(
            store,
            {"event_type": "metric", "agent_name": "a", "session_id": "s",
             "metadata": {"api_key": "secret123", "safe": "ok"}},
        )
        meta = store.log_metric.call_args.kwargs["metadata"]
        assert meta["api_key"] == "[REDACTED]"
        assert meta["safe"] == "ok"

    @pytest.mark.asyncio
    async def test_lineage_fields_folded_into_metadata(self):
        store = _make_store()
        await ingest_event(
            store,
            {"event_type": "tool_call", "agent_name": "child", "session_id": "s2",
             "parent_agent": "did:agent:parent", "parent_session_id": "sess-parent",
             "driven_by": "talon"},
        )
        meta = store.log_tool_call.call_args.kwargs["metadata"]
        assert meta["parent_agent"] == "did:agent:parent"
        assert meta["parent_session_id"] == "sess-parent"
        assert meta["driven_by"] == "talon"

    @pytest.mark.asyncio
    async def test_uses_host_redactor_when_present(self):
        store = _make_store()
        store.redact_metadata = lambda m: {"redacted": True}
        await ingest_events(
            store,
            {"event_type": "metric", "agent_name": "a", "session_id": "s",
             "metadata": {"x": 1}},
            redactor=store.redact_metadata,
        )
        assert store.log_metric.call_args.kwargs["metadata"]["redacted"] is True


class TestIngestBatch:
    @pytest.mark.asyncio
    async def test_single_event_returns_event_id(self):
        store = _make_store()
        out = await ingest_events(
            store, {"event_type": "metric", "agent_name": "a", "session_id": "s"}
        )
        assert out == {"event_id": "metric-1"}

    @pytest.mark.asyncio
    async def test_batch_returns_event_ids(self):
        store = _make_store()
        out = await ingest_events(
            store,
            {"events": [
                {"event_type": "metric", "agent_name": "a", "session_id": "s"},
                {"event_type": "error", "agent_name": "a", "session_id": "s",
                 "error_message": "x"},
            ]},
        )
        assert out["count"] == 2
        assert out["event_ids"] == ["metric-1", "err-1"]

    @pytest.mark.asyncio
    async def test_batch_events_not_a_list_raises(self):
        store = _make_store()
        with pytest.raises(IngestError):
            await ingest_events(store, {"events": "nope"})


class TestRedactMetadata:
    def test_scrubs_nested(self):
        out = redact_metadata({"outer": {"password": "p", "keep": 1}, "token": "t"})
        assert out["outer"]["password"] == "[REDACTED]"
        assert out["outer"]["keep"] == 1
        assert out["token"] == "[REDACTED]"

    def test_scrubs_inside_lists(self):
        out = redact_metadata({"items": [{"secret": "x"}]})
        assert out["items"][0]["secret"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 2. Hook structured emission
# ---------------------------------------------------------------------------

class TestStructuredEmission:
    @pytest.mark.asyncio
    async def test_pre_tool_use_emits_tool_call_and_metric(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(_make_input("PreToolUse", tool_name="web_search"))

        # Both the backward-compat metric and the structured tool_call fire.
        agent.observability_store.log_metric.assert_awaited_once()
        agent.observability_store.log_tool_call.assert_awaited_once()
        kwargs = agent.observability_store.log_tool_call.call_args.kwargs
        assert kwargs["tool_name"] == "web_search"
        assert kwargs["metadata"]["event_category"] == "tool_call"

    @pytest.mark.asyncio
    async def test_post_tool_use_pairs_via_cached_event_id(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(_make_input("PreToolUse", tool_name="t"))
        await hook.execute(
            _make_input("PostToolUse", tool_name="t",
                        tool_response={"success": True}, execution_time_ms=12)
        )
        resp_kwargs = agent.observability_store.log_tool_response.call_args.kwargs
        assert resp_kwargs["event_id"] == "call-1"  # cached from PreToolUse
        assert resp_kwargs["success"] is True
        assert resp_kwargs["duration_ms"] == 12

    @pytest.mark.asyncio
    async def test_post_tool_use_standalone_when_no_prior_call(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(
            _make_input("PostToolUse", tool_name="t", tool_response={"success": True})
        )
        assert agent.observability_store.log_tool_response.call_args.kwargs["event_id"] is None

    @pytest.mark.asyncio
    async def test_subagent_events_emit_structured(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(_make_input("PreSubagentCall", child_name="researcher"))
        await hook.execute(_make_input("PostSubagentCall", child_name="researcher"))
        assert agent.observability_store.log_tool_call.call_args.kwargs["metadata"]["event_category"] == "subagent_call"
        assert agent.observability_store.log_tool_response.call_args.kwargs["metadata"]["event_category"] == "subagent_response"

    @pytest.mark.asyncio
    async def test_pairing_uses_tool_use_id(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(
            _make_input("PreToolUse", tool_name="t",
                        tool_input={"tool_use_id": "abc"})
        )
        await hook.execute(
            _make_input("PostToolUse", tool_name="t",
                        tool_response={"tool_use_id": "abc", "success": True})
        )
        assert agent.observability_store.log_tool_response.call_args.kwargs["event_id"] == "call-1"

    @pytest.mark.asyncio
    async def test_structured_failure_does_not_block(self):
        agent = _make_hook_agent()
        agent.observability_store.log_tool_call.side_effect = RuntimeError("db")
        hook = ObservabilityHook(agent=agent)
        result = await hook.execute(_make_input("PreToolUse", tool_name="t"))
        assert result.continue_execution is True


# ---------------------------------------------------------------------------
# 3. Lineage capture
# ---------------------------------------------------------------------------

class TestLineageCapture:
    @pytest.mark.asyncio
    async def test_subagent_id_and_child_name_captured(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(
            _make_input("PreSubagentCall", child_did="did:agent:child",
                        child_name="researcher")
        )
        meta = agent.observability_store.log_metric.call_args.kwargs["metadata"]
        assert meta["subagent_id"] == "did:agent:child"
        assert meta["child_name"] == "researcher"

    @pytest.mark.asyncio
    async def test_parent_agent_from_hook_input(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(
            _make_input("PreToolUse", tool_name="t", parent_did="did:agent:parent")
        )
        meta = agent.observability_store.log_metric.call_args.kwargs["metadata"]
        assert meta["parent_agent"] == "did:agent:parent"

    @pytest.mark.asyncio
    async def test_parent_session_from_agent(self):
        agent = _make_hook_agent()
        agent.parent_session_id = "sess-driver"
        hook = ObservabilityHook(agent=agent)
        await hook.execute(_make_input("PreToolUse", tool_name="t"))
        meta = agent.observability_store.log_metric.call_args.kwargs["metadata"]
        assert meta["parent_session_id"] == "sess-driver"

    @pytest.mark.asyncio
    async def test_no_lineage_keys_when_absent(self):
        agent = _make_hook_agent()
        hook = ObservabilityHook(agent=agent)
        await hook.execute(_make_input("PreToolUse", tool_name="t"))
        meta = agent.observability_store.log_metric.call_args.kwargs["metadata"]
        assert "parent_agent" not in meta
        assert "subagent_id" not in meta


# ---------------------------------------------------------------------------
# 4. Agent tree / subtree query
# ---------------------------------------------------------------------------

class FakeAgentManager:
    def __init__(self, children_map, names=None):
        self._children = children_map
        self._names = names or {}

    def get_children(self, did):
        return list(self._children.get(did, []))

    def get_agent(self, did):
        name = self._names.get(did)
        return SimpleNamespace(agent_name=name) if name else None


class TestAgentTree:
    @pytest.mark.asyncio
    async def test_builds_recursive_tree(self):
        mgr = FakeAgentManager(
            {"root": ["c1", "c2"], "c1": ["gc1"]},
            names={"root": "root-agent", "c1": "child-1", "gc1": "grandchild"},
        )
        tree = await build_agent_tree(mgr, "root")
        assert tree["agent_name"] == "root-agent"
        assert len(tree["children"]) == 2
        c1 = next(c for c in tree["children"] if c["agent_did"] == "c1")
        assert c1["children"][0]["agent_did"] == "gc1"

    @pytest.mark.asyncio
    async def test_degrades_to_flat_node_without_manager(self):
        tree = await build_agent_tree(None, "root")
        assert tree == {"agent_did": "root", "agent_name": "root", "children": []}

    @pytest.mark.asyncio
    async def test_cycle_safe(self):
        mgr = FakeAgentManager({"a": ["b"], "b": ["a"]})
        tree = await build_agent_tree(mgr, "a")
        # b's child 'a' is already visited → no infinite recursion, empty children.
        b = tree["children"][0]
        assert b["children"][0]["children"] == []

    @pytest.mark.asyncio
    async def test_collect_subtree_dids(self):
        mgr = FakeAgentManager({"root": ["c1", "c2"], "c1": ["gc1"]})
        dids = await collect_subtree_dids(mgr, "root")
        assert set(dids) == {"root", "c1", "c2", "gc1"}


class TestSubtreeEvents:
    @pytest.mark.asyncio
    async def test_unions_per_agent_query(self):
        mgr = FakeAgentManager(
            {"root": ["c1"]},
            names={"root": "root-agent", "c1": "child-1"},
        )
        events_by_agent = {
            "root-agent": [SimpleNamespace(event_id="e1", agent_name="root-agent")],
            "child-1": [SimpleNamespace(event_id="e2", agent_name="child-1")],
        }

        store = MagicMock()

        async def _query(agent_name=None, event_type=None, since=None, limit=None):
            return events_by_agent.get(agent_name, [])

        store.query_events = _query
        out = await query_subtree_events(store, mgr, "root", subtree=True)
        assert out["count"] == 2
        assert set(out["agents"]) == {"root-agent", "child-1"}
        assert {e["event_id"] for e in out["events"]} == {"e1", "e2"}

    @pytest.mark.asyncio
    async def test_single_agent_when_subtree_false(self):
        mgr = FakeAgentManager({"root": ["c1"]}, names={"root": "root-agent"})
        store = MagicMock()

        async def _query(agent_name=None, event_type=None, since=None, limit=None):
            return [SimpleNamespace(event_id="e1", agent_name=agent_name)]

        store.query_events = _query
        out = await query_subtree_events(
            store, mgr, "root", root_agent_name="root-agent", subtree=False
        )
        assert out["subtree"] is False
        assert out["agents"] == ["root-agent"]


# ---------------------------------------------------------------------------
# 5. Router integration
# ---------------------------------------------------------------------------

def _make_app(store, agent_manager=None, agent_id="did:agent:root",
              agent_name="root-agent"):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(get_router())
    app.state.agent = SimpleNamespace(
        agent_id=agent_id,
        agent_name=agent_name,
        observability_store=store,
        agent_manager=agent_manager,
    )
    return app


class TestRouterDataPlane:
    def test_post_events_single(self):
        from fastapi.testclient import TestClient

        store = _make_store()
        client = TestClient(_make_app(store))
        resp = client.post(
            "/api/observability/events",
            json={"event_type": "metric", "agent_name": "a", "session_id": "s"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"event_id": "metric-1"}

    def test_post_events_batch(self):
        from fastapi.testclient import TestClient

        store = _make_store()
        client = TestClient(_make_app(store))
        resp = client.post(
            "/api/observability/events",
            json={"events": [
                {"event_type": "tool_call", "agent_name": "a", "session_id": "s"},
                {"event_type": "metric", "agent_name": "a", "session_id": "s"},
            ]},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_post_events_unknown_type_422(self):
        from fastapi.testclient import TestClient

        store = _make_store()
        client = TestClient(_make_app(store))
        resp = client.post(
            "/api/observability/events",
            json={"event_type": "bogus", "agent_name": "a", "session_id": "s"},
        )
        assert resp.status_code == 422

    def test_post_events_missing_required_422(self):
        from fastapi.testclient import TestClient

        store = _make_store()
        client = TestClient(_make_app(store))
        resp = client.post(
            "/api/observability/events",
            json={"event_type": "metric", "agent_name": "a"},
        )
        assert resp.status_code == 422

    def test_agent_tree_endpoint(self):
        from fastapi.testclient import TestClient

        mgr = FakeAgentManager(
            {"did:agent:root": ["c1"]},
            names={"did:agent:root": "root-agent", "c1": "child-1"},
        )
        client = TestClient(_make_app(_make_store(), agent_manager=mgr))
        resp = client.get("/api/observability/agent-tree")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_did"] == "did:agent:root"
        assert body["children"][0]["agent_did"] == "c1"

    def test_events_subtree_endpoint(self):
        from fastapi.testclient import TestClient

        mgr = FakeAgentManager(
            {"did:agent:root": ["c1"]},
            names={"did:agent:root": "root-agent", "c1": "child-1"},
        )
        store = _make_store()

        events_by_agent = {
            "root-agent": [SimpleNamespace(
                event_id="e1", agent_name="root-agent", timestamp=None,
                event_type="tool_call", tool_name="t", session_id="s",
                duration_ms=None, success=True, error_message=None, metadata={})],
            "child-1": [SimpleNamespace(
                event_id="e2", agent_name="child-1", timestamp=None,
                event_type="tool_call", tool_name="t2", session_id="s2",
                duration_ms=None, success=True, error_message=None, metadata={})],
        }

        async def _query(agent_name=None, event_type=None, since=None, limit=None):
            return events_by_agent.get(agent_name, [])

        store.query_events = _query
        client = TestClient(_make_app(store, agent_manager=mgr))
        resp = client.get("/api/observability/events?subtree=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert {e["event_id"] for e in body["events"]} == {"e1", "e2"}

    def test_post_events_503_when_store_missing(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(get_router())
        app.state.agent = SimpleNamespace(
            agent_id="did:agent:root", observability_store=None)
        client = TestClient(app)
        resp = client.post(
            "/api/observability/events",
            json={"event_type": "metric", "agent_name": "a", "session_id": "s"},
        )
        assert resp.status_code == 503
