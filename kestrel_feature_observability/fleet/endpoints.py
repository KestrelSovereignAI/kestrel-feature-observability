"""Host-root observability router + the streamable live stream.

Mounted at the host root (no agent prefix) by :meth:`HostFeature.get_router`.
Endpoints:

* ``POST /api/host/observability/events`` — ingest (single or ``{"events":[…]}``
  batch), bulk insert, metadata redaction, 422 on unknown ``event_type`` or
  missing ``agent_name``/``session_id``.
* ``GET  /api/host/observability/events`` — filter by orchestrator/agent/session/time
  (+ ``subtree=true``).
* ``GET  /api/host/observability/tree`` — orchestrator tree with a ``Direct`` node.
* ``GET  /api/host/observability/stream`` — Streamable-HTTP live stream: one
  fetch-based endpoint, body streamed as ``text/event-stream`` with a session id
  + ``Last-Event-ID`` resumability, fanned out behind the pub/sub backplane.

Auth is the host's (cookie or API-key header — no URL token). State-changing
routes use the host CSRF via :func:`_enforce_csrf`; ingest is API-key (machine)
and therefore CSRF-exempt.
"""

import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Union

from .store import FleetObservabilityStore, IngestError

logger = logging.getLogger(__name__)

API_PREFIX = "/api/host/observability"

#: A host-supplied tenant resolver: maps a request to its tenant. May return the
#: :class:`uuid.UUID` directly or an awaitable of one. When absent (or it
#: returns ``None``), the store falls back to its zero-config default tenant.
TenantResolver = Callable[[Any], Union[uuid.UUID, Awaitable[Optional[uuid.UUID]], None]]


async def _resolve_tenant(
    resolver: Optional[TenantResolver], request: Any
) -> Optional[uuid.UUID]:
    """Resolve the per-request tenant, awaiting an async resolver if needed.

    Returns ``None`` when no resolver is wired so the store falls back to the
    zero-config default tenant (INV-SOLO).
    """
    if resolver is None:
        return None
    result = resolver(request)
    if inspect.isawaitable(result):
        result = await result
    return result


def _enforce_csrf(request: Any) -> None:
    """Apply the host CSRF check to a state-changing, cookie-authed request.

    Lazily imports the host helper (``kestrel_sovereign.security.csrf``: cookie
    ``kestrel_csrf`` / header ``X-CSRF-Token``) so the package imports without
    the full framework. No-op when the host isn't present. Ingest does **not**
    call this — it is API-key (machine) and CSRF-exempt.
    """
    try:
        from kestrel_sovereign.security.csrf import enforce_csrf
    except Exception:  # pragma: no cover - framework absent (tests / standalone)
        return
    enforce_csrf(
        request, authed_via_cookie=True
    )  # pragma: no cover - requires the host


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sse_frame(stream_id: int, event: dict) -> str:
    """Encode one Server-Sent-Events frame with an ``id:`` for resumability."""
    return f"id: {stream_id}\ndata: {json.dumps(event)}\n\n"


async def event_stream(
    store: FleetObservabilityStore,
    *,
    last_event_id: Optional[int] = None,
    session_id: str,
    tenant_id: Optional[uuid.UUID] = None,
) -> AsyncIterator[str]:
    """Yield SSE frames: a session preamble, replayed backlog, then live events.

    Resumable — replays buffered frames whose id is greater than
    ``last_event_id`` before switching to live delivery. The ``id:`` on each
    frame is echoed back by the browser as ``Last-Event-ID`` on reconnect.

    Fail-closed per tenant: a subscriber only sees frames for its resolved
    tenant (``tenant_id`` or the store's zero-config default). The backplane
    ring is shared across tenants, so frames are filtered here by ``tenant_id``.
    """
    tenant_str = str(tenant_id if tenant_id is not None else store.tenant_id)
    yield f": stream {session_id}\n\n"
    yield "retry: 3000\n\n"
    subscription = store.pubsub.subscribe(last_event_id=last_event_id)
    try:
        async for stream_id, event in subscription:
            if event.get("tenant_id") != tenant_str:
                continue
            yield _sse_frame(stream_id, event)
    finally:
        await subscription.aclose()


def get_router(
    store_provider: Callable[[], Optional[FleetObservabilityStore]],
    tenant_resolver: Optional[TenantResolver] = None,
) -> Any:
    """Build the host-root ``APIRouter``.

    ``store_provider`` returns the live store (or ``None`` before host start);
    routes 503 until the store is open.

    ``tenant_resolver`` (host-supplied) resolves each request to its tenant so
    ingest/query/stream are isolated per caller. When ``None`` (or it resolves
    ``None``), the store falls back to its zero-config default tenant so a solo
    deployment works with no configuration (INV-SOLO).
    """
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.responses import StreamingResponse

    router = APIRouter(prefix=API_PREFIX, tags=["observability-fleet"])

    def _store() -> FleetObservabilityStore:
        store = store_provider()
        if store is None:
            raise HTTPException(503, "observability store not started")
        return store

    @router.post("/events")
    async def post_events(request: Request) -> dict:
        # API-key (machine) surface → CSRF-exempt (no _enforce_csrf here).
        store = _store()
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")

        if isinstance(payload, dict) and "events" in payload:
            events = payload.get("events")
        elif isinstance(payload, dict):
            events = [payload]
        elif isinstance(payload, list):
            events = payload
        else:
            raise HTTPException(422, "body must be an event object or {\"events\": [...]}")

        if not isinstance(events, list) or not events:
            raise HTTPException(422, "no events to ingest")

        tenant_id = await _resolve_tenant(tenant_resolver, request)
        try:
            ids = await store.ingest(events, tenant_id=tenant_id)
        except IngestError as exc:
            raise HTTPException(422, str(exc))
        return {"ingested": len(ids), "ids": ids}

    @router.get("/events")
    async def get_events(
        request: Request,
        orchestrator: Optional[str] = Query(None),
        agent_name: Optional[str] = Query(None),
        session_id: Optional[str] = Query(None),
        since: Optional[str] = Query(None),
        until: Optional[str] = Query(None),
        subtree: bool = Query(False),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict:
        store = _store()
        tenant_id = await _resolve_tenant(tenant_resolver, request)
        try:
            events = await store.query(
                orchestrator=orchestrator,
                agent_name=agent_name,
                session_id=session_id,
                since=_parse_ts(since),
                until=_parse_ts(until),
                subtree=subtree,
                limit=limit,
                tenant_id=tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"events": events, "count": len(events)}

    @router.get("/tree")
    async def get_tree(request: Request) -> dict:
        store = _store()
        tenant_id = await _resolve_tenant(tenant_resolver, request)
        return await store.tree(tenant_id=tenant_id)

    @router.get("/stream")
    async def stream(request: Request) -> Any:
        store = _store()
        # Resumability: the browser replays the last frame id it saw.
        header_id = request.headers.get("Last-Event-ID")
        last_event_id = int(header_id) if header_id and header_id.isdigit() else None
        session_id = uuid_hex()
        tenant_id = await _resolve_tenant(tenant_resolver, request)
        generator = event_stream(
            store,
            last_event_id=last_event_id,
            session_id=session_id,
            tenant_id=tenant_id,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Stream-Session": session_id,
            },
        )

    return router


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


__all__ = ["get_router", "event_stream", "API_PREFIX"]
