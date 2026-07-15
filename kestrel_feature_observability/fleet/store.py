"""Fleet observability store — the data plane behind the host-root endpoints.

Owns an entities :class:`SessionFactory` (its own async engine), a fleet
``TenantContext`` (every read/write is scoped, fail-closed), and the pub/sub
backplane the live stream fans out over.

Lifecycle: :meth:`open` binds the engine + ensures the schema; :meth:`close`
disposes it. Ingest bulk-inserts redacted events and publishes each to the
backplane; :meth:`query` and :meth:`tree` serve the read side.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from kestrel_feature_entities import (
    EntityBase,
    PrivacyMode,
    SessionFactory,
    TenantContext,
    resolve_engine_target,
)
from sqlalchemy import select

from .backplane import InProcessPubSub, PubSub
from .models import EVENT_TYPES, ObservabilityEvent
from .redaction import redact_metadata


class IngestError(Exception):
    """Malformed ingest event → surfaced by the router as HTTP 422."""


def _coerce_ts(value: Any) -> datetime:
    """Parse an inbound ``ts`` (ISO string / epoch seconds) → aware datetime."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IngestError(f"invalid ts: {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise IngestError(f"invalid ts: {value!r}")


def _shorten_did(value: Optional[str]) -> Optional[str]:
    """Defensive polish (Q1): render a DID-shaped label shortened, else as-is.

    Never resolves an identity — just aliases a raw ``did:…`` so the tree stays
    readable if a producer forgot to emit a friendly name. No column, no lookup.
    """
    if value and value.startswith("did:"):
        tail = value.rsplit(":", 1)[-1]
        short = tail[:10] + "…" if len(tail) > 10 else tail
        return f"did:…{short}"
    return value


class FleetObservabilityStore:
    """Tenant-scoped fleet event store + live-stream publisher."""

    def __init__(
        self,
        session_factory: SessionFactory,
        tenant_id: uuid.UUID,
        pubsub: PubSub,
    ) -> None:
        self._factory = session_factory
        #: Zero-config fallback tenant (INV-SOLO). Used whenever a request
        #: resolves no tenant, so a solo deployment works with no config.
        self._default_tenant_id = tenant_id
        self._pubsub = pubsub

    def _resolve(self, tenant_id: Optional[uuid.UUID]) -> uuid.UUID:
        """Per-request tenant, falling back to the default (INV-SOLO)."""
        return tenant_id if tenant_id is not None else self._default_tenant_id

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    async def open(
        cls,
        engine_url: str,
        tenant_id: uuid.UUID,
        *,
        mode: PrivacyMode = PrivacyMode.NORMAL,
        pubsub: Optional[PubSub] = None,
    ) -> "FleetObservabilityStore":
        """Bind an engine at ``engine_url``, ensure the schema, start pub/sub."""
        target = resolve_engine_target(mode, engine_url)
        factory = SessionFactory(target, mode)
        pubsub = pubsub or InProcessPubSub()
        await pubsub.start()
        store = cls(factory, tenant_id, pubsub)
        await store._ensure_schema()
        return store

    async def _ensure_schema(self) -> None:
        """Create the observability table if absent.

        The shipped Alembic revision is the source of truth for managed
        (Postgres) deployments; this idempotent create verifies/materialises the
        table for self-contained/volatile bindings. Restricted (via
        ``checkfirst``) to the observability table only.

        ``tenant_id`` carries no foreign key to ``tenants`` (see
        :class:`~kestrel_feature_observability.fleet.models.ObservabilityEvent`),
        so no ``tenants`` table need exist for ``create_all`` to emit the schema
        or for ingest to succeed — tenant isolation is enforced by
        ``TenantContext`` at the session layer, not by referential integrity.
        """
        async with self._factory.engine.begin() as conn:
            await conn.run_sync(
                EntityBase.metadata.create_all,
                tables=[ObservabilityEvent.__table__],
                checkfirst=True,
            )

    async def close(self) -> None:
        await self._pubsub.stop()
        await self._factory.dispose()

    @property
    def pubsub(self) -> PubSub:
        return self._pubsub

    @property
    def tenant_id(self) -> uuid.UUID:
        """The zero-config default tenant (INV-SOLO fallback)."""
        return self._default_tenant_id

    # -- ingest -------------------------------------------------------------

    @staticmethod
    def _validate(event: Any) -> None:
        if not isinstance(event, dict):
            raise IngestError("event must be an object")
        event_type = event.get("event_type")
        if event_type not in EVENT_TYPES:
            raise IngestError(f"unknown event_type: {event_type!r}")
        if not event.get("agent_name"):
            raise IngestError("agent_name is required")
        if not event.get("session_id"):
            raise IngestError("session_id is required")

    def _build(self, event: dict, tenant_id: uuid.UUID) -> ObservabilityEvent:
        self._validate(event)
        metadata = event.get("metadata")
        redacted = redact_metadata(dict(metadata)) if isinstance(metadata, dict) else None
        return ObservabilityEvent(
            tenant_id=tenant_id,
            orchestrator=event.get("orchestrator"),
            agent_name=event["agent_name"],
            session_id=event["session_id"],
            event_type=event["event_type"],
            tool_name=event.get("tool_name"),
            duration_ms=event.get("duration_ms"),
            success=event.get("success"),
            error_message=event.get("error_message"),
            event_metadata=redacted,
            ts=_coerce_ts(event.get("ts")),
            workflow_run_id=event.get("workflow_run_id"),
            stage=event.get("stage"),
        )

    async def ingest(
        self, events: list[dict], *, tenant_id: Optional[uuid.UUID] = None
    ) -> list[str]:
        """Validate, redact, and **bulk insert** ``events``; publish each live.

        Events are stamped with the per-request ``tenant_id`` (or the zero-config
        default when none is resolved). Returns the created event ids.
        All-or-nothing: any invalid event raises :class:`IngestError` before
        anything is written.
        """
        resolved = self._resolve(tenant_id)
        rows = [self._build(e, resolved) for e in events]  # validate all up front
        with TenantContext.use(resolved):
            async with self._factory.write_session() as session:
                session.add_all(rows)
                await session.flush()
                created = [row.to_dict() for row in rows]
        for payload in created:
            await self._pubsub.publish(payload)
        return [row["id"] for row in created]

    # -- query --------------------------------------------------------------

    async def query(
        self,
        *,
        orchestrator: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        subtree: bool = False,
        limit: int = 200,
        tenant_id: Optional[uuid.UUID] = None,
    ) -> list[dict]:
        """Filter events. With ``subtree`` and an ``agent_name``, include events
        that agent orchestrates (``orchestrator == agent_name``) as well as its
        own — the whole subtree rather than just its direct events.
        """
        stmt = select(ObservabilityEvent)
        if orchestrator is not None:
            stmt = stmt.where(ObservabilityEvent.orchestrator == orchestrator)
        if agent_name is not None:
            if subtree:
                stmt = stmt.where(
                    (ObservabilityEvent.agent_name == agent_name)
                    | (ObservabilityEvent.orchestrator == agent_name)
                )
            else:
                stmt = stmt.where(ObservabilityEvent.agent_name == agent_name)
        if session_id is not None:
            stmt = stmt.where(ObservabilityEvent.session_id == session_id)
        if since is not None:
            stmt = stmt.where(ObservabilityEvent.ts >= since)
        if until is not None:
            stmt = stmt.where(ObservabilityEvent.ts <= until)
        stmt = stmt.order_by(ObservabilityEvent.ts.desc()).limit(limit)

        with TenantContext.use(self._resolve(tenant_id)):
            async with self._factory.read_session() as session:
                result = await session.execute(stmt)
                return [row.to_dict() for row in result.scalars().all()]

    # -- tree ---------------------------------------------------------------

    async def tree(self, *, tenant_id: Optional[uuid.UUID] = None) -> dict:
        """Orchestrator → agents grouping over stored friendly names.

        Groups by the ``orchestrator`` / ``agent_name`` values **as stored** (no
        DID resolution). ``orchestrator = null`` collects under a top-level
        ``"Direct"`` node. Scoped to the per-request ``tenant_id`` (or default).
        """
        stmt = select(
            ObservabilityEvent.orchestrator,
            ObservabilityEvent.agent_name,
        )
        with TenantContext.use(self._resolve(tenant_id)):
            async with self._factory.read_session() as session:
                result = await session.execute(stmt)
                pairs = result.all()

        # orchestrator label -> {agent_name -> count}
        groups: dict[Optional[str], dict[str, int]] = {}
        for orchestrator, agent_name in pairs:
            agents = groups.setdefault(orchestrator, {})
            agents[agent_name] = agents.get(agent_name, 0) + 1

        nodes = []
        for orchestrator in sorted(groups, key=lambda o: (o is not None, o or "")):
            agents = groups[orchestrator]
            is_direct = orchestrator is None
            label = "Direct" if is_direct else _shorten_did(orchestrator)
            children = [
                {
                    "agent_name": name,
                    "label": _shorten_did(name),
                    "event_count": count,
                }
                for name, count in sorted(agents.items())
            ]
            nodes.append(
                {
                    "orchestrator": orchestrator,
                    "label": label,
                    "is_direct": is_direct,
                    "event_count": sum(agents.values()),
                    "agents": children,
                }
            )
        return {"tree": nodes}


__all__ = ["FleetObservabilityStore", "IngestError"]
