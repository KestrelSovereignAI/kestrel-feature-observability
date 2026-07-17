"""FleetObservabilityHostFeature UI contribution + discovery wiring."""

from __future__ import annotations

from kestrel_feature_observability.fleet.feature import (
    FleetObservabilityHostFeature,
)


def test_get_ui_contributions_ships_single_observability_panel():
    """A single container module is registered as the top-level panel."""
    feature = FleetObservabilityHostFeature()
    contributions = feature.get_ui_contributions()
    assert contributions is not None
    # Exactly one registered top-level panel: the "Observability" container.
    assert contributions.modules == ["observability.js"]


def test_no_retired_panels_ship_on_disk():
    """The retired Swimlane/Runs views must no longer ship as static assets."""
    import os

    feature = FleetObservabilityHostFeature()
    contributions = feature.get_ui_contributions()
    assert contributions is not None
    for view in ("swimlane.js", "swimlane.lanes.js", "runs.js"):
        assert not os.path.isfile(os.path.join(contributions.static_dir, view))


def test_ui_module_paths_are_mount_relative_and_shipped():
    """Every declared module URL must resolve to a shipped file.

    The host serves ``static_dir`` at ``/host/features/{slug}/static`` and
    resolves each module as ``{mount}/{path}`` — so a module path must be
    relative to ``static_dir`` (no leading slash, no ``{slug}`` prefix) and
    point at a real file, or the console ``import()`` 404s.
    """
    import os

    feature = FleetObservabilityHostFeature()
    contributions = feature.get_ui_contributions()
    assert contributions is not None
    assert contributions.static_dir is not None
    for module in contributions.modules:
        # Mount-relative: the host prepends the mount + slug itself.
        assert not module.startswith("/"), module
        assert not module.startswith(f"{feature.name}/"), module
        shipped = os.path.join(contributions.static_dir, module)
        assert os.path.isfile(shipped), shipped


def test_entry_point_registered():
    """``FleetObservabilityHostFeature`` stays on the ``host_features`` group.

    Resolve from the installed distribution's entry-point metadata when it is
    present; otherwise fall back to the packaging source of truth
    (``pyproject.toml``). The verification gate runs a bare ``pytest -q`` off
    PATH against the source tree (``pythonpath = ["."]`` in pyproject.toml),
    where the package is importable but not pip-installed, so no ``*.dist-info``
    entry-point metadata exists to enumerate. Either way the entry point must
    remain declared and point at the fleet subpackage.
    """
    from importlib.metadata import entry_points

    group = "kestrel_sovereign.host_features"
    registered = {ep.name: ep.value for ep in entry_points(group=group)}
    if "FleetObservabilityHostFeature" not in registered:
        import pathlib
        import tomllib

        pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        registered = data["project"]["entry-points"][group]

    assert "FleetObservabilityHostFeature" in registered
    assert registered["FleetObservabilityHostFeature"] == (
        "kestrel_feature_observability.fleet:FleetObservabilityHostFeature"
    )
