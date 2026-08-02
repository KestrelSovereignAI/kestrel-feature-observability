"""Release packaging contracts for the fleet HostFeature surface."""

from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLEET_SDK_REQUIREMENT = "kestrel-sovereign-sdk>=0.34,<0.35"
TEST_SDK_REQUIREMENT = "kestrel-sovereign-sdk[metrics]>=0.34,<0.35"
BASE_SDK_REQUIREMENT = "kestrel-sovereign-sdk>=0.34,<0.35"
METRICS_SDK_REQUIREMENT = "kestrel-sovereign-sdk[metrics]>=0.34,<0.35"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _package_from_lock(name: str) -> dict:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(package for package in lock["package"] if package["name"] == name)


def test_release_version_is_0_17_11_in_project_and_lock():
    assert _pyproject()["project"]["version"] == "0.17.11"
    assert _package_from_lock("kestrel-feature-observability")["version"] == "0.17.11"


def test_every_install_surface_uses_the_sdk_0_34_contract():
    metadata = _pyproject()
    project = metadata["project"]
    extras = project["optional-dependencies"]

    assert BASE_SDK_REQUIREMENT in project["dependencies"]
    assert extras["metrics"] == [METRICS_SDK_REQUIREMENT]
    assert extras["fleet"] == [FLEET_SDK_REQUIREMENT]
    assert TEST_SDK_REQUIREMENT in extras["test"]
    assert TEST_SDK_REQUIREMENT in metadata["dependency-groups"]["dev"]


def test_lock_records_every_public_sdk_contract():
    requirements = _package_from_lock("kestrel-feature-observability")["metadata"][
        "requires-dist"
    ]

    assert {
        "name": "kestrel-sovereign-sdk",
        "specifier": ">=0.34,<0.35",
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "marker": "extra == 'fleet'",
        "specifier": ">=0.34,<0.35",
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "extras": ["metrics"],
        "marker": "extra == 'metrics'",
        "specifier": ">=0.34,<0.35",
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "extras": ["metrics"],
        "marker": "extra == 'test'",
        "specifier": ">=0.34,<0.35",
    } in requirements


def test_documentation_and_guard_warning_match_the_sdk_contract():
    for relative_path in (
        "README.md",
        "AGENTS.md",
        "kestrel_feature_observability/fleet/__init__.py",
        "tests/conftest.py",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert ">=0.34,<0.35" in text, relative_path
