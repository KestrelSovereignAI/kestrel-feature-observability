"""Fleet-scoped observability HostFeature (the ``[fleet]`` extra).

This subpackage ships the host-scoped ``FleetObservabilityHostFeature``
(discovered via the ``kestrel_sovereign.host_features`` entry-point group) that
owns the fleet-wide observability store, host-root ingest/query endpoints, a
streamable live stream, and the orchestrator swimlane panel. It is the fleet
*consumer*; the per-agent *producer* (the emitter hook) lives in the parent
``kestrel_feature_observability`` package and carries **no** ``entities``
dependency.

The store's heavy dependencies (``kestrel-feature-entities`` + SQLAlchemy) live
behind the ``kestrel-feature-observability[fleet]`` extra. The imports below are
**guarded**: on a base emitter install where the extra is absent, importing this
subpackage degrades to ``FleetObservabilityHostFeature is None`` instead of
raising, so the guarded ``host_features`` entry point simply resolves to ``None``
and the host skips it.
"""

try:
    from kestrel_feature_observability.fleet.feature import (
        FLEET_TENANT_ID,
        FleetObservabilityHostFeature,
    )
    from kestrel_feature_observability.fleet.models import (
        EVENT_TYPES,
        GATE_EVENT_TYPES,
        GATE_KINDS,
        ObservabilityEvent,
    )
    from kestrel_feature_observability.fleet.redaction import redact_metadata
    from kestrel_feature_observability.fleet.store import (
        FleetObservabilityStore,
        IngestError,
    )

    __all__ = [
        "FleetObservabilityHostFeature",
        "FLEET_TENANT_ID",
        "FleetObservabilityStore",
        "IngestError",
        "ObservabilityEvent",
        "EVENT_TYPES",
        "GATE_EVENT_TYPES",
        "GATE_KINDS",
        "redact_metadata",
    ]
except ImportError:  # pragma: no cover - [fleet] extra (entities) not installed
    FleetObservabilityHostFeature = None
    __all__ = ["FleetObservabilityHostFeature"]
