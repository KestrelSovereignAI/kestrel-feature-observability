"""create observability_events

Initial revision for the fleet observability event store. Mirrors
``kestrel_feature_observability.fleet.models.ObservabilityEvent``.

Revision ID: 0001_obs_events
Revises:
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_obs_events"
down_revision = None
branch_labels = None
depends_on = None

_TABLE = "observability_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("orchestrator", sa.String(length=255), nullable=True),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=255), nullable=True),
        sa.Column("stage", sa.String(length=255), nullable=True),
        # AuditMixin
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        # No foreign keys: ``tenant_id`` carries no FK to ``tenants`` (this
        # package does not own or ship that table, and the fleet store's default
        # tenant is synthetic — a hard FK would fail ingest and impose a
        # migration-ordering dependency on a table we don't manage). Tenant
        # isolation is enforced at the session layer by ``TenantContext``, which
        # scopes every query on the indexed ``tenant_id`` column. Workflow
        # correlation (``workflow_run_id``/``stage``) is likewise correlation-only.
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observability_events")),
    )
    for column in (
        "ts",
        "agent_name",
        "session_id",
        "orchestrator",
        "tenant_id",
        "workflow_run_id",
        "stage",
    ):
        op.create_index(
            op.f(f"ix_{_TABLE}_{column}"), _TABLE, [column], unique=False
        )


def downgrade() -> None:
    for column in (
        "stage",
        "workflow_run_id",
        "tenant_id",
        "orchestrator",
        "session_id",
        "agent_name",
        "ts",
    ):
        op.drop_index(op.f(f"ix_{_TABLE}_{column}"), table_name=_TABLE)
    op.drop_table(_TABLE)
