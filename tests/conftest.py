"""Shared fixtures for the fleet observability tests."""

from __future__ import annotations

# The fleet HostFeature imports the host SDK's HostFeature/UIContributions
# contract (``kestrel-sovereign-sdk >=0.30``, the ``[fleet]`` extra). The
# verification gate runs a bare ``pytest -q`` from whatever environment is on
# PATH (see ``pythonpath = ["."]`` in pyproject.toml); that environment may be a
# base emitter install where the host SDK contract is absent. Mirror the guarded
# import in ``fleet/__init__.py``: when the contract is unavailable, skip the
# fleet-dependent test module instead of erroring out at conftest import time, so
# the emitter tests still run and ``pytest -q`` exits clean. Where the extra *is*
# installed (host/CI), the fleet test runs as before.
try:
    from kestrel_feature_observability.fleet.feature import (  # noqa: F401
        FleetObservabilityHostFeature,
    )
except ImportError:  # [fleet] extra (host SDK contract) not installed
    collect_ignore_glob = ["test_feature.py"]
