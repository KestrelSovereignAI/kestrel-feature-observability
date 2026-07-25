"""Fleet observability ``HostFeature``.

Ships the single "Observability" console panel: Timeline for temporal overview,
Navigator for hierarchy plus persistent span inspection, and the curated
host-supervised Phoenix embed for exhaustive trace forensics (the OTel-native
pivot, epic #32). The custom event store and query routes are retired: all
three views read Phoenix, so nothing writes to or reads from a local store.

Discovered at host scope via the ``kestrel_sovereign.host_features`` entry point.
"""

from __future__ import annotations

import logging
from typing import Optional

from kestrel_sdk import HostFeature, UIContributions

logger = logging.getLogger(__name__)


class FleetObservabilityHostFeature(HostFeature):
    """Host-scoped fleet observability feature.

    Ships the single "Observability" console panel backed by host-supervised
    Phoenix. Discovered at host scope, not per-agent.
    """

    #: Stable slug used for mount path / capability gating.
    name = "observability-fleet"

    # -- UI -----------------------------------------------------------------

    def get_ui_contributions(self) -> Optional[UIContributions]:
        """Ship the Timeline, Navigator inspector, and embedded Phoenix panel."""
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
            # panel module reads Phoenix for Timeline/Navigator and embeds its UI
            # at ``/phoenix/`` for exhaustive trace forensics.
            modules=["observability.js"],
            # Host panels are always-on; keep the capability gate off (the
            # sovereign gate bug is fixed separately in
            # KestrelSovereignAI/kestrel-sovereign#2459).
            capability=None,
        )


__all__ = ["FleetObservabilityHostFeature"]
