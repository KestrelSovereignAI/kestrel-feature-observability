"""HTTP endpoints for the Observability feature — LLM diagnostics + data plane.

Read-side routes the Console's "LLM Calls" panel consumes:

* ``GET /api/observability/llm-calls`` — paged, filterable list of LLM call
  records written to the feature's observability store.
* ``GET /api/observability/llm-stats`` — aggregate stats (counts, tokens,
  latency avg + percentiles, per-provider / per-model breakdown) over a window.

Data-plane routes (issue #10) that let a swimlane UI reconstruct the fleet:

* ``POST /api/observability/events`` — external ingest. Out-of-process agents
  (notably kestrel-talon) push a single event or ``{"events":[...]}`` batch into
  the same store; each ``event_type`` maps to a ``store.log_*`` writer, inbound
  metadata is redacted, and parent lineage from the body is folded in.
* ``GET /api/observability/agent-tree`` — the spawn hierarchy (agent → children,
  recursively) built from ``AgentManager.get_children``.
* ``GET /api/observability/events`` — per-agent or whole-subtree event query.

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

import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Path prefix matches the downstream Frinz mount so migration is mechanical.
API_PREFIX = "/api/observability"

# Upper bound on paging offset. Paging over a cursor-less store fetches
# ``limit + offset`` rows, so an unbounded offset (``?offset=1000000000``) would
# materialize the whole table; cap the offset to keep the over-fetch bounded.
MAX_OFFSET = 100_000

# Row cap for the agent-scoped aggregate + percentile fetch in ``get_llm_stats``.
_STATS_FETCH_LIMIT = 10_000

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
    offset`` rows and slice — the natural cost of a cursor-less backend. Offset is
    clamped to ``MAX_OFFSET`` so a huge ``?offset=`` can't materialize the whole
    table.
    """
    offset = min(max(0, offset), MAX_OFFSET)
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

    Every aggregate (counts / tokens / avg-latency / percentiles / per-provider /
    per-model breakdown, the Frinz shape) is computed here from an *agent-scoped*
    ``query_llm_calls(agent_did=...)`` fetch. The store's ``get_llm_stats`` takes
    only time bounds, so in a multi-agent store it would mix in other agents'
    rows while the endpoint claims per-agent scoping — computing from the scoped
    query keeps counts/tokens/success/latency isolated to the requesting agent.
    """
    calls: List[Any] = []
    query_error = False
    try:
        raw = await store.query_llm_calls(
            agent_did=agent_did, since=since, limit=_STATS_FETCH_LIMIT
        )
        calls = list(raw)
    except Exception as exc:  # noqa: BLE001 - stats are supplementary, never fatal
        query_error = True
        logger.debug("llm-stats aggregate computation skipped: %s", exc)

    total_calls = len(calls)
    success_count = sum(1 for c in calls if getattr(c, "success", None))
    success_rate = round(success_count / total_calls * 100.0, 2) if total_calls else 0.0

    durations = sorted(float(getattr(c, "duration_ms", 0) or 0) for c in calls)
    avg_duration_ms = round(sum(durations) / total_calls, 2) if total_calls else 0

    calls_by_provider: Dict[str, int] = {}
    calls_by_model: Dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    for c in calls:
        provider = getattr(c, "provider", None)
        model = getattr(c, "model", None)
        calls_by_provider[provider] = calls_by_provider.get(provider, 0) + 1
        calls_by_model[model] = calls_by_model.get(model, 0) + 1
        total_input_tokens += int(getattr(c, "input_tokens", 0) or 0)
        total_output_tokens += int(getattr(c, "output_tokens", 0) or 0)

    stats: Dict[str, Any] = {
        "total_calls": total_calls,
        "success_count": success_count,
        "success_rate": success_rate,
        "avg_duration_ms": avg_duration_ms,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "calls_by_provider": calls_by_provider,
        "calls_by_model": calls_by_model,
        "latency_ms": {
            "avg": avg_duration_ms,
            "p50": round(_percentile(durations, 50), 2),
            "p95": round(_percentile(durations, 95), 2),
            "p99": round(_percentile(durations, 99), 2),
        },
        # The store has no agent-scoped aggregate, so every total above is
        # computed from a fetch capped at ``_STATS_FETCH_LIMIT``. When the cap is
        # hit the counts/tokens are a floor, not an exact total — flag it so the
        # consumer doesn't present truncated numbers as complete. ``error`` marks
        # a failed store fetch so an all-zeros body isn't mistaken for an idle
        # agent.
        "truncated": total_calls >= _STATS_FETCH_LIMIT,
        "error": query_error,
    }

    if period_hours is not None:
        stats["period_hours"] = period_hours
    if since is not None:
        stats["since"] = since.isoformat()
    return stats


# ---------------------------------------------------------------------------
# External ingest (POST /events) — out-of-process agents push here
# ---------------------------------------------------------------------------

# Keys whose values are scrubbed from inbound metadata before it is persisted.
# Mirrors the host's redaction intent so telemetry pushed by out-of-process
# agents (notably kestrel-talon) can't smuggle secrets into the store.
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|authorization|auth|"
    r"credential|private[_-]?key|cookie|session[_-]?token|bearer)",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"

# Top-level POST-body fields that carry parent/lineage, folded into metadata so
# a swimlane can nest talon/subagent sublanes under their driver.
_LINEAGE_FIELDS = (
    "parent_agent",
    "parent_session_id",
    "driven_by",
    "driver",
    "subagent_id",
    "child_session_id",
)

# event_type → the store method that persists it. ``agent_response`` has no
# ``log_`` prefix on the store, matching the host surface named in issue #10.
_INGEST_TYPES = {
    "tool_call": "tool_call",
    "tool_response": "tool_response",
    "subagent_call": "tool_call",
    "subagent_response": "tool_response",
    "error": "error",
    "metric": "metric",
    "agent_response": "agent_response",
}


class IngestError(Exception):
    """Raised for a malformed ingest event → surfaced as HTTP 422."""


def redact_metadata(metadata: Any, redactor: Optional[Any] = None) -> Dict[str, Any]:
    """Return a copy of ``metadata`` with sensitive values scrubbed.

    Prefers the host store's own ``redact_metadata`` when provided (issue #10:
    "reuse the host's redaction"); otherwise applies a local key-name scrub
    recursively over nested dicts/lists.
    """
    if redactor is not None:
        try:
            result = redactor(metadata)
            # Only trust a synchronous redactor that returns a mapping/list;
            # anything else (e.g. a coroutine) falls back to the local scrub.
            if isinstance(result, (dict, list)):
                return result
        except Exception:  # noqa: BLE001 - fall back to local scrub
            logger.debug("host redactor failed; using local scrub", exc_info=True)

    if isinstance(metadata, dict):
        out: Dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                out[key] = _REDACTED
            else:
                out[key] = redact_metadata(value, None)
        return out
    if isinstance(metadata, list):
        return [redact_metadata(v, None) for v in metadata]  # type: ignore[return-value]
    return metadata


def _build_ingest_metadata(event: Dict[str, Any], redactor: Optional[Any]) -> Dict[str, Any]:
    """Redact inbound metadata and fold in lineage fields from the body."""
    metadata = redact_metadata(dict(event.get("metadata") or {}), redactor)
    metadata.setdefault("event_category", event.get("event_type"))
    for field in _LINEAGE_FIELDS:
        value = event.get(field)
        if value is not None and field not in metadata:
            metadata[field] = value
    if event.get("session_id") is not None:
        metadata.setdefault("session_id", event["session_id"])
    return metadata


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, else return it as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


async def ingest_event(
    store: Any,
    event: Dict[str, Any],
    *,
    redactor: Optional[Any] = None,
) -> Any:
    """Persist a single external event via the matching ``store.log_*``.

    Required: ``agent_name`` and ``session_id``. Unknown ``event_type`` →
    ``IngestError`` (HTTP 422). Returns the created ``event_id`` (or ``None``).
    """
    if not isinstance(event, dict):
        raise IngestError("event must be an object")

    event_type = event.get("event_type")
    if event_type not in _INGEST_TYPES:
        raise IngestError(f"unknown event_type: {event_type!r}")

    agent_name = event.get("agent_name")
    session_id = event.get("session_id")
    if not agent_name:
        raise IngestError("agent_name is required")
    if not session_id:
        raise IngestError("session_id is required")

    metadata = _build_ingest_metadata(event, redactor)
    target = _INGEST_TYPES[event_type]

    if target == "tool_call":
        return await _maybe_await(
            store.log_tool_call(
                agent_name=agent_name,
                session_id=session_id,
                tool_name=event.get("tool_name"),
                metadata=metadata,
            )
        )
    if target == "tool_response":
        # Paired UPDATE when a correlation id is supplied; standalone INSERT
        # otherwise (a lone response posted by talon) — issue #10 Q2 Option 2.
        return await _maybe_await(
            store.log_tool_response(
                event_id=event.get("event_id") or event.get("tool_use_id"),
                agent_name=agent_name,
                session_id=session_id,
                tool_name=event.get("tool_name"),
                success=event.get("success", True),
                duration_ms=event.get("duration_ms"),
                metadata=metadata,
            )
        )
    if target == "error":
        return await _maybe_await(
            store.log_error(
                agent_name=agent_name,
                session_id=session_id,
                error_message=event.get("error_message") or event.get("error") or "",
                metadata=metadata,
            )
        )
    if target == "metric":
        return await _maybe_await(
            store.log_metric(
                agent_name=agent_name,
                metric_name=event.get("metric_name") or "external",
                metric_value=event.get("metric_value", 1),
                metadata=metadata,
            )
        )
    # agent_response
    return await _maybe_await(
        store.agent_response(
            agent_name=agent_name,
            session_id=session_id,
            metadata=metadata,
        )
    )


async def ingest_events(
    store: Any,
    payload: Any,
    *,
    redactor: Optional[Any] = None,
) -> Dict[str, Any]:
    """Ingest a single event or a ``{"events":[...]}`` batch.

    Returns ``{"event_id": ...}`` for a single event, or
    ``{"event_ids": [...], "count": N}`` for a batch.
    """
    if isinstance(payload, dict) and "events" in payload:
        events = payload.get("events") or []
        if not isinstance(events, list):
            raise IngestError("'events' must be a list")
        event_ids = [await ingest_event(store, e, redactor=redactor) for e in events]
        return {"event_ids": event_ids, "count": len(event_ids)}

    event_id = await ingest_event(store, payload, redactor=redactor)
    return {"event_id": event_id}


# ---------------------------------------------------------------------------
# Agent tree / subtree (GET /agent-tree, subtree events query)
# ---------------------------------------------------------------------------


def _agent_name_of(agent_manager: Any, did: str) -> str:
    """Resolve a DID to a human agent name via the manager, else the DID."""
    if agent_manager is not None:
        getter = getattr(agent_manager, "get_agent", None)
        if callable(getter):
            try:
                record = getter(did)
            except Exception:  # noqa: BLE001
                record = None
            if record is not None:
                name = getattr(record, "agent_name", None) or getattr(record, "name", None)
                if name:
                    return name
    return did


async def build_agent_tree(
    agent_manager: Any,
    root_did: str,
    *,
    _visited: Optional[set] = None,
) -> Dict[str, Any]:
    """Build the spawn hierarchy (agent → children, recursively) from a DID.

    Uses ``AgentManager.get_children`` (sync or async). Degrades to a single
    node with no children when the manager is unavailable. Cycle-safe.
    """
    visited = _visited if _visited is not None else set()
    node: Dict[str, Any] = {
        "agent_did": root_did,
        "agent_name": _agent_name_of(agent_manager, root_did),
        "children": [],
    }
    if root_did in visited:
        return node
    visited.add(root_did)

    if agent_manager is None or not hasattr(agent_manager, "get_children"):
        return node

    try:
        child_dids = await _maybe_await(agent_manager.get_children(root_did)) or []
    except Exception:  # noqa: BLE001 - degrade to a flat node
        logger.debug("get_children(%s) failed; flat node", root_did, exc_info=True)
        return node

    for child_did in child_dids:
        node["children"].append(
            await build_agent_tree(agent_manager, child_did, _visited=visited)
        )
    return node


async def collect_subtree_dids(agent_manager: Any, root_did: str) -> List[str]:
    """Flatten the spawn subtree rooted at ``root_did`` into a DID list."""
    ordered: List[str] = []
    seen: set = set()

    async def _walk(did: str) -> None:
        if did in seen:
            return
        seen.add(did)
        ordered.append(did)
        if agent_manager is None or not hasattr(agent_manager, "get_children"):
            return
        try:
            children = await _maybe_await(agent_manager.get_children(did)) or []
        except Exception:  # noqa: BLE001
            return
        for child in children:
            await _walk(child)

    await _walk(root_did)
    return ordered


def _serialize_event(e: Any) -> Dict[str, Any]:
    """Project a store event record into the JSON shape the UI consumes."""
    ts = getattr(e, "timestamp", None)
    return {
        "event_id": getattr(e, "event_id", None),
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts is not None else None),
        "agent_name": getattr(e, "agent_name", None),
        "agent_did": getattr(e, "agent_did", None),
        "event_type": getattr(e, "event_type", None),
        "tool_name": getattr(e, "tool_name", None),
        "session_id": getattr(e, "session_id", None),
        "duration_ms": getattr(e, "duration_ms", None),
        "success": getattr(e, "success", None),
        "error_message": getattr(e, "error_message", None),
        "metadata": getattr(e, "metadata", None),
    }


async def query_subtree_events(
    store: Any,
    agent_manager: Any,
    root_did: str,
    *,
    root_agent_name: Optional[str] = None,
    event_type: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 200,
    subtree: bool = True,
) -> Dict[str, Any]:
    """Union per-agent ``query_events`` across an agent + its descendants.

    Resolves the spawn subtree to DIDs, maps each to an agent name, and unions
    a bounded ``query_events`` fetch per agent. When ``subtree`` is False (or no
    manager is available) it degrades to the single root agent — issue #10 Q3
    Option 1. Lineage stored in each event's ``metadata`` lets the UI nest.
    """
    if subtree and agent_manager is not None:
        dids = await collect_subtree_dids(agent_manager, root_did)
        agent_names = [_agent_name_of(agent_manager, d) for d in dids]
    else:
        agent_names = [root_agent_name or _agent_name_of(agent_manager, root_did)]

    # De-dup names while preserving order.
    seen: set = set()
    unique_names = [n for n in agent_names if not (n in seen or seen.add(n))]

    events: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for name in unique_names:
        try:
            raw = await store.query_events(
                agent_name=name,
                event_type=event_type or None,
                since=since,
                limit=limit,
            )
        except TypeError:
            # Store's query_events may not accept agent_name/since kwargs.
            raw = await store.query_events(event_type=event_type or None, limit=limit)
        except Exception:  # noqa: BLE001 - one agent's failure is non-fatal
            logger.debug("query_events failed for agent %s", name, exc_info=True)
            continue
        for e in raw:
            serialized = _serialize_event(e)
            eid = serialized.get("event_id")
            if eid is not None and eid in seen_ids:
                continue
            if eid is not None:
                seen_ids.add(eid)
            events.append(serialized)

    return {
        "events": events,
        "count": len(events),
        "agents": unique_names,
        "subtree": bool(subtree and agent_manager is not None),
    }


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

    def _resolve_agent_manager(request: Request, agent):
        """Locate the spawn AgentManager (drives the agent-tree hierarchy)."""
        return (
            getattr(agent, "agent_manager", None)
            or getattr(request.state, "agent_manager", None)
            or getattr(request.app.state, "agent_manager", None)
        )

    def _redactor(store):
        """Host store's own metadata redaction, if it exposes one."""
        return getattr(store, "redact_metadata", None)

    @router.post("/events")
    async def post_events(request: Request):
        """Ingest external telemetry (single event or ``{"events":[...]}``).

        Out-of-process agents (e.g. kestrel-talon) push events into the same
        ``observability_store``. Best-effort/non-blocking; unknown ``event_type``
        or missing ``agent_name``/``session_id`` → 422.
        """
        agent = _resolve_agent(request)
        store = _store(agent)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid JSON body")
        try:
            return await ingest_events(store, payload, redactor=_redactor(store))
        except IngestError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            logger.exception("Failed to ingest observability events")
            raise HTTPException(500, "Failed to ingest events. Please try again.")

    @router.get("/agent-tree")
    async def agent_tree(request: Request):
        """Return the spawn hierarchy (agent → children, recursively)."""
        agent = _resolve_agent(request)
        manager = _resolve_agent_manager(request, agent)
        root_did = _agent_did(agent)
        if root_did is None:
            raise HTTPException(status_code=503, detail="Agent DID unavailable")
        try:
            return await build_agent_tree(manager, root_did)
        except Exception:
            logger.exception("Failed to build agent tree")
            raise HTTPException(500, "Failed to build agent tree. Please try again.")

    @router.get("/events")
    async def events(
        request: Request,
        event_type: Optional[str] = None,
        subtree: bool = Query(True),
        limit: int = Query(200, ge=1, le=1000),
        hours_ago: Optional[int] = Query(None, ge=1, le=168),
    ):
        """Query events for the agent, optionally across its whole subtree."""
        agent = _resolve_agent(request)
        store = _store(agent)
        manager = _resolve_agent_manager(request, agent)
        root_did = _agent_did(agent)
        since = (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            if hours_ago
            else None
        )
        try:
            return await query_subtree_events(
                store,
                manager,
                root_did,
                root_agent_name=getattr(agent, "agent_name", None),
                event_type=event_type,
                since=since,
                limit=limit,
                subtree=subtree,
            )
        except Exception:
            logger.exception("Failed to query subtree events")
            raise HTTPException(500, "Failed to query events. Please try again.")

    @router.get("/llm-calls")
    async def llm_calls(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
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
    "MAX_OFFSET",
    "get_router",
    "query_llm_calls",
    "get_llm_stats",
    "IngestError",
    "redact_metadata",
    "ingest_event",
    "ingest_events",
    "build_agent_tree",
    "collect_subtree_dids",
    "query_subtree_events",
]
