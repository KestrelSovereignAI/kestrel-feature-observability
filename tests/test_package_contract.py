"""Release packaging contracts for the fleet HostFeature surface."""

from __future__ import annotations

import pathlib
import tomllib


ROOT = pathlib.Path(__file__).resolve().parent.parent
FLEET_SDK_REQUIREMENT = "kestrel-sovereign-sdk>=0.32.0,<0.33"
TEST_SDK_REQUIREMENT = "kestrel-sovereign-sdk[metrics]>=0.32.0,<0.33"
BASE_SDK_REQUIREMENT = "kestrel-sovereign-sdk>=0.14.1,<1"
METRICS_SDK_REQUIREMENT = "kestrel-sovereign-sdk[metrics]>=0.14.1,<1"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _package_from_lock(name: str) -> dict:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(package for package in lock["package"] if package["name"] == name)


def test_release_version_is_0_17_7_in_project_and_lock():
    assert _pyproject()["project"]["version"] == "0.17.7"
    assert _package_from_lock("kestrel-feature-observability")["version"] == "0.17.7"


def test_only_fleet_and_test_surfaces_use_the_narrow_sdk_contract():
    metadata = _pyproject()
    project = metadata["project"]
    extras = project["optional-dependencies"]

    assert BASE_SDK_REQUIREMENT in project["dependencies"]
    assert extras["metrics"] == [METRICS_SDK_REQUIREMENT]
    assert extras["fleet"] == [FLEET_SDK_REQUIREMENT]
    assert TEST_SDK_REQUIREMENT in extras["test"]
    assert TEST_SDK_REQUIREMENT in metadata["dependency-groups"]["dev"]


def test_lock_records_the_same_fleet_and_test_sdk_contracts():
    requirements = _package_from_lock("kestrel-feature-observability")["metadata"][
        "requires-dist"
    ]

    assert {
        "name": "kestrel-sovereign-sdk",
        "marker": "extra == 'fleet'",
        "specifier": ">=0.32.0,<0.33",
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "extras": ["metrics"],
        "marker": "extra == 'test'",
        "specifier": ">=0.32.0,<0.33",
    } in requirements


def test_fleet_contract_documentation_and_guard_warning_match_the_extra():
    for relative_path in (
        "README.md",
        "AGENTS.md",
        "kestrel_feature_observability/fleet/__init__.py",
        "tests/conftest.py",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert ">=0.32.0,<0.33" in text, relative_path
