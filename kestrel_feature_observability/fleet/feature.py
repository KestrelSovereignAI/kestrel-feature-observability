"""Fleet observability ``HostFeature``.

Ships the single "Observability" console panel — a thin embed of the
host-supervised Phoenix UI (the OTel-native pivot, epic #32). The custom event
store, query routes, and swimlane/runs panels this feature once carried are
retired: emission is fully OTel (the per-agent hook emits spans) and the UI is
the Phoenix embed, so nothing writes to or reads from a local store anymore.

Discovered at host scope via the ``kestrel_sovereign.host_features`` entry point.
"""

from __future__ import annotations

import logging
from typing import Optional

from kestrel_sdk import HostFeature, UIContributions

logger = logging.getLogger(__name__)


class FleetObservabilityHostFeature(HostFeature):
    """Host-scoped fleet observability feature.

    Ships the single "Observability" console panel embedding the host-supervised
    Phoenix UI. Discovered at host scope, not per-agent.
    """

    #: Stable slug used for mount path / capability gating.
    name = "observability-fleet"

    # -- UI -----------------------------------------------------------------

    def get_ui_contributions(self) -> Optional[UIContributions]:
        """Ship the single "Observability" console panel (embedded Phoenix UI)."""
        import os

        static_dir = os.path.join(os.path.dirname(__file__), "static")
        if not os.path.isdir(static_dir):
            return None
        return UIContributions(
            static_dir=static_dir,
            # Mount-relative to this feature's static dir. The host serves
            # ``static_dir`` at ``/host/features/{slug}/static`` and resolves each
            # module as ``{mount}/{path}``, so the path is relative to the static
            # root (the file ships at ``static/observability.js``) — no ``slug``
            # prefix here (the host already adds it) or the URL 404s. The single
            # panel module embeds the self-hosted Phoenix UI (``/phoenix/``) via
            # an iframe (OTel-native pivot #32).
            modules=["observability.js"],
            # Host panels are always-on; keep the capability gate off (the
            # sovereign gate bug is fixed separately in
            # KestrelSovereignAI/kestrel-sovereign#2459).
            capability=None,
        )


__all__ = ["FleetObservabilityHostFeature"]
