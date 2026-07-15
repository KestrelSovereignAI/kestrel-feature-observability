"""Fleet observability entity model.

A single fleet-wide, tenant-scoped event table. Every read/write is scoped by
``TenantMixin`` + ``TenantContext`` (fail-closed: no active tenant → no rows).

The model is the *consumer/aggregator* side of the observability split — it only
**accepts and indexes** events. Emission (including the friendly ``agent_name`` /
``orchestrator`` values and the gate lifecycle events) is the producer's job in
Phase 3; here we just persist what arrives.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from kestrel_feature_entities import AuditMixin, EntityBase, TenantMixin, UUIDPrimaryKey
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# ---------------------------------------------------------------------------
# Accepted event types (issue Phase 2 scope). Emission is Phase 3 — the model
# only accepts + indexes these; unknown types are rejected at ingest (HTTP 422).
# ---------------------------------------------------------------------------

#: Core telemetry event types.
CORE_EVENT_TYPES = frozenset(
    {
        "tool_call",
        "tool_response",
        "agent_response",
        "subagent_call",
        "subagent_response",
        "error",
        "metric",
    }
)

#: Gate lifecycle event types. Gate events carry ``metadata.gate`` +
#: ``metadata.attempt`` (see :data:`GATE_KINDS`).
GATE_EVENT_TYPES = frozenset({"gate_started", "gate_passed", "gate_failed"})

#: The full set of accepted ``event_type`` values.
EVENT_TYPES = CORE_EVENT_TYPES | GATE_EVENT_TYPES

#: Recognised ``metadata.gate`` values carried on gate lifecycle events. Not
#: enforced here (emission is Phase 3); exposed for producers/tests.
GATE_KINDS = frozenset(
    {"self-review", "quality", "integration", "eye", "verify", "ci", "demo"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ObservabilityEvent(EntityBase, TenantMixin, AuditMixin):
    """A single fleet observability event.

    Tenant-scoped (``TenantMixin``) and audited (``AuditMixin``); the primary
    key is a time-ordered UUIDv7.

    Friendly names are **stored, not resolved**: ``agent_name`` and
    ``orchestrator`` hold the display values the producer emitted. ``/tree``
    groups by these as-stored — there is deliberately no ``did`` column and no
    identity-resolver dependency (Q1: store-and-display).

    Workflow correlation (``workflow_run_id`` + ``stage``) is correlation-only:
    both are indexed but carry **no foreign key** — authoritative run state
    lives in the workflows' own store.
    """

    __tablename__ = "observability_events"

    id: Mapped[UUIDPrimaryKey]

    # Override ``TenantMixin.tenant_id`` to drop its ``FK(tenants.id)``. This
    # package does not own or ship a ``tenants`` table (the host does, behind the
    # ``[fleet]`` extra), and the fleet store's zero-config default tenant is
    # synthetic — no matching ``tenants`` row is guaranteed to exist. A hard FK
    # would fail closed at ingest (FK violation) and impose a migration-ordering
    # dependency on a table we don't manage. Tenant isolation does NOT rely on
    # the FK: ``TenantContext`` scopes every query by the ``tenant_id`` column
    # value (fail-closed) at the session layer, so the column alone suffices.
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    # --- lineage / grouping ------------------------------------------------
    #: Driving agent's friendly name; ``None`` renders under a "Direct" node.
    orchestrator: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # --- event payload -----------------------------------------------------
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ``metadata`` is reserved on the declarative base, so the Python attribute
    # is ``event_metadata`` while the DB column stays ``metadata`` (the wire
    # field). Redacted at ingest.
    event_metadata: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSON, nullable=True
    )

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    # --- workflow correlation (no FK — correlation only) -------------------
    workflow_run_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    stage: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict (wire ``metadata`` field name)."""
        return {
            "id": str(self.id),
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "orchestrator": self.orchestrator,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "tool_name": self.tool_name,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error_message": self.error_message,
            "metadata": self.event_metadata or {},
            "ts": self.ts.isoformat() if self.ts else None,
            "workflow_run_id": self.workflow_run_id,
            "stage": self.stage,
        }


__all__ = [
    "ObservabilityEvent",
    "EVENT_TYPES",
    "CORE_EVENT_TYPES",
    "GATE_EVENT_TYPES",
    "GATE_KINDS",
]
