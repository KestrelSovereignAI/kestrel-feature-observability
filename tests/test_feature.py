"""FleetObservabilityHostFeature lifecycle + discovery wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kestrel_feature_observability.fleet.feature import (
    FLEET_TENANT_ID,
    FleetObservabilityHostFeature,
)


def _ctx(db_url: str) -> SimpleNamespace:
    return SimpleNamespace(config={"observability_fleet_db_url": db_url})


@pytest.mark.asyncio
async def test_on_host_start_opens_store_and_stop_closes(db_url):
    feature = FleetObservabilityHostFeature()
    assert feature.store is None

    await feature.on_host_start(_ctx(db_url))
    assert feature.store is not None
    assert feature.store.tenant_id == FLEET_TENANT_ID

    # Round-trips through the live store.
    ids = await feature.store.ingest(
        [{"agent_name": "a", "session_id": "s", "event_type": "metric"}]
    )
    assert len(ids) == 1

    await feature.on_host_stop(_ctx(db_url))
    assert feature.store is None


@pytest.mark.asyncio
async def test_on_host_stop_is_idempotent(db_url):
    feature = FleetObservabilityHostFeature()
    # Safe to stop before ever starting.
    await feature.on_host_stop(_ctx(db_url))
    assert feature.store is None


async def test_router_503_before_start(db_url):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    feature = FleetObservabilityHostFeature()
    app = FastAPI()
    app.include_router(feature.get_router())
    with TestClient(app) as client:
        resp = client.get("/api/host/observability/tree")
        assert resp.status_code == 503


def test_get_ui_contributions_ships_swimlane():
    feature = FleetObservabilityHostFeature()
    contributions = feature.get_ui_contributions()
    assert contributions is not None
    assert any("swimlane.js" in m for m in contributions.modules)


def test_ui_module_paths_are_mount_relative_and_shipped():
    """Every declared module URL must resolve to a shipped file.

    The host serves ``static_dir`` at ``/host/features/{slug}/static`` and
    resolves each module as ``{mount}/{path}`` — so a module path must be
    relative to ``static_dir`` (no leading slash, no ``{slug}`` prefix) and
    point at a real file, or the console ``import()`` 404s.
    """
    import os

    feature = FleetObservabilityHostFeature()
    contributions = feature.get_ui_contributions()
    assert contributions is not None
    assert contributions.static_dir is not None
    for module in contributions.modules:
        # Mount-relative: the host prepends the mount + slug itself.
        assert not module.startswith("/"), module
        assert not module.startswith(f"{feature.name}/"), module
        shipped = os.path.join(contributions.static_dir, module)
        assert os.path.isfile(shipped), shipped


@pytest.mark.asyncio
async def test_router_solo_default_when_no_resolver(db_url):
    """INV-SOLO: with no resolver configured, the production router still works."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    feature = FleetObservabilityHostFeature()
    await feature.on_host_start(_ctx(db_url))
    assert feature._tenant_resolver is None

    app = FastAPI()
    app.include_router(feature.get_router())
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/host/observability/events",
                json={"agent_name": "a", "session_id": "solo", "event_type": "metric"},
            )
            assert resp.status_code == 200
            events = client.get("/api/host/observability/events").json()["events"]
            assert {e["session_id"] for e in events} == {"solo"}
    finally:
        await feature.on_host_stop(_ctx(db_url))


@pytest.mark.asyncio
async def test_router_isolates_callers_via_host_supplied_resolver(db_url):
    """A host-configured resolver flows through the production feature path and
    isolates two callers' ingest+query (fail-closed)."""
    import uuid

    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    def resolver(request):
        return tenant_a if request.headers.get("X-Caller") == "a" else tenant_b

    ctx = SimpleNamespace(
        config={
            "observability_fleet_db_url": db_url,
            "observability_tenant_resolver": resolver,
        }
    )

    feature = FleetObservabilityHostFeature()
    await feature.on_host_start(ctx)
    assert feature._tenant_resolver is resolver

    app = FastAPI()
    app.include_router(feature.get_router())
    try:
        with TestClient(app) as client:
            client.post(
                "/api/host/observability/events",
                json={"agent_name": "a", "session_id": "from-a", "event_type": "metric"},
                headers={"X-Caller": "a"},
            )
            client.post(
                "/api/host/observability/events",
                json={"agent_name": "b", "session_id": "from-b", "event_type": "metric"},
                headers={"X-Caller": "b"},
            )
            a_events = client.get(
                "/api/host/observability/events", headers={"X-Caller": "a"}
            ).json()["events"]
            b_events = client.get(
                "/api/host/observability/events", headers={"X-Caller": "b"}
            ).json()["events"]
            assert {e["session_id"] for e in a_events} == {"from-a"}
            assert {e["session_id"] for e in b_events} == {"from-b"}
    finally:
        await feature.on_host_stop(ctx)


def test_entry_point_registered():
    from importlib.metadata import entry_points

    eps = entry_points(group="kestrel_sovereign.host_features")
    names = {ep.name for ep in eps}
    assert "FleetObservabilityHostFeature" in names
