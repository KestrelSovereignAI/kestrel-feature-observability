"""Direct contracts for the ToolResult migration of
ObservabilityFeature + WellnessFeature.

Pin the success/failure shapes so the framework's narration-honesty
audit hook (kestrel-sovereign #1042 layer 3) can trust the wire
format.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kestrel_feature_observability.observability.feature import (
    ObservabilityFeature,
)
from kestrel_feature_observability.wellness.feature import WellnessFeature
from kestrel_sdk.tools.result import ToolResult, ToolResultStatus


def _agent_with_store(events_metric=None, events_error=None):
    store = SimpleNamespace(
        query_events=AsyncMock(side_effect=[events_metric or [], events_error or []]),
    )
    return SimpleNamespace(observability_store=store), store


def _agent_no_store():
    return SimpleNamespace(observability_store=None)


# ---------------------------------------------------------------------------
# ObservabilityFeature
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


# ---------------------------------------------------------------------------
# WellnessFeature
# ---------------------------------------------------------------------------


def _wellness_with_db(db=None) -> WellnessFeature:
    """Build a WellnessFeature whose calculators are stubbed so we
    can drive the test envelope deterministically. Bypasses the
    initialize() path that would touch a real DB."""
    feat = WellnessFeature(
        agent=SimpleNamespace(did="did:test:agent-1")
    )
    feat._db = db
    feat._agent_id = "did:test:agent-1"
    feat._friction = MagicMock()
    feat._context_pressure = MagicMock()
    feat._interaction_depth = MagicMock()
    feat._session_continuity = MagicMock()
    feat._memory_health = MagicMock()
    feat._friction.measure = AsyncMock(return_value={"friction_rate": 0.1})
    feat._context_pressure.measure = AsyncMock(return_value={"pressure": 0.2})
    feat._interaction_depth.measure = AsyncMock(return_value={"depth_score": 0.8})
    feat._session_continuity.measure = AsyncMock(
        return_value={"continuity_score": 0.7}
    )
    feat._memory_health.measure = AsyncMock(return_value={"health_score": 0.9})
    return feat


@pytest.mark.asyncio
async def test_wellness_check_all_dimensions_ok_returns_ok():
    db = SimpleNamespace(execute=AsyncMock())
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "Wellness checkpoint" in result.confirmation
    assert "overall" in result.confirmation
    assert result.data["overall_score"] >= 0
    assert result.data["dimensions_with_errors"] == []


@pytest.mark.asyncio
async def test_wellness_check_failed_dimension_returns_partial():
    """#1042 honesty: when a calculator raises, we still produce a
    score for the dimensions that succeeded — but the result MUST
    be PARTIAL so the LLM can't narrate a clean wellness check."""
    db = SimpleNamespace(execute=AsyncMock())
    feat = _wellness_with_db(db=db)
    feat._friction.measure.side_effect = RuntimeError("friction calc broke")

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert "constitutional_friction" in result.data["dimensions_with_errors"]
    assert "friction" in result.error.lower()


@pytest.mark.asyncio
async def test_wellness_check_save_failure_returns_partial():
    """If the per-dim measurements all succeed but the checkpoint
    save fails, the user got their score but it's NOT persisted —
    that's a partial-success."""
    db = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("disk full")))
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert "checkpoint save failed" in result.error.lower()
    assert result.data["checkpoint_save_error"] == "disk full"


@pytest.mark.asyncio
async def test_wellness_check_no_db_returns_partial_not_silently_ok():
    """Without a DB, the metrics still get computed but the
    checkpoint can't persist. PARTIAL communicates that honestly
    rather than pretending the save succeeded."""
    feat = _wellness_with_db(db=None)

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert "no database available" in result.error.lower()


@pytest.mark.asyncio
async def test_wellness_history_no_db_returns_failed():
    feat = _wellness_with_db(db=None)
    result = await feat.wellness_history(limit=5)
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "Database not available" in result.error


@pytest.mark.asyncio
async def test_wellness_history_no_table_returns_ok_no_data():
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=False),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "No wellness checkpoints" in result.confirmation
    assert result.data["count"] == 0
    assert result.data["trend"] == "no_data"


@pytest.mark.asyncio
async def test_wellness_history_single_checkpoint_says_insufficient_not_stable():
    """#1042 honesty: 1 sample is insufficient to call a trend
    'stable'. Don't lie about the trend signal when there's only
    one data point."""
    row = ("c1", 0.85, json.dumps({}), "2026-05-07T12:00:00")
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=[row]),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["trend"] == "insufficient_data"
    assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_wellness_history_query_failure_returns_failed():
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(side_effect=RuntimeError("disk failed")),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "disk failed" in result.error


@pytest.mark.asyncio
async def test_wellness_export_no_db_returns_failed():
    feat = _wellness_with_db(db=None)
    result = await feat.wellness_export()
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "Database not available" in result.error


@pytest.mark.asyncio
async def test_wellness_export_no_table_returns_ok_with_empty_export():
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=False),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_export()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["count"] == 0
    assert result.data["export_format"] == "v1"
    assert "table does not exist yet" in result.confirmation


@pytest.mark.asyncio
async def test_wellness_export_query_failure_returns_failed():
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(side_effect=RuntimeError("disk failed")),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_export()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "disk failed" in result.error
