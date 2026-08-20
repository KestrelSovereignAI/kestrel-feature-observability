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

RELEASE_VERSION = "0.17.13"
SDK_FLOOR = ">=0.34"
SDK_SPECIFIER = ">=0.34,<1"

BASE_SDK_REQUIREMENT = f"kestrel-sovereign-sdk{SDK_SPECIFIER}"
METRICS_SDK_REQUIREMENT = f"kestrel-sovereign-sdk[metrics]{SDK_SPECIFIER}"
FLEET_SDK_REQUIREMENT = BASE_SDK_REQUIREMENT
TEST_SDK_REQUIREMENT = METRICS_SDK_REQUIREMENT

#: An upper bound anywhere below 1.0 re-creates the lockstep this policy exists
#: to remove. `<1` is the intended and only acceptable ceiling.
#:
#: Matched per line rather than as one expression: the specifier's own comma
#: (``>=0.34,<0.35``) terminates any character-class-based scan, which silently
#: made an earlier version of this guard match nothing at all.
#: Triggered by any mention of the SDK, not just the literal package name: the
#: prose that regenerates the wrong policy tends to say "the SDK (>=0.34,<0.35)"
#: without naming the distribution, and the docs are what a future maintainer
#: copies from.
_SDK_MENTION = re.compile(r"sdk", re.IGNORECASE)
_CEILING_BELOW_ONE = re.compile(r"<\s*=?\s*0\.\d+")


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
        lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        # Scan a two-line window: the fleet guard's warning wraps the mention
        # and its specifier onto separate lines, which a per-line scan misses.
        for index, line in enumerate(lines):
            window = " ".join(lines[index:index + 2])
            # AGENTS.md documents the retired policy on purpose, as history.
            if "previous" in window or "retired" in window:
                continue
            if _SDK_MENTION.search(window) and _CEILING_BELOW_ONE.search(window):
                offenders.append(f"{relative_path}:{index + 1}: {line.strip()}")
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
