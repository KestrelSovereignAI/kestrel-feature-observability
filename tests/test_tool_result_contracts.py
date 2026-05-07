"""Direct contracts for the ObservabilityFeature ToolResult migration.

Pin the success/failure shapes so the framework's narration-honesty
audit hook (kestrel-sovereign #1042 layer 3) can trust the wire
format. The existing ``test_observability_feature.py`` covers hook
registration and lifecycle; this file pins the @tool surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kestrel_feature_observability.feature import ObservabilityFeature
from kestrel_sdk.tools.result import ToolResult, ToolResultStatus


def _agent_with_store(events_metric=None, events_error=None):
    store = SimpleNamespace(
        query_events=AsyncMock(side_effect=[events_metric or [], events_error or []]),
    )
    return SimpleNamespace(observability_store=store), store


def _agent_no_store():
    return SimpleNamespace(observability_store=None)


# ---------------------------------------------------------------------------
# obs_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obs_status_no_store_returns_failed():
    feat = ObservabilityFeature(agent=_agent_no_store())
    result = await feat.obs_status()
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "ObservabilityStore not available" in result.error


@pytest.mark.asyncio
async def test_obs_status_returns_ok_with_event_counts():
    metric_event = SimpleNamespace(
        metadata={"hook_event": "before_tool", "metric_name": "hook.something"},
    )
    agent, _ = _agent_with_store(events_metric=[metric_event])
    feat = ObservabilityFeature(agent=agent)

    result = await feat.obs_status()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "hook event(s)" in result.confirmation
    assert result.data["total_hook_events"] == 1
    assert result.data["hook_event_counts"] == {"before_tool": 1}


@pytest.mark.asyncio
async def test_obs_status_query_failure_returns_failed():
    """#1042 contract: store query exceptions land in ToolResult,
    not as raised."""
    store = SimpleNamespace(
        query_events=AsyncMock(side_effect=RuntimeError("backend down")),
    )
    feat = ObservabilityFeature(agent=SimpleNamespace(observability_store=store))

    result = await feat.obs_status()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "backend down" in result.error


@pytest.mark.asyncio
async def test_obs_status_serialization_attribute_error_returns_failed():
    """Schema drift on event records: AttributeError out of the
    iteration loop must land in ToolResult.failed, not escape."""
    class _RaisingEvent:
        @property
        def metadata(self):
            raise AttributeError("schema drift: metadata column gone")

    store = SimpleNamespace(
        query_events=AsyncMock(side_effect=[[_RaisingEvent()], []]),
    )
    feat = ObservabilityFeature(agent=SimpleNamespace(observability_store=store))

    result = await feat.obs_status()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "schema drift" in result.error


# ---------------------------------------------------------------------------
# obs_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obs_events_no_store_returns_failed():
    feat = ObservabilityFeature(agent=_agent_no_store())
    result = await feat.obs_events(event_type="metric", limit=10)
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "ObservabilityStore not available" in result.error


@pytest.mark.asyncio
async def test_obs_events_phrases_limit_as_request_not_count():
    """#1042 honesty: confirmation must reflect the request, not
    fabricate a count when fewer events came back."""
    e1 = SimpleNamespace(
        event_id="e1",
        timestamp="t1",
        agent_name="a",
        event_type="metric",
        tool_name=None,
        duration_ms=10,
        success=True,
        error_message=None,
        metadata={},
    )
    store = SimpleNamespace(query_events=AsyncMock(return_value=[e1]))
    feat = ObservabilityFeature(agent=SimpleNamespace(observability_store=store))

    result = await feat.obs_events(limit=20)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "Retrieved 1 event" in result.confirmation
    assert "limit requested: 20" in result.confirmation
    assert result.data["count"] == 1
    assert result.data["limit_requested"] == 20


@pytest.mark.asyncio
async def test_obs_events_query_failure_returns_failed():
    store = SimpleNamespace(
        query_events=AsyncMock(side_effect=RuntimeError("backend down")),
    )
    feat = ObservabilityFeature(agent=SimpleNamespace(observability_store=store))

    result = await feat.obs_events(event_type="error", limit=5)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "backend down" in result.error


@pytest.mark.asyncio
async def test_obs_events_serialization_attribute_error_returns_failed():
    """Schema drift on event records during obs_events iteration
    must land in ToolResult.failed."""
    class _RaisingEvent:
        @property
        def event_id(self):
            raise AttributeError("schema drift: event_id column gone")
        timestamp = None
        agent_name = None
        event_type = None
        tool_name = None
        duration_ms = None
        success = None
        error_message = None
        metadata = None

    store = SimpleNamespace(
        query_events=AsyncMock(return_value=[_RaisingEvent()]),
    )
    feat = ObservabilityFeature(agent=SimpleNamespace(observability_store=store))

    result = await feat.obs_events(event_type="metric", limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "schema drift" in result.error
