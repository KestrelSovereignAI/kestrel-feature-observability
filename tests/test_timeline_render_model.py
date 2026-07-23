"""Timeline render-model resolution vs the REAL producer span shapes (#62).

The Timeline's raw geometry can't paint three producer span shapes directly:

- ``"<x> (started)"`` markers — instant points whose real bar is a SIBLING
  (the emitter / Claude-hook tool-start marker, paired with its ``PostToolUse``
  span) OR a PARENT (talon parents the marker UNDER the span it marks). A marker
  must never draw its own open-ended bar when its twin exists.
- turn roots (``"<agent> turn <n>"``, ``kestrel.marker=start``) — instant points
  that ARE the turn's start; the close signal is the ``"turn <n> summary"`` CHILD,
  then the next turn's start, then session end, then the live right edge.
- ``"turn <n> summary"`` / ``"session summary"`` spans — folded into their owning
  band, never their own bar.

``timeline.js`` exports the pure ``annotateRenderModel`` for exactly this — it is
run under node here over span records shaped like ``normalize()``'s output (the
real producer contract in ``hook.py`` / ``kestrel_obs_claude_hook.py`` / talon via
``tracing.py``), asserting the shipped resolution — not a source-string proxy.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

STATIC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "kestrel_feature_observability"
    / "fleet"
    / "static"
)

NODE = shutil.which("node")


def _module_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Lay out timeline.js + a node-loadable phoenix.js in an ESM package dir.

    ``timeline.js`` imports ``./phoenix.js``, which imports the console API client
    from a browser-absolute URL; stub that one import (the render-model code never
    touches it). A ``package.json`` marks the dir ESM so ``./phoenix.js`` loads.
    """
    pkg = tmp_path / "tl"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    phoenix = (STATIC / "phoenix.js").read_text(encoding="utf-8")
    stubbed = phoenix.replace(
        'import API from "/js/api.js";',
        "const API = { requestHost: async () => ({}) };",
    )
    assert "const API" in stubbed, "phoenix.js API import stub failed — import changed?"
    (pkg / "phoenix.js").write_text(stubbed, encoding="utf-8")
    (pkg / "timeline.js").write_text(
        (STATIC / "timeline.js").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return pkg


_HARNESS = r"""
import { annotateRenderModel, openStartFloors } from "./timeline.js";

// A normalized span record shaped like timeline.js's normalize() output.
let idc = 0;
function span(o) {
  idc += 1;
  return {
    id: o.id || `n${idc}`,
    name: o.name,
    start: o.start,
    end: o.end != null ? o.end : o.start,
    instant: o.end != null && o.end <= o.start,
    openEnded: o.openEnded === true,
    marker: o.marker || null,
    kind: o.kind || "TOOL",
    status: "ok",
    spanId: o.spanId,
    parentId: o.parentId || null,
    traceId: o.traceId || null,
    sessionId: o.sessionId != null ? o.sessionId : null,
    projectId: o.projectId != null ? o.projectId : null,
    attrs: o.attrs || {},
  };
}
const NOW = 10000;
const pick = (s) => ({ rHide: s.rHide, rOpen: s.rOpen, rEnd: s.rEnd, rLabel: s.rLabel, rSummary: s.rSummary });
const out = {};

// talon: marker parented UNDER a CLOSED real span → marker dropped, real is the bar.
{
  const real = span({ name: "implement", start: 100, end: 500, spanId: "impl", sessionId: "run#1" });
  const marker = span({ name: "implement (started)", start: 100, marker: "start", spanId: "im2", parentId: "impl", sessionId: "run#1" });
  annotateRenderModel([real, marker], NOW);
  out.talonClosed = { marker: pick(marker), real: pick(real) };
}
// talon: marker under an OPEN (in-flight) real span → marker dropped, parent is the open band.
{
  const real = span({ name: "review", start: 100, openEnded: true, spanId: "rv", sessionId: "run#1" });
  const marker = span({ name: "review (started)", start: 100, marker: "start", spanId: "rv2", parentId: "rv", sessionId: "run#1" });
  annotateRenderModel([real, marker], NOW);
  out.talonOpen = { marker: pick(marker), real: pick(real) };
}
// ORPHAN marker (twin not fetched yet) → survives as the single provisional open band.
{
  const orphan = span({ name: "coordinate (started)", start: 200, marker: "start", spanId: "orph", parentId: "MISSING", sessionId: "run#1" });
  annotateRenderModel([orphan], NOW);
  out.orphan = pick(orphan);
}
// emitter / Claude: tool-start marker is a SIBLING of the real tool span; turn summary folds.
{
  const turn = span({ name: "claude-code turn 1", start: 50, marker: "start", kind: "AGENT", spanId: "t1", sessionId: "S1", attrs: { kestrel: { turn_index: 1 } } });
  const toolStart = span({ name: "Bash (started)", start: 60, marker: "start", spanId: "bs1", parentId: "t1", sessionId: "S1" });
  const toolReal = span({ name: "Bash", start: 60, end: 90, spanId: "bs2", parentId: "t1", sessionId: "S1" });
  const summary = span({ name: "turn 1 summary", start: 50, end: 95, kind: "CHAIN", spanId: "ts1", parentId: "t1", sessionId: "S1", attrs: { kestrel: { tool_count: 1, success_ratio: 1, turn_duration_ms: 45 } } });
  annotateRenderModel([turn, toolStart, toolReal, summary], NOW);
  out.sibling = { toolStart: pick(toolStart), toolReal: pick(toolReal), summary: pick(summary), turn: pick(turn) };
}
// Two turns: the first closes at the NEXT turn's start; the last is the live tail.
{
  const t1 = span({ name: "claude-code turn 1", start: 100, marker: "start", kind: "AGENT", spanId: "c1", sessionId: "S2", attrs: { kestrel: { turn_index: 1 } } });
  const t2 = span({ name: "claude-code turn 2", start: 400, marker: "start", kind: "AGENT", spanId: "c2", sessionId: "S2", attrs: { kestrel: { turn_index: 2 } } });
  annotateRenderModel([t1, t2], NOW);
  out.twoTurns = { t1: pick(t1), t2: pick(t2) };
}
// Session ended (session summary) closes a summary-less last turn; session root keeps its tick.
{
  const root = span({ name: "claude-code", start: 10, kind: "AGENT", spanId: "sr", sessionId: "S3" });
  const turn = span({ name: "claude-code turn 1", start: 100, marker: "start", kind: "AGENT", spanId: "d1", sessionId: "S3", attrs: { kestrel: { turn_index: 1 } } });
  const summary = span({ name: "session summary", start: 10, end: 900, kind: "CHAIN", spanId: "ss", parentId: "sr", sessionId: "S3", attrs: { kestrel: { turn_count: 1, tool_count: 3, success_ratio: 0.5, session_duration_ms: 890 } } });
  annotateRenderModel([root, turn, summary], NOW);
  out.sessionEnd = { root: pick(root), turn: pick(turn), summary: pick(summary), rootStart: root.start };
}
// Invariant: an open child of a CLOSED turn is pinned to the turn end (never viewEnd).
{
  const turn = span({ name: "claude-code turn 1", start: 100, marker: "start", kind: "AGENT", spanId: "e1", sessionId: "S4", attrs: { kestrel: { turn_index: 1 } } });
  const child = span({ name: "LongTool", start: 120, openEnded: true, spanId: "ec", parentId: "e1", sessionId: "S4" });
  const summary = span({ name: "turn 1 summary", start: 100, end: 300, kind: "CHAIN", spanId: "es", parentId: "e1", sessionId: "S4", attrs: { kestrel: { tool_count: 1, success_ratio: 1, turn_duration_ms: 200 } } });
  annotateRenderModel([turn, child, summary], NOW);
  out.invariant = { child: pick(child), turnEnd: turn.rEnd };
}
// P2: two concurrent same-name markers with ONE completed twin (no correlation
// ids) → exactly one marker drops (paired), the other survives as an open band.
// The old `some(...)` sibling test hid BOTH once any `Bash` closed.
{
  const turn = span({ name: "claude-code turn 1", start: 50, marker: "start", kind: "AGENT", spanId: "ct", sessionId: "P2a", attrs: { kestrel: { turn_index: 1 } } });
  const m1 = span({ name: "Bash (started)", start: 60, marker: "start", spanId: "pm1", parentId: "ct", sessionId: "P2a" });
  const m2 = span({ name: "Bash (started)", start: 65, marker: "start", spanId: "pm2", parentId: "ct", sessionId: "P2a" });
  const r1 = span({ name: "Bash", start: 60, end: 80, spanId: "pr1", parentId: "ct", sessionId: "P2a" });
  annotateRenderModel([turn, m1, m2, r1], NOW);
  out.concurrentName = { m1: pick(m1), m2: pick(m2), r1: pick(r1) };
}
// P2: correlation-id pairing — marker id=1 pairs its OWN twin; marker id=2's twin
// hasn't arrived, so it stays open even though a same-name `Bash` exists.
{
  const turn = span({ name: "claude-code turn 2", start: 50, marker: "start", kind: "AGENT", spanId: "ct2", sessionId: "P2b", attrs: { kestrel: { turn_index: 2 } } });
  const m1 = span({ name: "Bash (started)", start: 60, marker: "start", spanId: "im1", parentId: "ct2", sessionId: "P2b", attrs: { tool: { call_id: "toolu_1" } } });
  const m2 = span({ name: "Bash (started)", start: 61, marker: "start", spanId: "im2b", parentId: "ct2", sessionId: "P2b", attrs: { tool: { call_id: "toolu_2" } } });
  const r1 = span({ name: "Bash", start: 60, end: 90, spanId: "ir1", parentId: "ct2", sessionId: "P2b", attrs: { tool: { call_id: "toolu_1" } } });
  annotateRenderModel([turn, m1, m2, r1], NOW);
  out.correlId = { m1: pick(m1), m2: pick(m2) };
}
// P1: live-poll floor — an unpaired marker whose (backdated) twin hasn't been
// persisted yet is the re-fetch floor; the floor reaches <= the twin's start, so
// the next poll pulls it and the marker pairs. (The open turn root also keeps the
// floor down, which is fine — it still covers the twin.)
{
  const turn = span({ name: "claude-code turn 1", start: 100, marker: "start", kind: "AGENT", spanId: "ft", sessionId: "F1", projectId: "P", attrs: { kestrel: { turn_index: 1 } } });
  const marker = span({ name: "Bash (started)", start: 120, marker: "start", spanId: "fm", parentId: "ft", sessionId: "F1", projectId: "P" });
  annotateRenderModel([turn, marker], NOW);
  const before = openStartFloors([turn, marker]);
  const markerOpenBefore = marker.rOpen;
  const real = span({ name: "Bash", start: 120, end: 150, spanId: "fr", parentId: "ft", sessionId: "F1", projectId: "P" });
  annotateRenderModel([turn, marker, real], NOW);
  out.markerFloor = {
    floor: before.get("P"),
    coversTwin: before.get("P") != null && before.get("P") <= 120,
    markerOpenBefore,
    markerHiddenAfter: marker.rHide === true,
  };
}
// P1: turn poll → later summary poll. An open (live-tail) turn is the floor; its
// backdated summary (start == turn start) closes it and clears the floor, so the
// poll stops re-fetching once the turn resolves.
{
  const t1 = span({ name: "claude-code turn 1", start: 200, marker: "start", kind: "AGENT", spanId: "gt", sessionId: "F2", projectId: "P", attrs: { kestrel: { turn_index: 1 } } });
  annotateRenderModel([t1], NOW);
  const before = openStartFloors([t1]);
  const openBefore = t1.rOpen;
  const summary = span({ name: "turn 1 summary", start: 200, end: 260, kind: "CHAIN", spanId: "gs", parentId: "gt", sessionId: "F2", projectId: "P", attrs: { kestrel: { tool_count: 0, success_ratio: 1, turn_duration_ms: 60 } } });
  annotateRenderModel([t1, summary], NOW);
  const after = openStartFloors([t1, summary]);
  out.turnFloor = { before: before.get("P"), openBefore, closedAfter: t1.rOpen === false, afterEmpty: after.get("P") == null };
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_annotate_render_model_resolves_producer_shapes(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / "harness.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(pkg),
    )
    r = json.loads(proc.stdout)

    # talon marker parented UNDER its span: dropped both when the parent is closed
    # and while it is open — never a second (open-ended) bar. The real span is the
    # bar (open while the stage runs; closed once it ends).
    assert r["talonClosed"]["marker"]["rHide"] is True
    assert r["talonClosed"]["real"]["rHide"] is False
    assert r["talonClosed"]["real"]["rOpen"] is False
    assert r["talonOpen"]["marker"]["rHide"] is True
    assert r["talonOpen"]["real"]["rOpen"] is True

    # An orphan marker (twin not yet fetched) survives as the SINGLE provisional
    # open band — the only case a "(started)" marker paints at all (#54.5).
    assert r["orphan"]["rHide"] is False
    assert r["orphan"]["rOpen"] is True

    # Sibling pairing (emitter / Claude): the tool-start marker is dropped, its
    # real ``PostToolUse`` sibling is the bar; the turn summary folds (never a
    # bar); the turn root closes at the summary end and gets an informative label.
    sib = r["sibling"]
    assert sib["toolStart"]["rHide"] is True
    assert sib["toolReal"]["rHide"] is False
    assert sib["summary"]["rHide"] is True
    assert sib["turn"]["rOpen"] is False
    assert sib["turn"]["rEnd"] == 95
    assert sib["turn"]["rLabel"] == "turn 1 · 1 tool · 45ms"
    assert sib["turn"]["rSummary"]["toolCount"] == 1

    # Turn band extent fallbacks: a summary-less turn closes at the NEXT turn's
    # start; the genuinely-last turn is the live tail (open).
    assert r["twoTurns"]["t1"]["rOpen"] is False
    assert r["twoTurns"]["t1"]["rEnd"] == 400
    assert r["twoTurns"]["t2"]["rOpen"] is True

    # Session end closes a summary-less last turn (never open-ended); the session
    # summary folds and its stats ride on the session root, which keeps its
    # instant marker tick (rEnd == start) rather than becoming a duplicate bar.
    se = r["sessionEnd"]
    assert se["summary"]["rHide"] is True
    assert se["turn"]["rOpen"] is False
    assert se["turn"]["rEnd"] == 900
    assert se["root"]["rSummary"]["kind"] == "session"
    assert se["root"]["rSummary"]["toolCount"] == 3
    assert se["root"]["rEnd"] == se["rootStart"]

    # The invariant: an open child of a closed turn is clamped to the turn's end,
    # not drawn out to the live right edge.
    assert r["invariant"]["turnEnd"] == 300
    assert r["invariant"]["child"]["rOpen"] is False
    assert r["invariant"]["child"]["rEnd"] == 300

    # P2: concurrent same-name markers with ONE completed twin (no correlation
    # ids) — exactly one marker is consumed one-to-one and the still-running one
    # survives as an open band. The pre-fix `some(...)` sibling test hid BOTH.
    cn = r["concurrentName"]
    assert cn["r1"]["rHide"] is False
    assert [cn["m1"]["rHide"], cn["m2"]["rHide"]].count(True) == 1
    assert [cn["m1"]["rOpen"], cn["m2"]["rOpen"]].count(True) == 1
    # Deterministic: the earlier-started marker pairs, the later one stays open.
    assert cn["m1"]["rHide"] is True
    assert cn["m2"]["rOpen"] is True

    # P2: correlation-id (tool.call_id) pairing — the id=1 marker pairs its OWN
    # twin; the id=2 marker's twin hasn't arrived, so it stays open even though a
    # same-name completed `Bash` exists.
    ci = r["correlId"]
    assert ci["m1"]["rHide"] is True
    assert ci["m2"]["rHide"] is False
    assert ci["m2"]["rOpen"] is True

    # P1: an unpaired marker's twin is BACKDATED to the marker's start, so the
    # live-poll floor must reach <= it; once the twin arrives the marker pairs.
    mf = r["markerFloor"]
    assert mf["markerOpenBefore"] is True
    assert mf["floor"] is not None
    assert mf["coversTwin"] is True
    assert mf["markerHiddenAfter"] is True

    # P1: an open turn is the floor; its backdated `turn N summary` closes it and
    # clears the floor, so the poll stops re-fetching once the turn resolves.
    tf = r["turnFloor"]
    assert tf["openBefore"] is True
    assert tf["before"] == 200
    assert tf["closedAfter"] is True
    assert tf["afterEmpty"] is True
