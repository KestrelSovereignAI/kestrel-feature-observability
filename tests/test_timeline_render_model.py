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
import { annotateRenderModel, heartbeatRuns, openStartFloors, schedulerBandModel } from "./timeline.js";

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
// Far-future clock so a span starting near t=0 is > STALE_MARKER_MS (15 min) old —
// the #67 abandoned-cap scenarios exercise stale open spans, whereas the NOW-based
// scenarios above stay recent (never abandoned).
const LATE = 10_000_000;
// Mirror timeline.js's thresholds (raised to 15 min in #69) so the harness can
// place spans just inside / just outside the abandoned + reconcile windows.
const STALE = 15 * 60 * 1000; // STALE_MARKER_MS
const RECONCILE = 15 * 60 * 1000; // STALE_RECONCILE_MS (grace past staleness)
const pick = (s) => ({ rHide: s.rHide, rOpen: s.rOpen, rEnd: s.rEnd, rLabel: s.rLabel, rSummary: s.rSummary, rAbandoned: s.rAbandoned });
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
// #78: the turn root can age out of the Timeline query while its recent tool
// children remain. The raw missing parentId must still form a sibling cohort:
// the completed Bash consumes only its exact call-id marker; the other same-name
// Bash remains open.
{
  const m1 = span({ name: "Bash (started)", start: 60, marker: "start", spanId: "om1", parentId: "old-turn-not-loaded", sessionId: "P2c", attrs: { tool: { call_id: "toolu_done" } } });
  const m2 = span({ name: "Bash (started)", start: 61, marker: "start", spanId: "om2", parentId: "old-turn-not-loaded", sessionId: "P2c", attrs: { tool: { call_id: "toolu_running" } } });
  const r1 = span({ name: "Bash", start: 60, end: 90, spanId: "or1", parentId: "old-turn-not-loaded", sessionId: "P2c", attrs: { tool: { call_id: "toolu_done" } } });
  annotateRenderModel([m1, m2, r1], NOW);
  out.missingParentCorrelId = { m1: pick(m1), m2: pick(m2), r1: pick(r1) };
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

// #67 — SIGKILL / power-loss cap. A hard kill can't be caught, so the held-open
// span never gets its close. Any still-open span older than STALE_MARKER_MS whose
// whole run COHORT has been silent that long is ABANDONED (capped), not painted
// running-to-now. All three open shapes are capped by the one unified pass.

// (a) held-open real span (frinz#657 shape) — a talon run root exported in-flight
//     but never closed → abandoned, capped to its own start (childless).
{
  const run = span({ name: "talon run", start: 1000, openEnded: true, spanId: "ka1", sessionId: "K1" });
  annotateRenderModel([run], LATE);
  out.abandonedHeldOpen = pick(run);
}
// (b) unpaired "(started)" marker whose twin never arrived → abandoned.
{
  const marker = span({ name: "coordinate (started)", start: 1000, marker: "start", spanId: "kb1", parentId: "MISSING", sessionId: "K2" });
  annotateRenderModel([marker], LATE);
  out.abandonedMarker = pick(marker);
}
// (c) summary-less live-tail turn root (Claude/emitter SIGKILL'd mid-turn) →
//     abandoned rather than open-ended to the live edge forever.
{
  const turn = span({ name: "claude-code turn 1", start: 1000, marker: "start", kind: "AGENT", spanId: "kc1", sessionId: "K3", attrs: { kestrel: { turn_index: 1 } } });
  annotateRenderModel([turn], LATE);
  out.abandonedTurn = pick(turn);
}
// (d) prefer observed evidence: an abandoned run WITH exported children ends at
//     the latest child end (not a fixed stub, not the live edge).
{
  const run = span({ name: "talon run", start: 1000, openEnded: true, spanId: "kd1", sessionId: "K4" });
  const tool = span({ name: "Bash", start: 1200, end: 5000, spanId: "kd2", parentId: "kd1", sessionId: "K4" });
  annotateRenderModel([run, tool], LATE);
  out.abandonedWithChild = { run: pick(run), tool: pick(tool) };
}
// (e) descendant-liveness exemption: a recent child keeps an OLD open root LIVE
//     (genuinely in-flight) — never marked abandoned. Self-correcting each poll.
{
  const run = span({ name: "talon run", start: 1000, openEnded: true, spanId: "ke1", sessionId: "K5" });
  const tool = span({ name: "Bash", start: LATE - 1000, openEnded: true, spanId: "ke2", parentId: "ke1", sessionId: "K5" });
  annotateRenderModel([run, tool], LATE);
  out.liveChildKeepsOpen = { run: pick(run), tool: pick(tool) };
}
// (f) nested held-open subtree (run root ⊃ stage), both silent past the window →
//     BOTH capped (the raw-open child must not exempt its parent forever).
{
  const run = span({ name: "talon run", start: 1000, openEnded: true, spanId: "kf1", sessionId: "K6" });
  const stage = span({ name: "implement", start: 1100, openEnded: true, spanId: "kf2", parentId: "kf1", sessionId: "K6" });
  annotateRenderModel([run, stage], LATE);
  out.nestedAbandoned = { run: pick(run), stage: pick(stage) };
}

// #69 — LIVE talon flicker. The held-open stage span (spanId 0fe0ee7c0d) is NOT
// exported while in-flight, so forward-poll loads only its "<stage> (started)"
// MARKER and its `command_execution` tool spans — BOTH parented under the missing
// stage, so the tools are SIBLINGS of the marker and the marker's OWN subtree is
// empty. An OLD marker whose cohort saw a RECENT sibling tool must stay open, not
// be abandoned by the empty-subtree signal (the #67/#68 regression this fixes).
{
  const marker = span({ name: "implement (started)", start: 1000, marker: "start", spanId: "lm1", parentId: "0fe0ee7c0d", sessionId: "LIVE1" });
  const tool = span({ name: "command_execution", start: LATE - 1000, end: LATE - 500, spanId: "lt1", parentId: "0fe0ee7c0d", sessionId: "LIVE1" });
  annotateRenderModel([marker, tool], LATE);
  out.liveTalonCohort = { marker: pick(marker), tool: pick(tool) };
}
// #69 contrast — a truly-DEAD talon run: the SAME sibling shape but the whole
// cohort has been silent past the window (no recent sibling) → the held-open
// marker still caps (the genuine SIGKILL case the guard exists for).
{
  const marker = span({ name: "implement (started)", start: 1000, marker: "start", spanId: "dm1", parentId: "deadstage", sessionId: "DEAD1" });
  const tool = span({ name: "command_execution", start: 1200, end: 1500, spanId: "dt1", parentId: "deadstage", sessionId: "DEAD1" });
  annotateRenderModel([marker, tool], LATE);
  out.deadTalonCohort = { marker: pick(marker), tool: pick(tool) };
}

// #67 P1 (live re-annotation) — a span loaded while RECENT is open; with NO new
// span IDs, only the clock advancing past STALE_MARKER_MS must flip it to
// abandoned. This is the pure core of the live fix: buildLayout re-runs
// annotateRenderModel every poll tick (not only when new IDs arrive), so a poll
// that adds nothing still catches staleness.
{
  const run = span({ name: "talon run", start: NOW - 1000, openEnded: true, spanId: "kt1", sessionId: "K9" });
  annotateRenderModel([run], NOW); // recent → genuinely live
  const whileRecent = { rOpen: run.rOpen, rAbandoned: run.rAbandoned };
  annotateRenderModel([run], NOW + STALE + 1); // same span, clock advanced → abandoned
  out.reAnnotateStale = { whileRecent, afterAdvance: { rOpen: run.rOpen, rAbandoned: run.rAbandoned } };
}

// #67 P1 (reconcile floor) — visual abandonment must NOT sever the backdated-twin
// re-fetch floor. An abandoned turn root still within the reconcile grace keeps
// anchoring the poll floor (floor <= its backdated summary's start); when that
// late summary lands it folds, un-abandons and closes the turn, and the floor
// clears so the poll stops re-fetching.
{
  const START = LATE - 1_200_000; // > STALE (abandoned) but within STALE+RECONCILE (reconciling)
  const t1 = span({ name: "claude-code turn 1", start: START, marker: "start", kind: "AGENT", spanId: "kr1", sessionId: "K7", projectId: "R", attrs: { kestrel: { turn_index: 1 } } });
  annotateRenderModel([t1], LATE);
  const before = openStartFloors([t1]);
  const abandonedBefore = t1.rAbandoned;
  const floorWhileAbandoned = before.get("R");
  const summary = span({ name: "turn 1 summary", start: START, end: START + 20_000, kind: "CHAIN", spanId: "kr2", parentId: "kr1", sessionId: "K7", projectId: "R", attrs: { kestrel: { tool_count: 0, success_ratio: 1, turn_duration_ms: 20000 } } });
  annotateRenderModel([t1, summary], LATE);
  const after = openStartFloors([t1, summary]);
  out.abandonedReconcile = {
    abandonedBefore,
    floorWhileAbandoned,
    coversTwin: floorWhileAbandoned != null && floorWhileAbandoned <= START,
    reAbandonedAfter: t1.rAbandoned,
    closedAfter: t1.rOpen === false,
    afterEmpty: after.get("R") == null,
  };
}

// #67 P1 (bounded floor) — an ANCIENT abandoned run (its twin will never arrive)
// is BEYOND the reconcile grace, so it drops out of the floor: the poll must not
// peg its cursor to days-ago and re-scan the whole span every tick forever.
{
  const run = span({ name: "talon run", start: 1000, openEnded: true, spanId: "kh1", sessionId: "K8", projectId: "R2" });
  annotateRenderModel([run], LATE);
  const floors = openStartFloors([run]);
  out.abandonedBeyondReconcile = { abandoned: run.rAbandoned, floorEmpty: floors.get("R2") == null };
}

// #87 — the VIRTUAL `session=scheduler` band: K idle heartbeats + M work ticks.
// Two failures this must pin down at once:
//  (a) the emitter used to DROP idle ticks (#42), which made an idle-but-alive
//      scheduler indistinguishable from a dead one. They now emit, so the
//      renderer owns their legibility — every tick stays represented (nothing is
//      silently collapsed) and idle is visually its OWN category, not work.
//  (b) every tick parents into ONE immortal per-process pseudo-root, so the band
//      must not read as a solid running task — but its extent is REAL and must be
//      drawn (#92): the model reports min→max plus `envelope:true`, and the paint
//      layer styles it as virtual (translucent/dashed) rather than suppressing it.
// Tick spans are INSTANT (the scheduler never stamps execution_time_ms, so the
// emitter makes them zero-duration point spans) and each pairs with its own
// "(started)" marker — the real producer shape from hook.py.
{
  const K = 3, M = 2;
  const root = span({ name: "kestrel-agent", start: 1000, kind: "AGENT", spanId: "scr", sessionId: "scheduler" });
  const members = [root];
  const idles = [], idleMarkers = [], works = [];
  for (let i = 0; i < K; i++) {
    const t = 2000 + i * 500;
    const m = span({ name: "restart_coordinator (started)", start: t, marker: "start", spanId: `sim${i}`, parentId: "scr", sessionId: "scheduler" });
    const r = span({ name: "restart_coordinator", start: t, end: t, spanId: `sir${i}`, parentId: "scr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "idle" } } });
    idleMarkers.push(m); idles.push(r); members.push(m, r);
  }
  for (let i = 0; i < M; i++) {
    const t = 6000 + i * 500;
    const m = span({ name: "training_cycle (started)", start: t, marker: "start", spanId: `swm${i}`, parentId: "scr", sessionId: "scheduler" });
    const r = span({ name: "training_cycle", start: t, end: t, spanId: `swr${i}`, parentId: "scr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "completed" } } });
    works.push(r); members.push(m, r);
  }
  annotateRenderModel(members, NOW);
  out.schedulerBand = {
    model: schedulerBandModel(members),
    // Idle ticks are their own render category; work ticks are NOT.
    idleFlags: idles.map((s) => s.rIdle),
    idleLabels: idles.map((s) => s.rLabel),
    workIdleFlags: works.map((s) => s.rIdle),
    // An idle tick is NOT a refusal — it ran, it just did nothing — so it must
    // never take the wide `denied`/`incomplete` stub treatment.
    idleOutcomes: idles.map((s) => s.rOutcome),
    // Every member is flagged as scheduler-session (band self-identification).
    allScheduler: members.every((s) => s.rScheduler === true),
    // Nothing is dropped: the only hidden spans are the paired "(started)"
    // markers, exactly as for any other twin — no tick is hidden.
    markersPaired: idleMarkers.every((s) => s.rHide === true),
    hiddenTicks: [...idles, ...works].filter((s) => s.rHide).length,
  };
}
// #87 contrast — a normal agent band is NOT a scheduler band: no aggregate model,
// so it keeps its ordinary continuous session envelope.
{
  const turn = span({ name: "claude-code turn 1", start: 100, marker: "start", kind: "AGENT", spanId: "nsb", sessionId: "S9", attrs: { kestrel: { turn_index: 1 } } });
  const tool = span({ name: "Bash", start: 120, end: 150, spanId: "nsb2", parentId: "nsb", sessionId: "S9" });
  annotateRenderModel([turn, tool], NOW);
  out.nonSchedulerBand = {
    model: schedulerBandModel([turn, tool]),
    scheduler: turn.rScheduler,
    idle: tool.rIdle,
  };
}
// #87 — a scheduler band with ONLY heartbeats (the common idle case) still
// reports its count, and the label singularizes correctly at K=1.
{
  const root = span({ name: "kestrel-agent", start: 1000, kind: "AGENT", spanId: "onr", sessionId: "scheduler" });
  const m = span({ name: "restart_coordinator (started)", start: 2000, marker: "start", spanId: "onm", parentId: "onr", sessionId: "scheduler" });
  const r = span({ name: "restart_coordinator", start: 2000, end: 2000, spanId: "ont", parentId: "onr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "idle" } } });
  annotateRenderModel([root, m, r], NOW);
  out.singleHeartbeat = { model: schedulerBandModel([root, m, r]), idle: r.rIdle };
}

// #92 — the live shape: a virtual scheduler band of K heartbeats ~1/min spanning
// ~29 minutes, which painted as a short 0ms-looking stub. Two things must hold:
// the band reports its REAL extent (so the envelope is drawn across 14:07→14:41,
// not collapsed to a point), and the beats coalesce ONLY at a scale where they'd
// actually overdraw — a paint-time decision against the current px/ms, so zooming
// in separates them again.
{
  const HB_BASE = 1_000_000;
  const MIN = 60_000;
  const K = 30; // ticks 1/min → a 29-minute extent, the observed live band
  const root = span({ name: "kestrel-agent", start: HB_BASE, kind: "AGENT", spanId: "hbr", sessionId: "scheduler" });
  const members = [root];
  const idles = [];
  for (let i = 0; i < K; i++) {
    const t = HB_BASE + i * MIN;
    members.push(span({ name: "restart_coordinator (started)", start: t, marker: "start", spanId: `hbm${i}`, parentId: "hbr", sessionId: "scheduler" }));
    const r = span({ name: "restart_coordinator", start: t, end: t, spanId: `hbt${i}`, parentId: "hbr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "idle" } } });
    idles.push(r);
    members.push(r);
  }
  const CLOCK = HB_BASE + 40 * MIN;
  annotateRenderModel(members, CLOCK);
  // The two real zoom levels of the Timeline window over the same plot width.
  const PLOT_PX = 1200;
  const ZOOM_IN = PLOT_PX / (30 * MIN); // the default 30-minute window
  const ZOOM_OUT = PLOT_PX / (24 * 60 * MIN); // the widest 24-hour window
  const runOut = (runs) => runs.map((r) => ({ startMs: r.startMs, endMs: r.endMs, count: r.count, coalesced: r.coalesced }));
  // Same band with an extra beat 100ms behind the second one: at the SAME zoom
  // that keeps the 1/min beats apart, only that colliding pair merges.
  const near = [idles[0], idles[1], span({ name: "restart_coordinator", start: HB_BASE + MIN + 100, end: HB_BASE + MIN + 100, spanId: "hbn", parentId: "hbr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "idle" } } })];
  out.heartbeatBand = {
    model: schedulerBandModel(members, CLOCK),
    firstTickMs: idles[0].start,
    lastTickMs: idles[K - 1].start,
    zoomedIn: runOut(heartbeatRuns(idles, ZOOM_IN)),
    zoomedOut: runOut(heartbeatRuns(idles, ZOOM_OUT)),
    mixed: runOut(heartbeatRuns(near, ZOOM_IN)),
  };
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

    # #78: sibling cohorts are keyed by the shared raw parentId even when that
    # parent span aged out of the Timeline query. Exact call-id pairing hides the
    # completed marker only; a different concurrent Bash remains visibly open.
    mpi = r["missingParentCorrelId"]
    assert mpi["m1"]["rHide"] is True
    assert mpi["m1"]["rOpen"] is False
    assert mpi["m2"]["rHide"] is False
    assert mpi["m2"]["rOpen"] is True
    assert mpi["r1"]["rHide"] is False
    assert mpi["r1"]["rOpen"] is False

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

    # Recent open spans (well within the window) are NEVER abandoned — the orphan
    # marker and the in-flight talon stage keep their provisional open band.
    assert r["orphan"]["rAbandoned"] is False
    assert r["talonOpen"]["real"]["rAbandoned"] is False

    # #67 — SIGKILL / power-loss cap. A still-open span past STALE_MARKER_MS with
    # no recent cohort activity is ABANDONED (rOpen=False), bounded to evidence,
    # not painted running-to-now. All three open shapes are capped uniformly.
    ab_ho = r["abandonedHeldOpen"]  # held-open real span (frinz#657 shape)
    assert ab_ho["rAbandoned"] is True
    assert ab_ho["rOpen"] is False
    assert ab_ho["rEnd"] == 1000  # childless → capped to its own start (instant stub)
    ab_mk = r["abandonedMarker"]  # unpaired "(started)" marker, twin never arrived
    assert ab_mk["rAbandoned"] is True
    assert ab_mk["rOpen"] is False
    assert ab_mk["rEnd"] == 1000
    ab_tn = r["abandonedTurn"]  # summary-less live-tail turn root
    assert ab_tn["rAbandoned"] is True
    assert ab_tn["rOpen"] is False
    assert ab_tn["rEnd"] == 1000

    # Prefer observed evidence: an abandoned run WITH exported children ends at the
    # latest child end, not a fixed stub and not the live edge. The child span
    # itself (closed) is untouched.
    awc = r["abandonedWithChild"]
    assert awc["run"]["rAbandoned"] is True
    assert awc["run"]["rOpen"] is False
    assert awc["run"]["rEnd"] == 5000
    assert awc["tool"]["rAbandoned"] is False

    # Descendant-liveness exemption: a recent child keeps an OLD open root LIVE —
    # never abandoned (genuinely in-flight, self-correcting each poll).
    lck = r["liveChildKeepsOpen"]
    assert lck["run"]["rAbandoned"] is False
    assert lck["run"]["rOpen"] is True

    # Nested held-open subtree, both silent past the window → BOTH capped; a raw
    # still-open child must not exempt its parent forever (the frinz#657 nesting).
    na = r["nestedAbandoned"]
    assert na["run"]["rAbandoned"] is True
    assert na["run"]["rOpen"] is False
    assert na["stage"]["rAbandoned"] is True
    assert na["stage"]["rOpen"] is False

    # #69 — LIVE talon flicker fix. A held-open run parents its "<stage> (started)"
    # marker and its tool spans as SIBLINGS (the marker's own subtree is empty), so
    # per-span subtree liveness would abandon the marker the moment it crosses the
    # window despite constant sibling activity. Cohort liveness: a RECENT sibling
    # tool under the same run keeps the OLD marker open (never abandoned).
    ltc = r["liveTalonCohort"]
    assert ltc["marker"]["rAbandoned"] is False
    assert ltc["marker"]["rOpen"] is True
    assert ltc["tool"]["rAbandoned"] is False
    # Contrast: the SAME sibling shape with the whole cohort silent past the window
    # → the held-open marker still caps (the genuine SIGKILL case is preserved).
    dtc = r["deadTalonCohort"]
    assert dtc["marker"]["rAbandoned"] is True
    assert dtc["marker"]["rOpen"] is False

    # #67 P1 (live re-annotation): re-annotating the SAME span (no new IDs) with
    # only the clock advanced past STALE_MARKER_MS flips a recent open span to
    # abandoned. The live fix rebuilds the render model every poll tick, so
    # staleness is caught even when a poll adds nothing.
    ras = r["reAnnotateStale"]
    assert ras["whileRecent"]["rOpen"] is True
    assert ras["whileRecent"]["rAbandoned"] is False
    assert ras["afterAdvance"]["rOpen"] is False
    assert ras["afterAdvance"]["rAbandoned"] is True

    # #67 P1 (reconcile floor): abandonment must not sever the backdated-twin
    # re-fetch floor. While within the bounded reconcile grace an abandoned span
    # still anchors the poll floor (<= its backdated summary start); once the late
    # summary lands the span un-abandons + closes and the floor clears.
    ar = r["abandonedReconcile"]
    assert ar["abandonedBefore"] is True
    assert ar["floorWhileAbandoned"] is not None
    assert ar["coversTwin"] is True
    assert ar["reAbandonedAfter"] is False
    assert ar["closedAfter"] is True
    assert ar["afterEmpty"] is True

    # ...but the reconcile floor is BOUNDED: an ancient abandoned run (its twin
    # will never arrive) drops out of the floor, so the poll doesn't peg its
    # cursor to days-ago and re-scan the whole span forever.
    abr = r["abandonedBeyondReconcile"]
    assert abr["abandoned"] is True
    assert abr["floorEmpty"] is True

    # #87 — the virtual `session=scheduler` band. K=3 idle heartbeats + M=2 work
    # ticks: the band shows a heartbeat COUNT and represents BOTH kinds, and
    # nothing is dropped (the acceptance criterion for the renderer half).
    sb = r["schedulerBand"]["model"]
    assert sb is not None
    assert sb["idleCount"] == 3
    assert sb["workCount"] == 2
    assert sb["tickCount"] == 5
    # The band names itself as the virtual scheduler session and carries the
    # visible aggregate — heartbeats are AGGREGATED, never collapsed to nothing.
    assert sb["sessionId"] == "scheduler"
    assert sb["virtual"] is True
    # …including its real duration, so the label says what the band covers.
    assert sb["label"] == "scheduler · 3 heartbeats · 2 ticks · 5.5s"
    # #92: the band's envelope IS drawn — across its real min→max extent (the
    # root at 1000 through the last work tick at 6500) — because the fix for the
    # 5-hour bar is the virtual STYLE, not hiding how long the session ran.
    assert sb["envelope"] is True
    assert sb["startMs"] == 1000
    assert sb["endMs"] == 6500
    assert sb["durationMs"] == 5500
    # Every member still renders — the aggregate is additive, not a filter.
    assert sb["spanCount"] == r["schedulerBand"]["model"]["spanCount"]
    assert sb["spanCount"] == 11  # root + (marker+tick) × 5

    # Idle ticks are their own visual category (teal beat + "· idle" label); work
    # ticks are not. An idle tick is NOT a refusal, so it never takes the wide
    # denied/incomplete stub — it ran, it just did nothing.
    sbb = r["schedulerBand"]
    assert sbb["idleFlags"] == [True, True, True]
    assert sbb["idleLabels"] == ["restart_coordinator · idle"] * 3
    assert sbb["workIdleFlags"] == [False, False]
    assert sbb["idleOutcomes"] == [None, None, None]
    # The band self-identifies on every span (feeds the band tooltip).
    assert sbb["allScheduler"] is True
    # Nothing dropped: the paired "(started)" markers hide exactly like any other
    # twin, and NO tick is hidden.
    assert sbb["markersPaired"] is True
    assert sbb["hiddenTicks"] == 0

    # A normal agent band is not a scheduler band — no aggregate model, so it
    # keeps the ordinary continuous session envelope.
    nsb = r["nonSchedulerBand"]
    assert nsb["model"] is None
    assert nsb["scheduler"] is False
    assert nsb["idle"] is False

    # The all-idle band (the common every-minute case) still reports its count,
    # and the label singularizes at K=1 with no work-tick clause.
    sh = r["singleHeartbeat"]
    assert sh["idle"] is True
    assert sh["model"]["idleCount"] == 1
    assert sh["model"]["workCount"] == 0
    assert sh["model"]["label"] == "scheduler · 1 heartbeat · 1.0s"

    # #92 — the live band: 30 heartbeats ~1/min over 29 minutes, which painted as
    # a short 0ms-looking stub hiding both its extent and its ticks.
    hb = r["heartbeatBand"]
    K, MIN = 30, 60_000

    # (1) The envelope spans the REAL extent (~29m), not a zero-duration point,
    # and stays flagged virtual so the paint layer styles it translucent/dashed
    # instead of as a solid task bar.
    hbm = hb["model"]
    assert hbm["envelope"] is True
    assert hbm["virtual"] is True
    assert hbm["startMs"] == hb["firstTickMs"]
    assert hbm["endMs"] == hb["lastTickMs"]
    assert hbm["durationMs"] == 29 * MIN
    # The count label is ALWAYS present, coalescing or not — with the duration
    # the operator went looking for.
    assert hbm["label"] == "scheduler · 30 heartbeats · 29m 0s"
    assert hbm["idleCount"] == K

    # (2) Zoomed in (the default 30-minute window over a 1200px plot), 1/min beats
    # are ~40px apart, so every tick is represented at its OWN real time.
    zin = hb["zoomedIn"]
    assert len(zin) == K
    assert all(run["count"] == 1 and run["coalesced"] is False for run in zin)
    assert [run["startMs"] for run in zin] == [hb["firstTickMs"] + i * MIN for i in range(K)]

    # (3) Zoomed out (a 24-hour window over the same plot), the same beats are
    # <1px apart, so they coalesce into ONE run — carrying the full count.
    zout = hb["zoomedOut"]
    assert len(zout) == 1
    assert zout[0]["count"] == K
    assert zout[0]["coalesced"] is True
    # The run still covers the real extent, so the count sits on a bar wide
    # enough to read rather than a smear at a point.
    assert zout[0]["startMs"] == hb["firstTickMs"]
    assert zout[0]["endMs"] == hb["lastTickMs"]

    # (4) Coalescing is a per-pair PIXEL decision, not a mode: at the very zoom
    # that keeps the 1/min beats apart, only a pair 100ms apart merges.
    mixed = hb["mixed"]
    assert [run["count"] for run in mixed] == [1, 2]
    assert [run["coalesced"] for run in mixed] == [False, True]

    # Nothing is dropped at any scale — every beat is accounted for in some run.
    assert sum(run["count"] for run in zin) == K
    assert sum(run["count"] for run in zout) == K


# ── #88: what a container span COVERS ────────────────────────────────────────
#
# A turn/session root is a zero-duration point and the scheduler pseudo-root is
# an immortal envelope, so neither one's own geometry answers "what is this?".
# `spanMemberRollup` walks the subtree the layout already indexed and reports the
# membership the hover/popover renders as "covers N spans over Xh".
_ROLLUP_HARNESS = r"""
import { annotateRenderModel, spanMemberRollup } from "./timeline.js";

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
// The two indexes `mount` maintains: Phoenix node id → span, and parent OTel
// spanId → Set<node id>.
function index(list) {
  const spans = new Map();
  const kids = new Map();
  for (const s of list) {
    spans.set(s.id, s);
    if (!s.parentId) continue;
    let set = kids.get(s.parentId);
    if (!set) {
      set = new Set();
      kids.set(s.parentId, set);
    }
    set.add(s.id);
  }
  return [spans, kids];
}
const NOW = 10000;
const out = {};

// A turn with 2 tool calls: 2 "(started)" markers pair away and the summary
// folds, so the operator SEES 2 bars — the count must say 2, not 5.
{
  const turn = span({ name: "claude-code turn 1", start: 50, marker: "start", kind: "AGENT", spanId: "rt", sessionId: "R1", attrs: { kestrel: { turn_index: 1 } } });
  const members = [turn];
  for (const [i, tool] of ["Bash", "Read"].entries()) {
    const t = 60 + i * 10;
    members.push(span({ name: `${tool} (started)`, start: t, marker: "start", spanId: `rm${i}`, parentId: "rt", sessionId: "R1" }));
    members.push(span({ name: tool, start: t, end: t + 5, spanId: `rr${i}`, parentId: "rt", sessionId: "R1" }));
  }
  const summary = span({ name: "turn 1 summary", start: 50, end: 95, kind: "CHAIN", spanId: "rs", parentId: "rt", sessionId: "R1", attrs: { kestrel: { tool_count: 2, success_ratio: 1, turn_duration_ms: 45 } } });
  members.push(summary);
  const [spans, kids] = index(members);
  out.beforeAnnotate = spanMemberRollup(turn, spans, kids).count;
  annotateRenderModel(members, NOW);
  out.turn = {
    rollup: spanMemberRollup(turn, spans, kids),
    hidden: members.filter((s) => s.rHide).length,
    visible: members.filter((s) => s !== turn && !s.rHide).length,
  };
}

// The virtual scheduler pseudo-root (#87): heartbeats, work ticks, the features
// ticking under it and when each last ran.
{
  const root = span({ name: "kestrel-agent", start: 1000, kind: "AGENT", spanId: "mr", sessionId: "scheduler" });
  const members = [root];
  for (let i = 0; i < 3; i++) {
    const t = 2000 + i * 500;
    members.push(span({ name: "restart_coordinator (started)", start: t, marker: "start", spanId: `mim${i}`, parentId: "mr", sessionId: "scheduler" }));
    members.push(span({ name: "restart_coordinator", start: t, end: t, spanId: `mir${i}`, parentId: "mr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "idle", feature_name: "ReflectionFeature" } } }));
  }
  const wt = 6000;
  members.push(span({ name: "training_cycle (started)", start: wt, marker: "start", spanId: "mwm", parentId: "mr", sessionId: "scheduler" }));
  members.push(span({ name: "training_cycle", start: wt, end: wt, spanId: "mwr", parentId: "mr", sessionId: "scheduler", attrs: { kestrel: { tool_outcome: "completed", feature_name: "StrategicMemoryFeature" } } }));
  annotateRenderModel(members, NOW);
  const [spans, kids] = index(members);
  out.scheduler = spanMemberRollup(root, spans, kids);
}

// A NESTED subtree still rolls up through its grandchildren, and a leaf with no
// children has nothing to report.
{
  const root = span({ name: "kestrel-agent", start: 10, kind: "AGENT", spanId: "gr", sessionId: "R2" });
  const turn = span({ name: "kestrel-agent turn 1", start: 20, marker: "start", kind: "AGENT", spanId: "gt", parentId: "gr", sessionId: "R2", attrs: { kestrel: { turn_index: 1 } } });
  const tool = span({ name: "Bash", start: 30, end: 900, spanId: "gb", parentId: "gt", sessionId: "R2" });
  annotateRenderModel([root, turn, tool], NOW);
  const [spans, kids] = index([root, turn, tool]);
  out.nested = spanMemberRollup(root, spans, kids);
  out.leaf = spanMemberRollup(tool, spans, kids);
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_member_rollup_counts_what_the_operator_can_see(tmp_path):
    """#88: "covers N spans" counts rendered members, not render chrome."""
    pkg = _module_dir(tmp_path)
    (pkg / "rollup.mjs").write_text(_ROLLUP_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / "rollup.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(pkg),
    )
    r = json.loads(proc.stdout)

    # Un-annotated, everything in the subtree counts — the gate for the fix.
    assert r["beforeAnnotate"] == 5
    turn = r["turn"]
    assert turn["hidden"] == 3  # 2 paired markers + the folded summary
    assert turn["visible"] == 2  # the 2 tool bars actually painted
    # A 2-tool turn covers 2 spans. Counting the render chrome reported 5.
    assert turn["rollup"]["count"] == 2
    # The time extent still spans the WHOLE subtree: the folded summary never
    # paints a bar but carries the turn's honest end (50 → 95).
    assert turn["rollup"]["startMs"] == 50
    assert turn["rollup"]["endMs"] == 95
    # Heartbeat/tick counts are scheduler identity — noise on an ordinary turn.
    assert turn["rollup"]["virtual"] is False
    assert turn["rollup"]["heartbeatCount"] is None
    assert turn["rollup"]["workCount"] is None

    # The virtual scheduler session names itself and splits idle from work.
    sched = r["scheduler"]
    assert sched["virtual"] is True
    assert sched["count"] == 4  # 3 heartbeats + 1 work tick (markers paired away)
    assert sched["heartbeatCount"] == 3
    assert sched["workCount"] == 1
    assert sorted(sched["features"]) == ["ReflectionFeature", "StrategicMemoryFeature"]
    assert sched["lastIdleMs"] == 3000  # the newest heartbeat
    assert sched["lastWorkMs"] == 6000

    # Grandchildren roll up; a childless leaf reports nothing rather than "0".
    assert r["nested"]["count"] == 2
    assert r["nested"]["endMs"] == 900
    assert r["leaf"] is None


# ── #94: scroll pans, only a modifier zooms; the present edge is magnetic ─────
#
# A Magic Mouse / trackpad emits `wheel` deltas for an ordinary one-finger drag,
# so mapping vertical wheel → zoom made plain scrolling rescale the window. The
# two pure functions below ARE the interaction contract: `wheelIntent` decides
# pan-vs-zoom per event, `magneticViewEnd` decides whether a time-pan re-engages
# Live at the present edge.
_INPUT_HARNESS = r"""
import { wheelIntent, magneticViewEnd } from "./timeline.js";

const wheel = (o) => ({ deltaX: 0, deltaY: 0, ctrlKey: false, metaKey: false, ...o });
const out = {};

// Plain scroll: vertical → lanes, horizontal → time, diagonal → both. Never zoom.
out.plainDown = wheelIntent(wheel({ deltaY: 40 }));
out.plainUp = wheelIntent(wheel({ deltaY: -40 }));
out.plainRight = wheelIntent(wheel({ deltaX: 30 }));
out.diagonal = wheelIntent(wheel({ deltaX: 30, deltaY: 40 }));
// A big vertical delta is still lanes, even though the old handler zoomed here.
out.bigVertical = wheelIntent(wheel({ deltaX: 2, deltaY: 400 }));

// Modifier scroll: ctrl (also how a trackpad pinch arrives) and ⌘ both zoom,
// in both directions, around the cursor.
out.ctrlDown = wheelIntent(wheel({ deltaY: 40, ctrlKey: true }));
out.ctrlUp = wheelIntent(wheel({ deltaY: -40, ctrlKey: true }));
out.metaDown = wheelIntent(wheel({ deltaY: 40, metaKey: true }));
out.metaUp = wheelIntent(wheel({ deltaY: -40, metaKey: true }));
// deltaY == 0 → the horizontal axis drives the factor rather than falling
// through to a pan the modifier never asked for.
out.ctrlHorizontal = wheelIntent(wheel({ deltaX: 30, ctrlKey: true }));
// A zero-delta wheel does nothing at all.
out.ctrlIdle = wheelIntent(wheel({ ctrlKey: true }));

// Magnetic present edge: `snapMs` is the px threshold converted at the current
// scale. Inside it (including an overshoot past `now`) → snap + Live on.
const NOW = 1_000_000;
out.inside = magneticViewEnd(NOW - 50, NOW, 100);
out.overshoot = magneticViewEnd(NOW + 5000, NOW, 100);
out.exactly = magneticViewEnd(NOW - 100, NOW, 100);
out.outside = magneticViewEnd(NOW - 101, NOW, 100);
out.deepHistory = magneticViewEnd(NOW - 3_600_000, NOW, 100);

process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_wheel_intent_and_magnetic_edge(tmp_path):
    """#94: plain scroll pans (2-axis); zoom needs ctrl/⌘; `now` re-engages Live."""
    pkg = _module_dir(tmp_path)
    (pkg / "input.mjs").write_text(_INPUT_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / "input.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(pkg),
    )
    r = json.loads(proc.stdout)

    # Plain vertical scroll moves through the LANES — it never zooms and never
    # pans time. This is the whole point of #94.
    assert r["plainDown"] == {"panTimeDx": 0, "scrollLanesDy": 40}
    assert r["plainUp"] == {"panTimeDx": 0, "scrollLanesDy": -40}
    assert "zoom" not in r["plainDown"]
    # Plain horizontal scroll pans time only.
    assert r["plainRight"] == {"panTimeDx": 30, "scrollLanesDy": 0}
    # Diagonal applies both axes in the same event.
    assert r["diagonal"] == {"panTimeDx": 30, "scrollLanesDy": 40}
    # The old handler zoomed whenever |deltaY| >= |deltaX|; now it scrolls lanes.
    assert r["bigVertical"] == {"panTimeDx": 2, "scrollLanesDy": 400}

    # ctrl (= trackpad pinch) and ⌘ zoom, in both directions, and never pan.
    for key in ("ctrlDown", "metaDown"):
        assert r[key] == {"zoom": pytest.approx(1.15)}
    for key in ("ctrlUp", "metaUp"):
        assert r[key] == {"zoom": pytest.approx(1 / 1.15)}
    assert r["ctrlHorizontal"] == {"zoom": pytest.approx(1.15)}
    # No delta, no action — and still not a pan.
    assert r["ctrlIdle"] == {"panTimeDx": 0, "scrollLanesDy": 0}

    # Magnetic right edge: a pan landing within the threshold of `now` snaps
    # there and turns Live back on — wheel and drag share this path, so both
    # gestures now agree (drag used to clamp at `now` and stay paused).
    assert r["inside"] == {"viewEnd": 1_000_000, "live": True}
    assert r["overshoot"] == {"viewEnd": 1_000_000, "live": True}
    assert r["exactly"] == {"viewEnd": 1_000_000, "live": True}
    # Panning left beyond it keeps the exact edge asked for, paused.
    assert r["outside"] == {"viewEnd": 999_899, "live": False}
    assert r["deepHistory"] == {"viewEnd": 1_000_000 - 3_600_000, "live": False}
