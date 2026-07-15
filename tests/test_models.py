"""Accepted event types + model metadata mapping."""

from __future__ import annotations

from kestrel_feature_observability.fleet.models import (
    CORE_EVENT_TYPES,
    EVENT_TYPES,
    GATE_EVENT_TYPES,
    GATE_KINDS,
    ObservabilityEvent,
)


def test_core_event_types_accepted():
    assert {
        "tool_call",
        "tool_response",
        "agent_response",
        "subagent_call",
        "subagent_response",
        "error",
        "metric",
    } <= CORE_EVENT_TYPES


def test_gate_event_types_accepted():
    assert GATE_EVENT_TYPES == {"gate_started", "gate_passed", "gate_failed"}
    assert GATE_EVENT_TYPES <= EVENT_TYPES


def test_gate_kinds():
    assert GATE_KINDS == {
        "self-review",
        "quality",
        "integration",
        "eye",
        "verify",
        "ci",
        "demo",
    }


def test_metadata_maps_to_reserved_column_name():
    # Python attribute is event_metadata; the DB/wire column is `metadata`.
    col = ObservabilityEvent.__table__.c["metadata"]
    assert col is not None


def test_correlation_columns_indexed():
    indexed = {
        col
        for index in ObservabilityEvent.__table__.indexes
        for col in index.columns.keys()
    }
    for expected in (
        "ts",
        "agent_name",
        "session_id",
        "orchestrator",
        "tenant_id",
        "workflow_run_id",
        "stage",
    ):
        assert expected in indexed, expected
