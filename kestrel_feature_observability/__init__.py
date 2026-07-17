"""
Kestrel Feature: Observability — lifecycle event logging via hook system.

Extracted from kestrel-sovereign as a standalone feature package.
Registers ``ObservabilityFeature`` via the ``kestrel_sovereign.features``
entry-point group; auto-discovered when installed alongside
kestrel-sovereign.

The feature attaches an ``ObservabilityHook`` to the agent's hook
system. Every lifecycle event is emitted as an OTel span (session run
span → tool spans) via ``KestrelTracer``; the fleet host feature owns
the embedded Phoenix UI that renders them. All mutations of Prometheus
metrics happen via ``kestrel_sdk.metrics``, so the feature shares the
framework's metric registry — a single ``/metrics`` scrape stays
coherent.

Install with the [metrics] extra to enable real Prometheus output:

    uv pip install 'kestrel-feature-observability[metrics]'

Without prometheus-client installed, metric handles are no-ops and
the hook still emits OTel spans.
"""

from importlib.metadata import PackageNotFoundError, version as _version

from .feature import ObservabilityFeature
from .hook import ObservabilityHook
from .tracing import KestrelTracer, configure as configure_tracing

try:
    __version__ = _version("kestrel-feature-observability")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = [
    "ObservabilityFeature",
    "ObservabilityHook",
    "KestrelTracer",
    "configure_tracing",
    "__version__",
]
