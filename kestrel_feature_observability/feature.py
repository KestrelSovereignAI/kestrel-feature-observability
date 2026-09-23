"""
Kestrel Observability Feature — always-on lifecycle event emitter.

Auto-discovers via the feature system and registers the ``ObservabilityHook``,
which emits an OTel trace (session → tool spans) per agent lifecycle via
``KestrelTracer``. This package is producer-only: the fleet host feature owns the
embedded Phoenix UI. Prometheus operational counters stay local to this package.
"""

import logging
from typing import List, Optional

from kestrel_sdk.features.base import Feature
from kestrel_sdk.hooks.base import Hook
from kestrel_feature_observability.hook import ObservabilityHook

logger = logging.getLogger(__name__)


class ObservabilityFeature(Feature):
    """Always-on observability. Emits all lifecycle events as OTel spans."""

    def __init__(self, agent):
        super().__init__(agent)
        self._hook: Optional[ObservabilityHook] = None

    @property
    def tool_description(self) -> str:
        return "Lifecycle event observability and monitoring"

    async def initialize(self):
        """Create the ObservabilityHook and subscribe it to core turn outcomes.

        The hook is auto-registered via ``get_hooks``. Its turn-outcome listener
        is registered here through the host's duck-typed
        ``add_turn_outcome_listener`` (kestrel-sovereign#3159), so every turn
        root ends with the outcome core computed — including the cancel paths
        that skip the SDK ``Stop`` hook (#118).
        """
        self._hook = ObservabilityHook(agent=self.agent)
        self._hook.subscribe_turn_outcomes()

    def get_hooks(self) -> List[Hook]:
        """Return the observability hook for auto-registration."""
        if self._hook:
            return [self._hook]
        return []

    async def shutdown(self):
        """Unsubscribe from turn outcomes, close open spans, drop the hook."""
        if self._hook is not None:
            self._hook.unsubscribe_turn_outcomes()
            try:
                self._hook.close()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
        self._hook = None
