"""Fleet-scoped observability HostFeature (the ``[fleet]`` extra).

This subpackage ships the host-scoped ``FleetObservabilityHostFeature``
(discovered via the ``kestrel_sovereign.host_features`` entry-point group) that
ships the single "Observability" console panel embedding the host-supervised
Phoenix UI. The per-agent *producer* (the emitter hook) lives in the parent
``kestrel_feature_observability`` package.

Since the custom store/entities were retired (issue #39), ``fleet/feature.py``
imports only ``HostFeature``/``UIContributions`` from ``kestrel_sdk`` — so the
host role is gated by the **SDK version**, not by an extra-only importable
module. The ``[fleet]`` extra tightens the SDK pin (``>=0.30.0,<0.31``) to the
range that exports that contract; the base install keeps a wider SDK floor
(``>=0.14.1``). The import below is therefore **guarded**: on a modern SDK (the
host env, or any base env whose SDK resolved to ``>=0.30``) it binds the real
class, but if the resolved SDK is too old to export the HostFeature contract (or
the symbols are moved/renamed) importing this subpackage degrades to
``FleetObservabilityHostFeature is None`` — with a warning logged — instead of
raising, so the ``host_features`` entry point resolves to ``None`` and the host
simply skips the panel.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from kestrel_feature_observability.fleet.feature import (
        FleetObservabilityHostFeature,
    )

    __all__ = ["FleetObservabilityHostFeature"]
except ImportError as exc:  # pragma: no cover - host SDK contract too old/absent
    logger.warning(
        "FleetObservabilityHostFeature unavailable: the host SDK contract "
        "(kestrel_sdk.HostFeature/UIContributions) could not be imported (%s). "
        "The Observability console panel will be skipped; install/upgrade to an "
        "SDK that exports the HostFeature contract (kestrel-sovereign-sdk "
        ">=0.30.0,<0.31, e.g. via the [fleet] extra).",
        exc,
    )
    FleetObservabilityHostFeature = None
    __all__ = ["FleetObservabilityHostFeature"]
