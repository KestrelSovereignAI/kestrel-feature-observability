"""Tests for the Observability query router and its pure query helpers.

Covers:
1. Pure helpers (query_llm_calls / get_llm_stats) against a fake store —
   filtering, paging, percentile derivation, empty-store path.
2. The FastAPI router mounted on an app with the standard agent context —
   GET /api/observability/llm-calls and /llm-stats, including the
   observability-not-enabled (503) path.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kestrel_feature_observability.endpoints import (
    API_PREFIX,
    get_llm_stats,
    get_router,
    query_llm_calls,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _call(event_id, model, provider="anthropic", success=True, duration_ms=100,
          input_tokens=10, output_tokens=20, agent_did="did:agent:1"):
    return SimpleNamespace(
        event_id=event_id,
        timestamp=datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc),
        provider=provider,
        model=model,
        companion_id=None,
        agent_did=agent_did,
        session_id="sess-1",
        duration_ms=duration_ms,
        success=success,
        error_message=None if success else "boom",
        system_prompt_preview="sys",
        user_prompt_preview="hi",
        response_preview="hello",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls=None,
        metadata={},
    )


class FakeStore:
    """Minimal stand-in for the host ObservabilityStore LLM-call surface."""

    def __init__(self, calls=None, stats=None):
        self._calls = calls or []
        self._stats = stats if stats is not None else {}
        self.last_query_kwargs = None

    async def query_llm_calls(self, **kwargs):
        self.last_query_kwargs = kwargs
        calls = self._calls
        model = kwargs.get("model")
        success = kwargs.get("success")
        if model is not None:
            calls = [c for c in calls if c.model == model]
        if success is not None:
            calls = [c for c in calls if c.success == success]
        return calls[: kwargs.get("limit", 100)]

    async def get_llm_stats(self, since=None, until=None):
        return dict(self._stats)


# ---------------------------------------------------------------------------
# 1. Pure helpers
# ---------------------------------------------------------------------------

class TestQueryLLMCallsHelper:
    @pytest.mark.asyncio
    async def test_serializes_and_projects_fields(self):
        store = FakeStore(calls=[_call("e1", "claude-opus-4-8")])
        out = await query_llm_calls(store, agent_did="did:agent:1", limit=50)
        assert out["count"] == 1
        call = out["calls"][0]
        assert call["event_id"] == "e1"
        assert call["model"] == "claude-opus-4-8"
        assert call["input_tokens"] == 10
        assert call["timestamp"].startswith("2026-07-04")

    @pytest.mark.asyncio
    async def test_scopes_by_agent_did(self):
        store = FakeStore(calls=[_call("e1", "m")])
        await query_llm_calls(store, agent_did="did:agent:xyz", limit=10)
        assert store.last_query_kwargs["agent_did"] == "did:agent:xyz"

    @pytest.mark.asyncio
    async def test_model_filter_forwarded(self):
        store = FakeStore(calls=[_call("e1", "a"), _call("e2", "b")])
        out = await query_llm_calls(store, model="b", limit=10)
        assert [c["event_id"] for c in out["calls"]] == ["e2"]

    @pytest.mark.asyncio
    async def test_offset_slices_results(self):
        store = FakeStore(calls=[_call(f"e{i}", "m") for i in range(5)])
        out = await query_llm_calls(store, limit=2, offset=2)
        assert out["offset"] == 2
        assert [c["event_id"] for c in out["calls"]] == ["e2", "e3"]

    @pytest.mark.asyncio
    async def test_empty_store(self):
        store = FakeStore(calls=[])
        out = await query_llm_calls(store, limit=10)
        assert out == {"calls": [], "count": 0, "limit": 10, "offset": 0}


class TestGetLLMStatsHelper:
    @pytest.mark.asyncio
    async def test_merges_store_stats_and_adds_percentiles(self):
        calls = [_call(f"e{i}", "m", duration_ms=d)
                 for i, d in enumerate([10, 20, 30, 40, 100])]
        store = FakeStore(
            calls=calls,
            stats={
                "total_calls": 5,
                "success_rate": 80.0,
                "avg_duration_ms": 40,
                "total_input_tokens": 50,
                "total_output_tokens": 100,
                "calls_by_provider": {"anthropic": 5},
                "calls_by_model": {"m": 5},
            },
        )
        out = await get_llm_stats(store, since=None, period_hours=24)
        assert out["total_calls"] == 5
        assert out["success_count"] == 4  # 80% of 5
        assert out["period_hours"] == 24
        assert out["latency_ms"]["avg"] == 40
        assert out["latency_ms"]["p95"] >= out["latency_ms"]["p50"]
        assert out["latency_ms"]["p99"] <= 100

    @pytest.mark.asyncio
    async def test_percentiles_zero_when_no_calls(self):
        store = FakeStore(calls=[], stats={"total_calls": 0, "success_rate": 0})
        out = await get_llm_stats(store)
        assert out["latency_ms"]["p95"] == 0
        assert out["success_count"] == 0


# ---------------------------------------------------------------------------
# 2. FastAPI router
# ---------------------------------------------------------------------------

def _make_app(store):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(get_router())
    app.state.agent = SimpleNamespace(agent_id="did:agent:1", observability_store=store)
    return app


class TestRouter:
    def test_prefix(self):
        assert API_PREFIX == "/api/observability"

    def test_llm_calls_endpoint(self):
        from fastapi.testclient import TestClient

        store = FakeStore(calls=[_call("e1", "claude-opus-4-8")])
        client = TestClient(_make_app(store))
        resp = client.get("/api/observability/llm-calls?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["calls"][0]["model"] == "claude-opus-4-8"
        # per-agent scoping applied
        assert store.last_query_kwargs["agent_did"] == "did:agent:1"

    def test_llm_calls_status_filter(self):
        from fastapi.testclient import TestClient

        store = FakeStore(calls=[_call("ok", "m"), _call("bad", "m", success=False)])
        client = TestClient(_make_app(store))
        resp = client.get("/api/observability/llm-calls?success=false")
        assert resp.status_code == 200
        assert [c["event_id"] for c in resp.json()["calls"]] == ["bad"]

    def test_llm_stats_endpoint(self):
        from fastapi.testclient import TestClient

        store = FakeStore(
            calls=[_call("e1", "m", duration_ms=50)],
            stats={"total_calls": 1, "success_rate": 100.0, "avg_duration_ms": 50,
                   "total_input_tokens": 10, "total_output_tokens": 20,
                   "calls_by_provider": {}, "calls_by_model": {}},
        )
        client = TestClient(_make_app(store))
        resp = client.get("/api/observability/llm-stats?hours_ago=12")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_calls"] == 1
        assert body["period_hours"] == 12
        assert "latency_ms" in body
        assert "since" in body

    def test_llm_calls_503_when_store_missing(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(get_router())
        app.state.agent = SimpleNamespace(agent_id="did:agent:1", observability_store=None)
        client = TestClient(app)
        resp = client.get("/api/observability/llm-calls")
        assert resp.status_code == 503
