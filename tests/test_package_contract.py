"""Release packaging contracts for the fleet HostFeature surface.

The SDK constraint is **floor-only** on every install surface (issue #99). The
host (`kestrel-sovereign`) pins the SDK to a single minor and therefore decides
which SDK the environment gets; a second ceiling declared here has to be walked
forward by hand, in a separate repo, on every host bump, and any lag makes the
dependency graph unsatisfiable. That is not hypothetical — the previous
`>=0.34,<0.35` policy did exactly that when the host moved to `>=0.35.0,<0.36`,
and every host with this package installed resolved into a broken pair.

These tests therefore assert the *policy*, not a literal version string, and
actively forbid reintroducing an upper bound.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent

RELEASE_VERSION = "0.17.12"
SDK_FLOOR = ">=0.34"
SDK_SPECIFIER = ">=0.34,<1"

BASE_SDK_REQUIREMENT = f"kestrel-sovereign-sdk{SDK_SPECIFIER}"
METRICS_SDK_REQUIREMENT = f"kestrel-sovereign-sdk[metrics]{SDK_SPECIFIER}"
FLEET_SDK_REQUIREMENT = BASE_SDK_REQUIREMENT
TEST_SDK_REQUIREMENT = METRICS_SDK_REQUIREMENT

#: An upper bound anywhere below 1.0 re-creates the lockstep this policy exists
#: to remove. `<1` is the intended and only acceptable ceiling.
_FORBIDDEN_CEILING = re.compile(r"kestrel-sovereign-sdk[^\"',\n]*<\s*0\.\d+")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _package_from_lock(name: str) -> dict:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(package for package in lock["package"] if package["name"] == name)


def test_release_version_matches_between_project_and_lock():
    assert _pyproject()["project"]["version"] == RELEASE_VERSION
    assert _package_from_lock("kestrel-feature-observability")["version"] == RELEASE_VERSION


def test_every_install_surface_declares_the_sdk_floor_only():
    metadata = _pyproject()
    project = metadata["project"]
    extras = project["optional-dependencies"]

    assert BASE_SDK_REQUIREMENT in project["dependencies"]
    assert extras["metrics"] == [METRICS_SDK_REQUIREMENT]
    assert extras["fleet"] == [FLEET_SDK_REQUIREMENT]
    assert TEST_SDK_REQUIREMENT in extras["test"]
    assert TEST_SDK_REQUIREMENT in metadata["dependency-groups"]["dev"]


def test_no_install_surface_declares_an_sdk_ceiling_below_one():
    """The regression guard for #99.

    Bumping the ceiling one minor at a time is what generated eight releases of
    lockstep churn and one unsatisfiable host. If a newer SDK genuinely breaks
    this package, raise the floor or fix the code — do not cap it.
    """
    sources = [
        "pyproject.toml",
        "uv.lock",
        "AGENTS.md",
        "README.md",
        "tests/conftest.py",
        "kestrel_feature_observability/fleet/__init__.py",
    ]
    offenders = []
    for relative_path in sources:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for line in text.splitlines():
            # AGENTS.md documents the retired policy on purpose, as history.
            if "previous" in line or "retired" in line:
                continue
            if _FORBIDDEN_CEILING.search(line):
                offenders.append(f"{relative_path}: {line.strip()}")
    assert not offenders, "SDK ceiling below 1 reintroduced:\n" + "\n".join(offenders)


def test_lock_records_the_floor_only_contract_on_every_surface():
    requirements = _package_from_lock("kestrel-feature-observability")["metadata"][
        "requires-dist"
    ]

    assert {
        "name": "kestrel-sovereign-sdk",
        "specifier": SDK_SPECIFIER,
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "marker": "extra == 'fleet'",
        "specifier": SDK_SPECIFIER,
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "extras": ["metrics"],
        "marker": "extra == 'metrics'",
        "specifier": SDK_SPECIFIER,
    } in requirements
    assert {
        "name": "kestrel-sovereign-sdk",
        "extras": ["metrics"],
        "marker": "extra == 'test'",
        "specifier": SDK_SPECIFIER,
    } in requirements


def test_documentation_and_guard_warning_state_the_floor():
    for relative_path in (
        "README.md",
        "AGENTS.md",
        "kestrel_feature_observability/fleet/__init__.py",
        "tests/conftest.py",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert SDK_FLOOR in text, relative_path
