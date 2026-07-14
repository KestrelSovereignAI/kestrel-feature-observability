"""Tests for the data plane this package still owns: structured hook emission,
parent lineage, and the agent-tree query.

External event ingest (``POST /events``) and fleet-wide/per-agent event query
(``GET /events``) moved to the fleet host feature (epic #20), so those routes
and their helpers no longer live here.

Covers:
1. Hook structured emission — tool_call/tool_response + subagent_*, pairing.
2. Lineage capture on hook events.
3. Agent-tree query + the GET /agent-tree route.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kestrel_feature_observability.endpoints import (
    build_agent_tree,
    get_router,
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
# 1. Hook structured emission
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
# 2. Lineage capture
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
# 3. Agent tree query
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


# ---------------------------------------------------------------------------
# 4. Router integration
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

    def test_events_routes_retired(self):
        # Issue #21: POST/GET /events are owned by the fleet host feature now;
        # this package's router must no longer register them (else it shadows
        # the tenant-aware fleet route).
        from fastapi.testclient import TestClient

        client = TestClient(_make_app(_make_store()))
        assert client.post(
            "/api/observability/events",
            json={"event_type": "metric", "agent_name": "a", "session_id": "s"},
        ).status_code == 404
        assert client.get("/api/observability/events").status_code == 404
