"""Navigator read-model vs the REAL producer / Phoenix contracts (#46 review).

Source-string assertions proved insufficient for the navigator's two external
boundaries, so both are pinned here against real artifacts:

- **The Phoenix GraphQL schema.** Every document ``navigator.js`` (and the
  embed's deep-link lookup in ``observability.js``) sends is validated against
  the vendored ``arize-phoenix`` 17.7.0 schema — the version the host
  supervises and the embed curation pins. Phoenix exposes ``node(id: ID!)``;
  there is no ``GlobalID`` scalar, so declaring one fails GraphQL validation
  before any resolver runs (the first cut's P1).

- **The producer span shape.** A fixture modeled on an actual Talon run trace
  (root: ``kestrel.agent_name == "talon"`` + ``kestrel.run_id``; the worker
  split — ``kestrel.stage`` / prefixed ``talon/implement`` names — only on
  child stage spans; NO session attribute anywhere) is fed through the
  navigator's real aggregation code, executed under node, asserting the
  ``talon → talon/implement → session → turn`` drill path stays reachable.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
STATIC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "kestrel_feature_observability"
    / "fleet"
    / "static"
)
PHOENIX_SCHEMA = FIXTURES / "phoenix_schema_17.7.0.graphql"

NODE = shutil.which("node")


def _navigator_source() -> str:
    return (STATIC / "navigator.js").read_text(encoding="utf-8")


def _navigator_module(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write navigator.js + shared phoenix.js as a node-importable module.

    phoenix.js imports the console's API client from an absolute URL only a
    browser can resolve; stub it — the code under test never touches it.
    """
    pkg = tmp_path / "nav"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    phoenix = (STATIC / "phoenix.js").read_text(encoding="utf-8").replace(
        'import API from "/js/api.js";',
        "const API = { requestHost: async () => ({}) };",
    )
    assert "const API" in phoenix, "phoenix.js API import stub failed"
    (pkg / "phoenix.js").write_text(phoenix, encoding="utf-8")
    module = pkg / "navigator.js"
    module.write_text(_navigator_source(), encoding="utf-8")
    return module


def _graphql_documents() -> list[str]:
    """Every GraphQL document the fleet static bundle sends to Phoenix.

    phoenix.js holds the shared documents in backtick template literals;
    observability.js's name→ID deep-link query is a double-quoted string.
    """
    phoenix = (STATIC / "phoenix.js").read_text(encoding="utf-8")
    docs = re.findall(r"`\s*(query\s[^`]*)`", phoenix)
    obs = (STATIC / "observability.js").read_text(encoding="utf-8")
    docs += re.findall(r'"(query\s[^"]*)"', obs)
    return docs


def test_all_expected_documents_are_extracted():
    """The extraction regexes actually see every operation (guards the guard)."""
    names = {m for doc in _graphql_documents() for m in re.findall(r"query\s+(\w+)", doc)}
    assert {
        "NavigatorProjects",
        "NavigatorSpanPage",
        "NavigatorTraceSpans",
        "KestrelProjects",
    } <= names


def test_graphql_documents_validate_against_phoenix_schema():
    """Full GraphQL validation of every document against Phoenix 17.7.0.

    Catches unknown types (``GlobalID``), unknown fields/arguments, and
    variable-type mismatches — the class of defect a source-string test can
    never see. Skipped (not passed) when graphql-core is absent; the
    variable-type test below still runs dependency-free.
    """
    graphql = pytest.importorskip("graphql", reason="graphql-core not installed")

    schema = graphql.build_schema(PHOENIX_SCHEMA.read_text(encoding="utf-8"))
    for doc in _graphql_documents():
        errors = graphql.validate(schema, graphql.parse(doc))
        assert not errors, (
            f"invalid against Phoenix 17.7.0: {[e.message for e in errors]}\n{doc}"
        )


def test_query_variable_types_exist_in_phoenix_schema():
    """Dependency-free backstop: every declared variable type must be a real
    Phoenix 17.7.0 type (kills the nonexistent-scalar class, e.g. GlobalID)."""
    sdl = PHOENIX_SCHEMA.read_text(encoding="utf-8")
    declared = set(
        re.findall(r"^(?:type|interface|enum|input|scalar|union)\s+(\w+)", sdl, re.M)
    ) | {"Int", "Float", "String", "Boolean", "ID"}

    docs = _graphql_documents()
    assert docs
    for doc in docs:
        for var, vtype in re.findall(r"\$(\w+):\s*\[?(\w+)", doc):
            assert vtype in declared, (
                f"${var}: {vtype} is not a Phoenix 17.7.0 schema type"
            )
        # Belt-and-suspenders for the original P1: the nonexistent scalar must
        # not reappear in any sent document (comments in the JS may cite it).
        assert "GlobalID" not in doc


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_navigator_aggregates_a_real_talon_trace(tmp_path):
    """Run the navigator's actual aggregation over a real Talon trace shape.

    The first cut read only root spans with an exact agent filter, so talon's
    worker split (child-span-only) and sessions (no session attribute) were
    unreachable. This drives the shipped code — not a source-string proxy —
    over the fixture and asserts every level of the drill path resolves.
    """
    module = _navigator_module(tmp_path)

    harness = tmp_path / "harness.mjs"
    harness.write_text(
        """
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [modPath, fixturePath] = process.argv.slice(2);
const { createAgg, mergeSpansIntoAgg, agentFilter, workerFilter } = await import(
  pathToFileURL(modPath).href
);
const fx = JSON.parse(readFileSync(fixturePath, "utf8"));

function summarize(spans) {
  const agg = createAgg();
  mergeSpansIntoAgg(agg, spans);
  return {
    agents: [...agg.agents.keys()],
    workers: [...agg.workers.keys()],
    sessions: [...agg.sessions.entries()].map(([id, e]) => ({
      id,
      attrKey: e.attrKey,
      roots: e.roots || 0,
    })),
    stagelessCount: agg.stageless ? agg.stageless.count : 0,
  };
}

process.stdout.write(
  JSON.stringify({
    talon: summarize(fx.talon_trace),
    emitter: summarize(fx.emitter_session),
    filters: {
      talon: agentFilter("talon"),
      talonImplement: workerFilter("talon", "implement"),
    },
  }),
);
""",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [NODE, str(harness), str(module), str(FIXTURES / "talon_trace.json")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    talon = result["talon"]
    # Agent level: the prefixed worker variants (talon/implement, talon/review)
    # normalize under the base agent — never separate Agent-level entries.
    assert talon["agents"] == ["talon"]
    # Subagent level: the split comes from child stage spans, whichever way the
    # producer stamped it — stage attr only (gate), prefixed name only
    # (review), or both (implement). A root-only read finds none of these.
    assert set(talon["workers"]) == {"implement", "review", "gate"}
    # The run root has no stage and no prefixed name → the worker-less bucket.
    assert talon["stagelessCount"] == 1
    # Session level: no talon span carries a session attribute; the run id is
    # the session, and the Turn level must filter on that same attribute. One
    # root (the run root) → one turn.
    assert talon["sessions"] == [
        {"id": "UncleSaurus/widget#7", "attrKey": "kestrel.run_id", "roots": 1}
    ]

    emitter = result["emitter"]
    # Emitter sessions stay session_id-keyed with no worker split (the
    # Subagent level passes through straight to sessions).
    assert emitter["agents"] == ["Meridian"]
    assert emitter["workers"] == []
    assert emitter["sessions"] == [
        {"id": "sess-42", "attrKey": "kestrel.session_id", "roots": 1}
    ]

    # The agent drill filter must reach the prefixed child names, not just the
    # exact base name — this is what made talon's subtree unreachable.
    talon_filter = result["filters"]["talon"]
    assert "== 'talon'" in talon_filter
    assert "'talon/' in attributes[\"kestrel\"][\"agent_name\"]" in talon_filter
    # And the worker filter matches both stamping styles.
    worker_filter = result["filters"]["talonImplement"]
    assert "attributes[\"kestrel\"][\"stage\"] == 'implement'" in worker_filter
    assert "== 'talon/implement'" in worker_filter


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_filter_dsl_emits_nested_attribute_subscripts(tmp_path):
    """The emitted span-filter strings must use NESTED attribute subscripts.

    Phoenix stores dotted OTel attribute keys nested and its filter DSL only
    matches nested subscripts — verified live against 17.7.0 (#50):
    ``attributes["kestrel.agent_name"] == 'Claw'`` returns 0 spans with no
    error, while ``attributes["kestrel"]["agent_name"] == 'Claw'`` matches, so
    a flat ref silently empties every drill. The schema-validation tests above
    check document validity, not DSL semantics; this runs the shipped
    ``attrRef`` / filter builders (every level filter — agent, worker,
    session, turn — goes through ``attrRef``) and pins the emitted strings.
    """
    module = _navigator_module(tmp_path)

    harness = tmp_path / "harness.mjs"
    harness.write_text(
        """
import { pathToFileURL } from "node:url";

const [modPath] = process.argv.slice(2);
const { attrRef, exactAgentFilter, agentFilter, workerFilter } = await import(
  pathToFileURL(modPath).href
);

process.stdout.write(
  JSON.stringify({
    refs: {
      agentName: attrRef("kestrel.agent_name"),
      stage: attrRef("kestrel.stage"),
      sessionId: attrRef("kestrel.session_id"),
      oiSessionId: attrRef("session.id"),
      runId: attrRef("kestrel.run_id"),
      dotless: attrRef("dotless"),
    },
    filters: {
      exact: exactAgentFilter("Claw"),
      agent: agentFilter("talon"),
      worker: workerFilter("talon", "implement"),
    },
  }),
);
""",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [NODE, str(harness), str(module)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    # attrRef: one subscript per dot segment; dotless keys unchanged. Covers
    # every attribute the level filters reference — the turn filter is
    # attrRef(sessionAttr) == '<id>' over the three session-key attributes.
    assert result["refs"] == {
        "agentName": 'attributes["kestrel"]["agent_name"]',
        "stage": 'attributes["kestrel"]["stage"]',
        "sessionId": 'attributes["kestrel"]["session_id"]',
        "oiSessionId": 'attributes["session"]["id"]',
        "runId": 'attributes["kestrel"]["run_id"]',
        "dotless": 'attributes["dotless"]',
    }

    # The verified-live agent-drill shape, end to end.
    assert result["filters"]["exact"] == "attributes[\"kestrel\"][\"agent_name\"] == 'Claw'"

    # No emitted filter may contain a flat dotted subscript — the exact form
    # Phoenix silently matches nothing on.
    flat_subscript = re.compile(r'attributes\["[^"]*\.[^"]*"\]')
    for name, emitted in result["filters"].items():
        assert not flat_subscript.search(emitted), (
            f"flat dotted attribute subscript in {name} filter: {emitted}"
        )
