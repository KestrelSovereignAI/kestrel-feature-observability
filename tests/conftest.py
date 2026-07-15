"""Shared fixtures for the fleet observability tests."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from kestrel_feature_observability.fleet.feature import FLEET_TENANT_ID
from kestrel_feature_observability.fleet.store import FleetObservabilityStore


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return FLEET_TENANT_ID


@pytest.fixture
def db_url(tmp_path) -> str:
    """A self-contained SQLite binding, one file per test."""
    return f"sqlite+aiosqlite:///{tmp_path / 'obs.db'}"


@pytest_asyncio.fixture
async def store(db_url, tenant_id):
    """An opened, tenant-scoped store bound to a throwaway SQLite file."""
    store = await FleetObservabilityStore.open(db_url, tenant_id)
    try:
        yield store
    finally:
        await store.close()


def make_event(**overrides) -> dict:
    """A valid ingest event with sensible defaults; override any field."""
    event = {
        "agent_name": "talon:acme/widgets#42",
        "session_id": "sess-1",
        "event_type": "tool_call",
        "tool_name": "Bash",
    }
    event.update(overrides)
    return event
