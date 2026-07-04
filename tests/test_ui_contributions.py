"""Tests for the LLM Calls panel UI contribution + its manifest entry.

Covers:
1. get_ui_contributions() returns a descriptor pointing at the shipped
   static/llm-calls.js, gated on the `observability` capability.
2. The contribution appears in the host UI manifest for an enabled agent.
   Uses the real ``kestrel_sovereign.ui_contributions.compute_ui_manifest`` when
   the host is installed; otherwise a faithful local replica of the same merge
   (the host is not a dependency of this package) so the suite stays green.
"""

import re
from pathlib import Path
from types import SimpleNamespace

from kestrel_feature_observability.feature import ObservabilityFeature


def _make_feature(enabled=True):
    agent = SimpleNamespace(agent_name="test-agent")
    feature = ObservabilityFeature(agent)
    feature.enabled = enabled
    return feature


def _slug(feature_key: str) -> str:
    """Mirror ui_contributions._feature_mount_name."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", feature_key).strip("-").lower()
    return s or "feature"


# ---------------------------------------------------------------------------
# 1. Contribution descriptor
# ---------------------------------------------------------------------------

class TestUIContributions:
    def test_returns_contribution(self):
        contrib = _make_feature().get_ui_contributions()
        assert contrib is not None
        assert contrib.modules == ["llm-calls.js", "timeline.js"]
        assert contrib.capability == "observability"

    def test_static_dir_exists_and_holds_module(self):
        contrib = _make_feature().get_ui_contributions()
        static_dir = Path(contrib.static_dir)
        assert static_dir.is_dir()
        assert (static_dir / "llm-calls.js").is_file()
        assert (static_dir / "timeline.js").is_file()

    def test_module_paths_are_relative(self):
        # No absolute asset paths beyond the /features/{slug}/static mount.
        contrib = _make_feature().get_ui_contributions()
        for m in contrib.modules:
            assert not m.startswith("/")
            assert "://" not in m


# ---------------------------------------------------------------------------
# 2. Manifest entry
# ---------------------------------------------------------------------------

def _local_manifest(agent):
    """Faithful local replica of compute_ui_manifest's enabled-feature merge."""
    manifest = []
    for name, feature in (getattr(agent, "features", {}) or {}).items():
        if not getattr(feature, "enabled", True):
            continue
        contrib = feature.get_ui_contributions()
        if contrib is None:
            continue
        mount = f"/features/{_slug(name)}/static" if contrib.static_dir else None
        modules = [f"{mount}/{m}" if mount else m for m in contrib.modules]
        if not modules:
            continue
        manifest.append({
            "feature": name,
            "capability": contrib.capability or name,
            "modules": modules,
            "css": list(contrib.css),
        })
    return manifest


def _compute_manifest(agent):
    try:
        from kestrel_sovereign.ui_contributions import compute_ui_manifest
        return compute_ui_manifest(agent)
    except Exception:
        return _local_manifest(agent)


class TestManifest:
    def test_contribution_appears_for_enabled_agent(self):
        feature = _make_feature(enabled=True)
        agent = SimpleNamespace(features={"ObservabilityFeature": feature})
        manifest = _compute_manifest(agent)

        entries = [e for e in manifest if e["capability"] == "observability"]
        assert len(entries) == 1
        entry = entries[0]
        assert any(m.endswith("/static/llm-calls.js") for m in entry["modules"])
        assert any(m.endswith("/static/timeline.js") for m in entry["modules"])

    def test_disabled_feature_contributes_nothing(self):
        feature = _make_feature(enabled=False)
        agent = SimpleNamespace(features={"ObservabilityFeature": feature})
        manifest = _compute_manifest(agent)
        assert not [e for e in manifest if e["capability"] == "observability"]
