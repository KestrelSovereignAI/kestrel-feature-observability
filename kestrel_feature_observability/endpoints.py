"""HTTP query endpoints for the Observability feature — LLM call diagnostics.

Exposes two read-only routes the Console's "LLM Calls" panel consumes:

* ``GET /api/observability/llm-calls`` — paged, filterable list of LLM call
  records written to the feature's observability store.
* ``GET /api/observability/llm-stats`` — aggregate stats (counts, tokens,
  latency avg + percentiles, per-provider / per-model breakdown) over a window.

The response shapes mirror the proven downstream implementation in
``frinz/endpoints/observability.py`` so a host that already built its own
"LLM Calls" pane can delete the copy and point at these routes (the paths are
identical: ``/api/observability/llm-calls|llm-stats``).

This is the read side only — the write path (``hook.py`` → store) is untouched.

Records are scoped to the requesting agent via the standard agent context
(``request.state.agent`` in multi-agent mode, ``request.app.state.agent`` in
single-agent mode), mirroring ``kestrel_sovereign.endpoints.spawn``. The router
is built lazily so this module imports cleanly in environments without FastAPI
(the host always provides it); the pure query helpers below carry the logic and
are unit-testable without a running app.

Note: this module intentionally does NOT use ``from __future__ import
annotations``. FastAPI resolves endpoint parameter annotations (e.g. ``Request``)
against the module globals; since FastAPI is imported lazily inside
``get_router()`` those symbols are not module-global, so stringized annotations
would make FastAPI mistake ``request`` for a query parameter (→ 422).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Path prefix matches the downstream Frinz mount so migration is mechanical.
API_PREFIX = "/api/observability"

# Fields projected for each call — the exact set Frinz's pane rendered.
_CALL_FIELDS = (
    "event_id",
    "provider",
    "model",
    "companion_id",
    "agent_did",
    "session_id",
    "duration_ms",
    "success",
    "error_message",
    "system_prompt_preview",
    "user_prompt_preview",
    "response_preview",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "metadata",
)


def _agent_did(agent: Any) -> Optional[str]:
    """Resolve the agent's DID for per-agent scoping of the store query."""
    return getattr(agent, "agent_id", None) or getattr(agent, "did", None)


def _serialize_call(call: Any) -> Dict[str, Any]:
    """Project one ``LLMCallEvent`` into the JSON shape the panel consumes."""
    ts = getattr(call, "timestamp", None)
    out: Dict[str, Any] = {
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts is not None else None),
    }
    for field in _CALL_FIELDS:
        out[field] = getattr(call, field, None)
    return out


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list (empty → 0)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = pct / 100.0 * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


async def query_llm_calls(
    store: Any,
    *,
    agent_did: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    companion_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    success: Optional[bool] = None,
    since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return a paged, filtered list of LLM call records from the store.

    Reads via ``store.query_llm_calls`` (the host observability store). Paging is
    applied here: the store has no native offset, so we over-fetch ``limit +
    offset`` rows and slice — the natural cost of a cursor-less backend.
    """
    raw = await store.query_llm_calls(
        agent_did=agent_did,
        companion_id=companion_id,
        provider=provider,
        model=model,
        success=success,
        since=since,
        limit=max(0, limit) + max(0, offset),
    )
    window = list(raw)[offset: offset + limit] if offset else list(raw)[:limit]
    calls = [_serialize_call(c) for c in window]
    return {
        "calls": calls,
        "count": len(calls),
        "limit": limit,
        "offset": offset,
    }


async def get_llm_stats(
    store: Any,
    *,
    agent_did: Optional[str] = None,
    since: Optional[datetime] = None,
    period_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """Return aggregate LLM stats plus latency percentiles for the window.

    ``store.get_llm_stats`` supplies the counts / tokens / avg-latency /
    per-provider / per-model breakdown (the Frinz shape). Percentiles aren't part
    of that aggregate, so we derive p50/p95/p99 in Python from the window's
    durations via a bounded ``query_llm_calls`` fetch. A ``success_count`` is
    surfaced explicitly (the panel renders "N / total") alongside the store's
    ``success_rate``.
    """
    stats: Dict[str, Any] = dict(await store.get_llm_stats(since=since))

    total_calls = stats.get("total_calls") or 0
    success_rate = stats.get("success_rate") or 0
    stats.setdefault("success_count", round(total_calls * success_rate / 100.0))

    # Latency percentiles from the window's durations (best-effort — never let a
    # percentile failure sink the whole stats response).
    durations: List[float] = []
    try:
        calls = await store.query_llm_calls(
            agent_did=agent_did, since=since, limit=1000
        )
        durations = sorted(
            float(getattr(c, "duration_ms", 0) or 0) for c in calls
        )
    except Exception as exc:  # noqa: BLE001 - percentiles are supplementary
        logger.debug("llm-stats percentile computation skipped: %s", exc)

    stats["latency_ms"] = {
        "avg": stats.get("avg_duration_ms", 0),
        "p50": round(_percentile(durations, 50), 2),
        "p95": round(_percentile(durations, 95), 2),
        "p99": round(_percentile(durations, 99), 2),
    }

    if period_hours is not None:
        stats["period_hours"] = period_hours
    if since is not None:
        stats["since"] = since.isoformat()
    return stats


def get_router():
    """Build and return the FastAPI router for the observability query endpoints.

    Imported lazily so this module (and its pure helpers) load in environments
    without FastAPI; the host always has it. Mirrors ``get_router()`` on the
    Spawn feature.
    """
    from fastapi import APIRouter, HTTPException, Query, Request

    router = APIRouter(prefix=API_PREFIX, tags=["observability"])

    def _resolve_agent(request: Request):
        """Standard agent context: routed agent, then single-agent fallback."""
        agent = getattr(request.state, "agent", None)
        if agent is None:
            agent = getattr(request.app.state, "agent", None)
        if agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized.")
        return agent

    def _store(agent):
        store = getattr(agent, "observability_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="Observability not enabled")
        return store

    @router.get("/llm-calls")
    async def llm_calls(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        companion_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        success: Optional[bool] = None,
        hours_ago: Optional[int] = Query(None, ge=1, le=168),
    ):
        """Paged, filterable list of LLM calls for the requesting agent."""
        agent = _resolve_agent(request)
        store = _store(agent)
        since = (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            if hours_ago
            else None
        )
        try:
            return await query_llm_calls(
                store,
                agent_did=_agent_did(agent),
                limit=limit,
                offset=offset,
                companion_id=companion_id,
                provider=provider,
                model=model,
                success=success,
                since=since,
            )
        except Exception:
            logger.exception("Failed to query LLM calls")
            raise HTTPException(500, "Failed to query LLM calls. Please try again.")

    @router.get("/llm-stats")
    async def llm_stats(
        request: Request,
        hours_ago: int = Query(24, ge=1, le=168),
    ):
        """Aggregate LLM stats + latency percentiles over the last N hours."""
        agent = _resolve_agent(request)
        store = _store(agent)
        since = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        try:
            return await get_llm_stats(
                store,
                agent_did=_agent_did(agent),
                since=since,
                period_hours=hours_ago,
            )
        except Exception:
            logger.exception("Failed to get LLM stats")
            raise HTTPException(500, "Failed to get stats. Please try again.")

    return router


__all__ = [
    "API_PREFIX",
    "get_router",
    "query_llm_calls",
    "get_llm_stats",
]
