"""Fleet observability ``HostFeature``.

The fleet *consumer/aggregator*: owns a fleet-wide, tenant-scoped event store
(via ``kestrel-feature-entities``), serves the host-root ingest/query endpoints
and a Streamable-HTTP live stream, and ships the orchestrator swimlane panel.
Discovered at host scope via the ``kestrel_sovereign.host_features`` entry point.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from kestrel_sdk import HostContext, HostFeature, UIContributions
from kestrel_sdk.storage.database import PrivacyMode

from .backplane import create_pubsub
from .endpoints import TenantResolver, get_router
from .store import FleetObservabilityStore

logger = logging.getLogger(__name__)

#: Fixed fleet tenant. The store is a single fleet-wide, tenant-scoped view; all
#: rows live under this tenant so ``TenantContext`` fails closed for anything
#: else. Override via host config key ``observability_fleet_tenant_id``.
FLEET_TENANT_ID = uuid.UUID("f1ee7000-0b5e-7000-8000-000000000001")

#: Default engine URL when the host config supplies none (self-contained dev).
DEFAULT_DB_URL = "sqlite+aiosqlite:///observability_fleet.db"


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping-like or attribute-style host config."""
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return getattr(config, key, default)


class FleetObservabilityHostFeature(HostFeature):
    """Host-scoped fleet observability feature.

    Owns a fleet-wide, tenant-scoped event store (via ``kestrel-feature-entities``)
    and serves host-root ingest/query + a streamable live stream, plus the
    orchestrator swimlane console panel. Discovered at host scope, not per-agent.
    """

    #: Stable slug used for mount path / capability gating.
    name = "observability-fleet"

    def __init__(self) -> None:
        self._store: Optional[FleetObservabilityStore] = None
        #: Host-supplied per-request tenant resolver (INV-SOLO: ``None`` until a
        #: host wires one, so a solo deployment falls back to the default tenant).
        self._tenant_resolver: Optional[TenantResolver] = None

    # -- routing ------------------------------------------------------------

    def get_router(self) -> Any:
        """Host-root router; reads the live store lazily (503 until started).

        Wires the per-request tenant resolver via :meth:`_resolve_request_tenant`
        so the host-supplied resolver (set from config in :meth:`on_host_start`)
        flows into ingest/query/tree/stream. Read lazily at request time — the
        router can be mounted before the host starts.
        """
        return get_router(lambda: self._store, self._resolve_request_tenant)

    def _resolve_request_tenant(self, request: Any) -> Any:
        """Delegate to the host-supplied resolver, or ``None`` (INV-SOLO).

        Returning ``None`` (no resolver wired, or the resolver returned ``None``)
        makes the store fall back to its zero-config default tenant so a solo
        deployment keeps working with no configuration.
        """
        resolver = self._tenant_resolver
        if resolver is None:
            return None
        return resolver(request)

    # -- lifecycle ----------------------------------------------------------

    async def on_host_start(self, ctx: HostContext) -> None:
        """Open the store engine + start the pub/sub backplane.

        Resolves the host engine target (SDK ``resolve_engine_target`` via
        :meth:`resolve_host_engine_target`), layers entities + a fleet
        ``TenantContext`` on top (the store does this), verifies/creates the
        schema, and starts the live-stream backplane.
        """
        config = getattr(ctx, "config", None)
        fallback_url = _config_get(config, "observability_fleet_db_url") or _config_get(
            config, "entities_db_url", DEFAULT_DB_URL
        )
        target = self.resolve_host_engine_target(fallback_url, mode=PrivacyMode.NORMAL)

        tenant_raw = _config_get(config, "observability_fleet_tenant_id")
        tenant_id = uuid.UUID(str(tenant_raw)) if tenant_raw else FLEET_TENANT_ID

        # De-pin the fixed tenant: adopt the host-supplied per-request resolver
        # if one is configured. Absent (solo/zero-config), requests resolve no
        # tenant and the store falls back to ``tenant_id`` above (INV-SOLO).
        resolver = _config_get(config, "observability_tenant_resolver")
        self._tenant_resolver = resolver if callable(resolver) else None

        backend = _config_get(config, "observability_backplane", "memory")
        dsn = _config_get(config, "observability_backplane_dsn")
        pubsub = create_pubsub(backend, dsn=dsn)

        self._store = await FleetObservabilityStore.open(
            target.url,
            tenant_id,
            mode=PrivacyMode.NORMAL,
            pubsub=pubsub,
        )
        logger.info(
            "FleetObservabilityHostFeature started (engine=%s, tenant=%s)",
            target.description,
            tenant_id,
        )

    async def on_host_stop(self, ctx: HostContext) -> None:
        """Dispose the engine and stop the backplane. Idempotent."""
        if self._store is not None:
            await self._store.close()
            self._store = None
        self._tenant_resolver = None
        logger.info("FleetObservabilityHostFeature stopped")

    # -- UI -----------------------------------------------------------------

    def get_ui_contributions(self) -> Optional[UIContributions]:
        """Ship the orchestrator swimlane console panel."""
        import os

        static_dir = os.path.join(os.path.dirname(__file__), "static")
        if not os.path.isdir(static_dir):
            return None
        return UIContributions(
            static_dir=static_dir,
            # Mount-relative to this feature's static dir. The host serves
            # ``static_dir`` at ``/host/features/{slug}/static`` and resolves each
            # module as ``{mount}/{path}``, so the path is relative to the static
            # root (the file ships at ``static/swimlane.js``) — no ``slug`` prefix
            # here (the host already adds it) or the URL 404s.
            modules=["swimlane.js"],
            # Host panels are always-on; keep the capability gate off (the
            # sovereign gate bug is fixed separately in
            # KestrelSovereignAI/kestrel-sovereign#2459).
            capability=None,
        )

    # -- convenience --------------------------------------------------------

    @property
    def store(self) -> Optional[FleetObservabilityStore]:
        return self._store


__all__ = ["FleetObservabilityHostFeature", "FLEET_TENANT_ID"]
