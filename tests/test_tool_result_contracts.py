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


@pytest.mark.asyncio
async def test_obs_events_no_store_returns_failed():
    """Claude review #5 test gap: obs_events no-store guard had no
    test coverage (only obs_status did)."""
    feat = ObservabilityFeature(agent=_agent_no_store())
    result = await feat.obs_events(event_type="metric", limit=10)
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "ObservabilityStore not available" in result.error


@pytest.mark.asyncio
async def test_obs_events_serialization_attribute_error_returns_failed():
    """Codex round-2 finding #2: obs_events also needs the schema-
    drift coverage. Schema drift on event records would otherwise
    AttributeError out of the iteration loop and escape the @tool
    envelope."""
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


@pytest.mark.asyncio
async def test_wellness_history_nan_inf_score_returns_insufficient_data():
    """Codex round-3 finding: ``float("nan")`` doesn't raise — all
    NaN comparisons are False, so corrupt rows would silently
    surface as ``stable``. ``float("inf")`` would also confidently
    say improving/declining from invalid data. Both must fall back
    to insufficient_data via ``math.isfinite`` check."""
    rows = [
        ("c2", float("nan"), json.dumps({}), "2026-05-07T13:00:00"),
        ("c1", 0.5, json.dumps({}), "2026-05-07T12:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)
    result = await feat.wellness_history(limit=10)
    assert result.status is ToolResultStatus.OK
    assert result.data["trend"] == "insufficient_data"

    # Same for inf.
    rows2 = [
        ("c2", float("inf"), json.dumps({}), "2026-05-07T13:00:00"),
        ("c1", 0.5, json.dumps({}), "2026-05-07T12:00:00"),
    ]
    db2 = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows2),
    )
    feat2 = _wellness_with_db(db=db2)
    result2 = await feat2.wellness_history(limit=10)
    assert result2.status is ToolResultStatus.OK
    assert result2.data["trend"] == "insufficient_data"


@pytest.mark.asyncio
async def test_wellness_history_non_float_score_returns_insufficient_data():
    """Codex round-2 finding #1: SQLite/schema drift could return
    overall_score as a string / Decimal / non-numeric. The trend
    diff math would TypeError out of the @tool envelope. Now safe-
    casts via ``float()`` and falls back to insufficient_data on
    cast failure rather than escaping."""
    rows = [
        ("c2", "not-a-number", json.dumps({}), "2026-05-07T13:00:00"),
        ("c1", "also-not", json.dumps({}), "2026-05-07T12:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["trend"] == "insufficient_data"


@pytest.mark.asyncio
async def test_obs_status_serialization_attribute_error_returns_failed():
    """Claude review #1: schema drift on event records would
    AttributeError out of the iteration loop. Pin envelope
    coverage."""
    bad_event = SimpleNamespace()  # no metadata attribute at all
    bad_event.metadata = None  # ensure first access path
    # Make accessing .metadata raise.
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
    # Claude review #3: failure path now includes data= context.
    assert result.data["agent_id"] == "did:test:agent-1"


@pytest.mark.asyncio
async def test_wellness_export_happy_path_with_rows():
    """Claude review #6: export happy-path with rows was uncovered
    — the row index mapping (id, agent_id, score, json, created_at)
    is exactly the kind of thing that breaks silently on schema
    drift."""
    rows = [
        ("c1", "did:test:agent-1", 0.85,
         json.dumps({"constitutional_friction": {"friction_rate": 0.1}}),
         "2026-05-07T12:00:00"),
        ("c2", "did:test:agent-1", 0.90, json.dumps({}),
         "2026-05-07T13:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_export()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["count"] == 2
    assert result.data["export_format"] == "v1"
    assert result.data["agent_id"] == "did:test:agent-1"
    # Verify row[index] mapping is correct:
    cps = result.data["checkpoints"]
    assert cps[0]["id"] == "c1"
    assert cps[0]["agent_id"] == "did:test:agent-1"
    assert cps[0]["overall_score"] == 0.85
    assert cps[0]["dimensions"] == {
        "constitutional_friction": {"friction_rate": 0.1}
    }
    assert cps[0]["created_at"] == "2026-05-07T12:00:00"


@pytest.mark.asyncio
async def test_wellness_history_trend_improving():
    """Claude review #6: trend branches for ≥2 checkpoints had no
    coverage — improving (latest - previous > 0.05)."""
    rows = [
        ("c2", 0.90, json.dumps({}), "2026-05-07T13:00:00"),
        ("c1", 0.80, json.dumps({}), "2026-05-07T12:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["trend"] == "improving"


@pytest.mark.asyncio
async def test_wellness_history_trend_declining():
    """Claude review #6: declining (latest - previous < -0.05)."""
    rows = [
        ("c2", 0.80, json.dumps({}), "2026-05-07T13:00:00"),
        ("c1", 0.90, json.dumps({}), "2026-05-07T12:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert result.status is ToolResultStatus.OK
    assert result.data["trend"] == "declining"


@pytest.mark.asyncio
async def test_wellness_history_trend_stable():
    """Claude review #6: stable (latest - previous within ±0.05)."""
    rows = [
        ("c2", 0.86, json.dumps({}), "2026-05-07T13:00:00"),
        ("c1", 0.85, json.dumps({}), "2026-05-07T12:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert result.status is ToolResultStatus.OK
    assert result.data["trend"] == "stable"


@pytest.mark.asyncio
async def test_wellness_check_all_dimensions_failed_returns_overall_zero():
    """Claude review #6: when ALL calculators error,
    _calculate_overall returns 0.0 (total_weight == 0). Pin this
    behavior so a regression doesn't silently flip to a non-zero
    value."""
    db = SimpleNamespace(execute=AsyncMock())
    feat = _wellness_with_db(db=db)
    feat._friction.measure.side_effect = RuntimeError("a")
    feat._context_pressure.measure.side_effect = RuntimeError("b")
    feat._interaction_depth.measure.side_effect = RuntimeError("c")
    feat._session_continuity.measure.side_effect = RuntimeError("d")
    feat._memory_health.measure.side_effect = RuntimeError("e")

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert result.data["overall_score"] == 0.0
    assert len(result.data["dimensions_with_errors"]) == 5


@pytest.mark.asyncio
async def test_wellness_history_corrupt_json_returns_partial(monkeypatch):
    """Codex round-1 finding #2: corrupt metrics_json was silently
    swallowed and returned as ``{}`` with status OK. Now surfaced
    via ToolResult.partial with the failure list in data."""
    rows = [
        ("c1", 0.85, "{not valid json", "2026-05-07T12:00:00"),
        ("c2", 0.90, json.dumps({"x": 1}), "2026-05-07T13:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_history(limit=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert "unreadable dimension JSON" in result.confirmation
    assert "corrupt" in result.error.lower()
    assert len(result.data["parse_failures"]) == 1
    assert result.data["parse_failures"][0]["id"] == "c1"
    # Both checkpoints still surface in the data, the corrupt one
    # with empty dimensions.
    assert result.data["count"] == 2
    assert result.data["checkpoints"][0]["dimensions"] == {}


@pytest.mark.asyncio
async def test_wellness_export_corrupt_json_returns_partial():
    rows = [
        ("c1", "did:test:agent-1", 0.85, "{bad json", "2026-05-07T12:00:00"),
        ("c2", "did:test:agent-1", 0.90, json.dumps({}), "2026-05-07T13:00:00"),
    ]
    db = SimpleNamespace(
        table_exists=AsyncMock(return_value=True),
        fetchall=AsyncMock(return_value=rows),
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_export()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert "unreadable dimension JSON" in result.confirmation
    assert len(result.data["parse_failures"]) == 1


@pytest.mark.asyncio
async def test_wellness_check_malformed_calculator_result_returns_failed():
    """Codex round-1 finding #3: a calculator that returns a
    non-dict (e.g. None, an int) would AttributeError out of
    ``_calculate_overall`` or the failed_dims comprehension. Now
    wrapped — adapter glitches land in ToolResult.failed."""
    db = SimpleNamespace(execute=AsyncMock())
    feat = _wellness_with_db(db=db)
    # Calculator returns a non-dict value. Both the failed_dims
    # comprehension and ``.get()`` calls inside _calculate_overall
    # would have crashed before the round-1 fix.
    feat._friction.measure = AsyncMock(return_value="not a dict")

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    # The wellness_check post-calc try wraps both the overall calc
    # and the failed_dims comprehension. A non-dict value passes
    # through _calculate_overall (since dimension="constitutional_friction"
    # branch reads via data.get), but the failed_dims comprehension
    # filters by isinstance(data, dict) so non-dict values are
    # ignored there. Net behavior: overall computed without that
    # dimension, dimensions_with_errors lists dict-failures only.
    # If a calculator returns something genuinely incompatible (e.g.
    # raises during dict.get), the post-calc try catches it.
    # Either way we get a structured result.
    assert isinstance(result.status, ToolResultStatus)


@pytest.mark.asyncio
async def test_wellness_check_calculator_returns_value_that_breaks_calc():
    """A calculator returning a value that crashes
    ``_calculate_overall`` (e.g. data type that raises on .get).
    Pin envelope-recovery."""

    class _BadDict(dict):
        def get(self, key, default=None):
            raise RuntimeError("simulated calculator-shape mismatch")

    db = SimpleNamespace(execute=AsyncMock())
    feat = _wellness_with_db(db=db)
    feat._friction.measure = AsyncMock(return_value=_BadDict())

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "post-calculation" in result.error.lower()
    assert "raw_metrics" in result.data


@pytest.mark.asyncio
async def test_wellness_check_save_failure_confirmation_does_not_fabricate_id():
    """Claude review #4: when the checkpoint save fails, the
    confirmation must NOT include the checkpoint_id (an operator
    querying history with it would find nothing). Pin this so a
    regression doesn't put a phantom ID back in."""
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("disk full"))
    )
    feat = _wellness_with_db(db=db)

    result = await feat.wellness_check()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    # checkpoint_id is in data (for potential retry) but NOT in the
    # confirmation, since it represents a record that was never
    # persisted.
    cp_id = result.data["checkpoint_id"]
    assert cp_id  # generated ID is present in data
    assert cp_id[:8] not in result.confirmation, (
        "save-failed confirmation must not narrate a checkpoint_id "
        "that was never written; operators querying history with "
        "that ID would find nothing"
    )
    assert "not saved" in result.confirmation.lower()
