"""
Kestrel Feature: Observability — lifecycle event logging via hook system.

Extracted from kestrel-sovereign as a standalone feature package.
Registers ``ObservabilityFeature`` via the ``kestrel_sovereign.features``
entry-point group; auto-discovered when installed alongside
kestrel-sovereign.

The feature attaches an ``ObservabilityHook`` to the agent's hook
system. Every lifecycle event is POSTed to the fleet host's
observability store (the fleet feature owns the store, query routes,
and UI). All mutations of Prometheus metrics happen via
``kestrel_sdk.metrics``, so the feature shares the framework's metric
registry — a single ``/metrics`` scrape stays coherent.

Install with the [metrics] extra to enable real Prometheus output:

    uv pip install 'kestrel-feature-observability[metrics]'

Without prometheus-client installed, metric handles are no-ops and
the hook still emits events to the fleet store.
"""

from importlib.metadata import PackageNotFoundError, version as _version

from .feature import ObservabilityFeature
from .hook import ObservabilityHook

try:
    __version__ = _version("kestrel-feature-observability")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = ["ObservabilityFeature", "ObservabilityHook", "__version__"]
