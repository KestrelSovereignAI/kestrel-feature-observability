"""Standalone fallback for the host's ``UIContributions`` descriptor.

The Console UI-extension contract (``get_ui_contributions()`` →
``UIContributions``) is owned by the host package ``kestrel_sovereign``. This
feature has no runtime dependency on the host, so when it runs outside one (unit
tests, ``import``-time inspection) the host class is unavailable. This local,
field-for-field copy lets ``get_ui_contributions()`` still return an inspectable
descriptor. Inside a real host the host's own class is imported and used instead
— it is the one the manifest builder ``isinstance``-checks against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class UIContributions:
    """Static frontend assets + entry modules a feature contributes to the UI.

    Mirrors ``kestrel_sovereign.features.base.UIContributions``. See that class
    for the authoritative field semantics.
    """

    modules: List[str] = field(default_factory=list)
    css: List[str] = field(default_factory=list)
    static_dir: Optional[str] = None
    capability: Optional[str] = None
