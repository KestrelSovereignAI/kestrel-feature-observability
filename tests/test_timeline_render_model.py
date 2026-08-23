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

The tail of the module covers what feeds that model: the paged POLL WALK (#109),
mounted for real against a Phoenix double whose time-range and cursor semantics
are the server's, so a walk that gets truncated has to resume where it stopped
instead of quietly writing off the spans it never pulled.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from test_span_navigation_contract import _write_fake_dom

STATIC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "kestrel_feature_observability"
    / "fleet"
    / "static"
)

NODE = shutil.which("node")

# timeline.js tuning the poll-walk tests are written against.
PAGE_SIZE = 500
MAX_POLL_PAGES = 6
MAX_HISTORY_ROUNDS = 4


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


_FAKE_PHOENIX = r"""
// A Phoenix span-page double with REAL paging semantics (#109).
//
// The bug class this exists to catch is invisible to a lenient double, so this
// one refuses to fabricate anything the shipped server would not give:
//
//   - `timeRange` really filters: [start, end) over startTime, start INCLUSIVE
//     (which is what makes a boundary-millisecond tie observable at all).
//   - results are ascending by (startTime, id) and `first` really bounds a page.
//   - a cursor is an opaque handle into ONE query's result set. `after: null`
//     means "from the top" — it NEVER auto-advances (a previous harness did,
//     which fabricated continuation the client does not have and hid this whole
//     class of bug) — and a cursor replayed under different bounds is an error,
//     not a silently-reinterpreted offset.
//   - `holdCalls` parks a page in flight until `release()`, so a scenario can
//     interact with the view (pan, tick) DURING a walk — the real server is slow
//     and the client's own guards key on "a fetch is in flight".
export function installFakePhoenix({ projects, spans = [], failCalls = [], holdCalls = [] }) {
  const store = [...spans];
  const calls = [];
  const served = [];
  const failures = new Set(failCalls);
  const holds = new Set(holdCalls);
  const gates = new Map(); // call index → resolve fn of the parked page
  // One result set per (project, bounds), memoized: a cap-sized fixture is paged
  // ~120 times and re-filtering + re-sorting 60k rows per page dominates the run.
  // Semantics are unchanged — the cache is dropped whenever the store changes.
  const pools = new Map();
  const order = (a, b) =>
    Date.parse(a.startTime) - Date.parse(b.startTime) || String(a.id).localeCompare(String(b.id));
  const boundsKey = (projectId, timeRange) =>
    JSON.stringify([
      projectId,
      (timeRange && timeRange.start) || null,
      (timeRange && timeRange.end) || null,
    ]);
  const ok = (data) => ({ status: 200, ok: true, json: async () => ({ data }) });

  globalThis.fetch = async (_url, options) => {
    const { query, variables = {} } = JSON.parse(options.body);
    if (query.includes("NavigatorProjects")) {
      return ok({ projects: { edges: projects.map((node) => ({ node })) } });
    }
    if (!query.includes("NavigatorSpanPage")) throw new Error("unexpected GraphQL operation");
    const { projectId, first, after = null, timeRange = null } = variables;
    const call = { projectId, first, after, timeRange, endCursor: null, hasNext: null };
    calls.push(call);
    const index = calls.length - 1;
    if (failures.has(index)) throw new Error(`phoenix page ${index} failed`);
    if (holds.has(index)) {
      await new Promise((resolve) => gates.set(index, resolve));
    }

    const key = boundsKey(projectId, timeRange);
    const startMs = timeRange && timeRange.start != null ? Date.parse(timeRange.start) : null;
    const endMs = timeRange && timeRange.end != null ? Date.parse(timeRange.end) : null;
    let pool = pools.get(key);
    if (!pool) {
      pool = store
        .filter((s) => {
          if (s.projectId !== projectId) return false;
          const t = Date.parse(s.startTime);
          if (startMs != null && t < startMs) return false; // start is INCLUSIVE
          if (endMs != null && t >= endMs) return false;
          return true;
        })
        .sort(order);
      pools.set(key, pool);
    }

    let offset = 0;
    if (after != null) {
      const parsed = JSON.parse(globalThis.atob(after));
      if (parsed.key !== key) {
        throw new Error(`cursor replayed under different bounds: ${parsed.key} vs ${key}`);
      }
      offset = parsed.offset + 1;
    }
    const page = pool.slice(offset, offset + first);
    for (const s of page) served.push(s.id);
    call.hasNext = offset + page.length < pool.length;
    call.endCursor = page.length
      ? globalThis.btoa(JSON.stringify({ key, offset: offset + page.length - 1 }))
      : null;
    return ok({
      node: {
        spans: {
          pageInfo: { hasNextPage: call.hasNext, endCursor: call.endCursor },
          edges: page.map((node) => ({ node })),
        },
      },
    });
  };

  return {
    calls,
    served,
    add: (...more) => {
      pools.clear();
      store.push(...more);
    },
    held: () => [...gates.keys()],
    release(index) {
      const open = gates.get(index);
      if (!open) throw new Error(`call ${index} is not held`);
      gates.delete(index);
      open();
    },
  };
}

// A raw Phoenix span node as the client's normalize() reads it, plus the
// `projectId` this double routes on (the real node is reached THROUGH a project,
// so the field is harness bookkeeping the client never looks at).
//
// `open: true` is a span with NO closed end yet (a held-open talon run root, an
// in-flight turn): Phoenix reports a null `endTime`, which `normalize()` turns
// into `openEnded` with `end = start` — the degradation that used to sort live
// work to the FRONT of the eviction order (#111).
// `session`/`traceId` decide which eviction UNIT a span belongs to
// (`sessionKeyFor`: the stamped session id, else the trace), and `kestrel`
// carries any further producer attributes under that namespace (`tool_outcome`,
// `turn_index`, ...), merged over the shorthands.
export function rawSpan({
  id,
  name,
  start,
  dur = 40,
  agent,
  spanId,
  parentId = null,
  kind = "TOOL",
  projectId,
  open = false,
  marker = null,
  session = null,
  traceId = "trace-1",
  kestrel = null,
}) {
  return {
    id,
    name,
    spanKind: kind.toLowerCase(),
    startTime: new Date(start).toISOString(),
    endTime: open ? null : new Date(start + dur).toISOString(),
    latencyMs: open ? null : dur,
    statusCode: "OK",
    parentId,
    attributes: JSON.stringify({
      openinference: { span: { kind } },
      kestrel: {
        agent_name: agent,
        ...(marker != null ? { marker } : {}),
        ...(session != null ? { session_id: session } : {}),
        ...(kestrel || {}),
      },
    }),
    context: { spanId, traceId },
    projectId,
  };
}

// Let every in-flight walk finish: wait until fetch activity goes quiet.
export async function settle(calls) {
  let last = -1;
  let quiet = 0;
  for (let i = 0; i < 600; i++) {
    await new Promise((resolve) => setTimeout(resolve, 2));
    if (calls.length === last) {
      quiet += 1;
      if (quiet >= 8) return;
    } else {
      quiet = 0;
      last = calls.length;
    }
  }
  throw new Error("fetch activity never settled");
}

// Every string the canvas painted — lane gutter labels and span bar labels — so
// "was it ingested" is answered by what the view actually renders.
export function paintedText(canvas) {
  const frames = (canvas.context && canvas.context.frames) || [];
  const out = [];
  for (const frame of frames) {
    for (const op of frame.operations) {
      if (op.type === "fillText") out.push(String(op.args[0]));
    }
  }
  return out;
}

// The operations of the MOST RECENT frame only. A cap-sized store paints tens of
// thousands of operations per frame, so scenarios that repaint many times drop
// earlier frames (`forgetFrames`) and read the last one — the only frame that
// shows the settled store.
export function lastFrame(canvas) {
  const frames = (canvas.context && canvas.context.frames) || [];
  return frames.length ? frames[frames.length - 1].operations : [];
}

export function forgetFrames(canvas) {
  if (canvas.context) canvas.context.frames.length = 0;
}

export function frameText(ops) {
  return ops.filter((op) => op.type === "fillText").map((op) => String(op.args[0]));
}
"""


def _poll_pkg(tmp_path: pathlib.Path) -> pathlib.Path:
    """Module dir for the mounted poll-walk tests: shipped JS + DOM/Phoenix doubles."""
    pkg = _module_dir(tmp_path)
    _write_fake_dom(pkg)
    (pkg / "fake-phoenix.mjs").write_text(_FAKE_PHOENIX, encoding="utf-8")
    return pkg


def _run_scenario(pkg: pathlib.Path, name: str, source: str) -> dict:
    (pkg / name).write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / name)],
        cwd=str(pkg),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout)


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


# ── #101: orchestrated lanes nest under their orchestrator ───────────────────
#
# A talon run launched BY an agent carries `kestrel.orchestrator` = that agent on
# every span, but the layout bucketed project → agent → worker and sorted agents
# alphabetically, so Emma's talon run rendered as a sibling top-level `talon`
# lane BELOW Emma. `laneGroups` is the pure grouping the layout now consumes: it
# keys lanes by the (agent, orchestrator) PAIR and nests them, so the same agent
# driven by two orchestrators is two lanes in two places.
_LANE_HARNESS = r"""
import { annotateRenderModel, laneGroups } from "./timeline.js";

let idc = 0;
// A normalized span record shaped like timeline.js's normalize() output — with
// the lane-relevant fields (projectName / agent / worker / orchestrator) the
// grouping reads.
function span(o) {
  idc += 1;
  return {
    id: o.id || `n${idc}`,
    name: o.name || "work",
    start: o.start != null ? o.start : 1000,
    end: o.end != null ? o.end : (o.start != null ? o.start : 1000) + 10,
    instant: false,
    openEnded: false,
    marker: null,
    kind: o.kind || "TOOL",
    status: "ok",
    spanId: o.spanId || `s${idc}`,
    parentId: o.parentId || null,
    traceId: null,
    sessionId: o.sessionId != null ? o.sessionId : `sess${idc}`,
    projectId: o.projectId != null ? o.projectId : "P",
    projectName: o.projectName || "kestrel-fleet",
    agent: o.agent,
    worker: o.worker || null,
    orchestrator: o.orchestrator != null ? o.orchestrator : null,
    rHide: o.rHide === true,
    attrs: o.attrs || {},
  };
}
// The lane shape the layout turns into rows, JSON-friendly.
const shape = (lanes) =>
  lanes.map((l) => ({
    label: l.label,
    level: l.level,
    agent: l.agent,
    orchestrator: l.orchestrator,
    worker: l.worker,
    count: l.items.length,
  }));
const groups = (list) =>
  Object.fromEntries([...laneGroups(list)].map(([name, lanes]) => [name, shape(lanes)]));
const out = {};

// The live `kestrel-fleet` shape from the issue: four plain agents, Emma's talon
// run (with its two worker stages), talon runs orchestrated by agents that have
// NO lane here (claude-code / codex — top-level, but each still its own lane), a
// `Direct` talon run, and Claw's own self-orchestrated spans.
{
  const list = [
    span({ agent: "Claw" }),
    span({ agent: "Claw", orchestrator: "Claw" }), // rule 3 — same lane, not nested
    span({ agent: "Emma" }),
    span({ agent: "Meridian" }),
    span({ agent: "Nellie" }),
    span({ agent: "talon", orchestrator: "Emma", name: "talon run" }),
    span({ agent: "talon", orchestrator: "Emma", worker: "implement" }),
    span({ agent: "talon", orchestrator: "Emma", worker: "review" }),
    span({ agent: "talon", orchestrator: "claude-code" }), // rule 2 — no lane here
    span({ agent: "talon", orchestrator: "codex" }), // rule 2
    span({ agent: "talon", orchestrator: "Direct" }), // rule 4
  ];
  out.fleet = groups(list)["kestrel-fleet"];
  // The reveal key: the orchestrator each span's lane RESOLVED to (null = the
  // agent's top-level lane), not the raw attribute.
  out.fleetLaneOrchestrators = list.map((s) => s.rLaneOrchestrator);
}

// Rule 2 in isolation: the orchestrator names an agent with no lane in THIS
// project, so the run stays top-level — but it KEEPS its own identity (rule 2 is
// placement, not erasure), so the lane is still labeled by its launcher.
{
  out.noParentLane = groups([
    span({ agent: "talon", orchestrator: "claude-code" }),
    span({ agent: "talon", orchestrator: "claude-code", worker: "implement" }),
  ])["kestrel-fleet"];
}

// …and two such orchestrators stay two DISTINCT top-level lanes: neither
// `claude-code` nor `codex` has a lane here, but pooling their runs into one
// anonymous `talon` band would merge unrelated launchers into a single row.
// Only the true "nobody launched this" spans (`Direct`, no attribute) share the
// plain lane.
{
  out.unresolvedSplit = groups([
    span({ agent: "talon", orchestrator: "claude-code" }),
    span({ agent: "talon", orchestrator: "claude-code" }),
    span({ agent: "talon", orchestrator: "codex" }),
    span({ agent: "talon", orchestrator: "Direct" }),
    span({ agent: "talon" }),
  ])["kestrel-fleet"];
}

// Rule 2 is PER PROJECT: Emma has a lane in kestrel-fleet but not in owner/repo,
// so the identical orchestrator attribution nests in one project and not in the
// other.
{
  out.perProject = groups([
    span({ agent: "Emma", projectName: "kestrel-fleet" }),
    span({ agent: "talon", orchestrator: "Emma", projectName: "kestrel-fleet" }),
    span({ agent: "talon", orchestrator: "Emma", projectName: "owner/repo" }),
  ]);
}

// Rule 3 in isolation: `Claw` spans stamped `orchestrator=Claw` are ONE plain
// top-level lane — an agent never parents itself.
{
  out.selfOrchestrated = groups([
    span({ agent: "Claw", orchestrator: "Claw" }),
    span({ agent: "Claw", orchestrator: "Claw" }),
    span({ agent: "Claw" }),
  ])["kestrel-fleet"];
}

// Rule 4 in isolation, at its sharpest: an agent literally named `Direct` HAS a
// lane here (so rule 2 would pass), and the sentinel must still not nest.
{
  out.directSentinel = groups([
    span({ agent: "Direct" }),
    span({ agent: "talon", orchestrator: "Direct" }),
  ])["kestrel-fleet"];
}

// One agent, three orchestrators → three lanes: nested under each launcher that
// has a lane here, and top-level (but still its own lane) for the one that
// doesn't.
{
  const emma1 = span({ agent: "talon", orchestrator: "Emma", name: "for Emma" });
  const claw1 = span({ agent: "talon", orchestrator: "Claw", name: "for Claw" });
  const loose = span({ agent: "talon", orchestrator: "claude-code", name: "loose" });
  const list = [span({ agent: "Emma" }), span({ agent: "Claw" }), emma1, claw1, loose];
  out.split = groups(list)["kestrel-fleet"];
  out.splitLaneOrchestrators = {
    emma: emma1.rLaneOrchestrator,
    claw: claw1.rLaneOrchestrator,
    loose: loose.rLaneOrchestrator,
  };
}

// Worker sub-lanes follow their OWN lane: the same worker name under two
// orchestrators is two sub-lanes, each with its parent's level + 1.
{
  out.splitWorkers = groups([
    span({ agent: "Emma" }),
    span({ agent: "talon", orchestrator: "Emma", worker: "implement" }),
    span({ agent: "talon", orchestrator: "claude-code", worker: "implement" }),
  ])["kestrel-fleet"];
}

// Render chrome never invents a lane: an `rHide` span (a paired "(started)"
// marker / folded summary) is dropped exactly as the layout drops it, so a
// hidden-only agent gets no lane and can't satisfy rule 2 for anyone.
{
  out.hidden = groups([
    span({ agent: "Emma" }),
    span({ agent: "ghost", rHide: true }),
    span({ agent: "talon", orchestrator: "ghost" }),
  ])["kestrel-fleet"];
}

// A mutual orchestration cycle (A↔B, neither top-level) still renders both lanes
// — never dropped on the floor.
{
  out.cycle = groups([
    span({ agent: "A", orchestrator: "B" }),
    span({ agent: "B", orchestrator: "A" }),
  ])["kestrel-fleet"];
}

// End to end with the render model: a real talon run whose "(started)" marker
// pairs away still lands in the nested lane, with only the visible bar in it.
{
  const list = [
    span({ agent: "Emma", name: "Emma turn 1" }),
    span({ agent: "talon", orchestrator: "Emma", name: "implement (started)", start: 100, spanId: "m1", sessionId: "run#1" }),
    span({ agent: "talon", orchestrator: "Emma", name: "implement", start: 100, end: 500, spanId: "r1", sessionId: "run#1" }),
  ];
  list[1].marker = "start";
  list[1].end = 100;
  list[1].parentId = "r1";
  annotateRenderModel(list, 10_000);
  out.annotated = { lanes: groups(list)["kestrel-fleet"], markerHidden: list[1].rHide };
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_lane_groups_nest_orchestrated_lanes(tmp_path):
    """#101: a run nests under the agent that launched it, keyed by the pair."""
    pkg = _module_dir(tmp_path)
    (pkg / "lanes.mjs").write_text(_LANE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / "lanes.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(pkg),
    )
    r = json.loads(proc.stdout)

    # The whole point: Emma's talon run renders UNDER Emma (labeled by its
    # orchestrator) with its worker stages below it, while the talon runs nobody
    # here launched keep their own top-level lane at the bottom.
    fleet = r["fleet"]
    assert [(lane["label"], lane["level"]) for lane in fleet] == [
        ("Claw", 1),
        ("Emma", 1),
        ("Emma/talon", 2),
        # Worker segments render as prose (#104); the lane's `worker` key below
        # stays the raw stage value.
        ("talon/Implement", 3),
        ("talon/Review", 3),
        ("Meridian", 1),
        ("Nellie", 1),
        ("talon", 1),
        ("claude-code/talon", 1),
        ("codex/talon", 1),
    ]
    # Levels, spelled out: an orchestrator-nested agent lane is 2 and its worker
    # sub-lanes are 3 (a top-level agent stays 1 and its workers 2).
    nested = next(lane for lane in fleet if lane["label"] == "Emma/talon")
    assert nested["level"] == 2
    assert nested["agent"] == "talon"
    assert nested["orchestrator"] == "Emma"
    assert nested["worker"] is None
    assert [lane["level"] for lane in fleet if lane["worker"] == "implement"] == [3]

    # Rule 3: Claw's self-orchestrated spans stayed in the ONE plain Claw lane
    # (2 spans) — an agent never parents itself — and rule 4 put the `Direct`
    # talon run in the plain `talon` lane. Rule 2 is placement only: the
    # claude-code and codex runs stay top-level but keep their own identities
    # rather than pooling into that plain lane.
    claw = next(lane for lane in fleet if lane["label"] == "Claw")
    assert (claw["level"], claw["orchestrator"], claw["count"]) == (1, None, 2)
    loose = next(lane for lane in fleet if lane["label"] == "talon")
    assert (loose["level"], loose["orchestrator"], loose["count"]) == (1, None, 1)
    for launcher in ("claude-code", "codex"):
        lane = next(x for x in fleet if x["label"] == f"{launcher}/talon")
        assert (lane["level"], lane["agent"], lane["orchestrator"], lane["count"]) == (
            1,
            "talon",
            launcher,
            1,
        )

    # The lane's orchestrator identity is stamped per span — the key
    # scroll-to-lane matches on, now that (project, agent, worker) no longer
    # identifies one row. Only `Direct` and self-orchestration normalize to null.
    assert r["fleetLaneOrchestrators"] == [
        None,  # Claw
        None,  # Claw orchestrated by Claw (rule 3)
        None,  # Emma
        None,  # Meridian
        None,  # Nellie
        "Emma",  # talon run launched by Emma
        "Emma",  # …and its worker stages
        "Emma",
        "claude-code",  # talon by claude-code — top-level (rule 2), own lane
        "codex",  # talon by codex — likewise
        None,  # talon by the `Direct` sentinel (rule 4)
    ]

    # Rule 2: an orchestrator with no lane in this project leaves the run
    # top-level — at level 1, with an ordinary worker sub-lane — while the lane
    # still names its launcher.
    assert r["noParentLane"] == [
        {
            "label": "claude-code/talon",
            "level": 1,
            "agent": "talon",
            "orchestrator": "claude-code",
            "worker": None,
            "count": 1,
        },
        {
            "label": "talon/Implement",
            "level": 2,
            "agent": "talon",
            "orchestrator": "claude-code",
            "worker": "implement",
            "count": 1,
        },
    ]

    # Two unresolvable orchestrators are two lanes, not one pooled band; only the
    # `Direct` and unattributed spans share the plain `talon` lane.
    assert [
        (lane["label"], lane["level"], lane["orchestrator"], lane["count"])
        for lane in r["unresolvedSplit"]
    ] == [
        ("talon", 1, None, 2),
        ("claude-code/talon", 1, "claude-code", 2),
        ("codex/talon", 1, "codex", 1),
    ]

    # …and rule 2 is evaluated PER PROJECT: the identical attribution nests where
    # Emma has a lane and stays top-level where she does not — same identity in
    # both, different placement.
    per_project = r["perProject"]
    assert [(lane["label"], lane["level"]) for lane in per_project["kestrel-fleet"]] == [
        ("Emma", 1),
        ("Emma/talon", 2),
    ]
    assert [
        (lane["label"], lane["level"], lane["orchestrator"])
        for lane in per_project["owner/repo"]
    ] == [
        ("Emma/talon", 1, "Emma"),
    ]

    # Rule 3: no self-nesting — one top-level lane holding all three spans.
    assert r["selfOrchestrated"] == [
        {
            "label": "Claw",
            "level": 1,
            "agent": "Claw",
            "orchestrator": None,
            "worker": None,
            "count": 3,
        },
    ]

    # Rule 4: `Direct` means "no orchestrator" — even when an agent by that name
    # really does have a lane here, it never becomes a parent.
    assert [(lane["label"], lane["level"]) for lane in r["directSentinel"]] == [
        ("Direct", 1),
        ("talon", 1),
    ]

    # One agent under three orchestrators → three lanes: one under each launcher
    # that has a lane here, and the third top-level but still its own. This is the
    # (agent, orchestrator) pair key.
    assert [(lane["label"], lane["level"]) for lane in r["split"]] == [
        ("Claw", 1),
        ("Claw/talon", 2),
        ("Emma", 1),
        ("Emma/talon", 2),
        ("claude-code/talon", 1),
    ]
    # Each run landed in its own lane — the spans are partitioned, not duplicated.
    assert [lane["count"] for lane in r["split"]] == [1, 1, 1, 1, 1]
    assert r["splitLaneOrchestrators"] == {
        "emma": "Emma",
        "claw": "Claw",
        "loose": "claude-code",
    }

    # Worker sub-lanes follow their own lane, each at its parent's level + 1.
    assert [(lane["label"], lane["level"]) for lane in r["splitWorkers"]] == [
        ("Emma", 1),
        ("Emma/talon", 2),
        ("talon/Implement", 3),
        ("claude-code/talon", 1),
        ("talon/Implement", 2),
    ]

    # Render chrome never invents a lane: the hidden `ghost` span gets no lane, so
    # it can't satisfy rule 2 either — its talon run stays top-level (keeping the
    # attribution the span really carries).
    assert [(lane["label"], lane["level"]) for lane in r["hidden"]] == [
        ("Emma", 1),
        ("ghost/talon", 1),
    ]

    # A mutual orchestration cycle still renders both lanes rather than losing one.
    assert sorted(lane["label"] for lane in r["cycle"]) == ["A/B", "B/A"]
    assert all(lane["count"] == 1 for lane in r["cycle"])

    # With the render model resolved, the paired "(started)" marker is dropped and
    # the nested lane holds exactly the one visible bar.
    ann = r["annotated"]
    assert ann["markerHidden"] is True
    assert [(lane["label"], lane["count"]) for lane in ann["lanes"]] == [
        ("Emma", 1),
        ("Emma/talon", 1),
    ]


# ── #104: talon stage labels read as prose ──────────────────────────────────
#
# Talon opens a stage scope as ``start_span(<stage>)``, so a stage span is NAMED
# by the ``kestrel.stage`` it carries, and its live-visibility twin (#80) is that
# same name plus ``" (started)"``. Both spellings must land on the same prose or
# one stage paints two bars under two names — and the marker never equals its own
# stage value, which is exactly what an equality-only rule misses (#103).
#
# The NAME test is the whole rule; nothing may gate on the span KIND. Talon picks
# the kind PER STAGE (``_STAGE_SPAN_KINDS``: implement/review ``LLM``, coordinate
# ``AGENT``, gate ``CHAIN``, else ``CHAIN``), so a kind gate stops recognizing the
# very bars it was written for the moment that mapping moves — while the gutter,
# which has no such gate, keeps reading as prose. The harness below therefore
# never invents a kind: every stage span carries the one its producer emits.
#
# Everything else stays exactly as emitted. ``kestrel.stage`` is stamped on EVERY
# span inside a stage — ``command_execution``, ``Bash``, ``ci``, ``ci (waiting)``
# and ``self-review`` all carry it — and none of them is named for its stage, so
# the name test excludes them precisely.
_STAGE_LABEL_HARNESS = r"""
import { annotateRenderModel, laneGroups } from "./timeline.js";

let idc = 0;
function span(o) {
  idc += 1;
  return {
    id: o.id || `n${idc}`,
    name: o.name,
    start: o.start != null ? o.start : 100,
    end: o.end != null ? o.end : (o.start != null ? o.start : 100) + 50,
    instant: false,
    openEnded: false,
    marker: o.marker || null,
    kind: o.kind || "TOOL",
    status: "ok",
    spanId: o.spanId || `s${idc}`,
    parentId: o.parentId || null,
    traceId: "trace-1",
    sessionId: "run#7",
    projectId: "P",
    projectName: "kestrel-fleet",
    agent: o.agent || "talon",
    worker: o.worker !== undefined ? o.worker : null,
    orchestrator: o.orchestrator != null ? o.orchestrator : null,
    attrs: o.attrs || {},
  };
}

// The producer's own per-stage tables (kestreltalon/observability.py). A stage
// has no single kind and no single agent name, so a test that invents either
// cannot catch a rule that keys off them.
const STAGE_KINDS = { coordinate: "AGENT", implement: "LLM", review: "LLM", gate: "CHAIN" };
const STAGE_AGENTS = { implement: "talon/implement", review: "talon/review" };

// A talon stage span exactly as `stage_span_scope` emits it: NAMED for the
// stage, carrying it as `kestrel.stage`, with that stage's kind and agent name.
const stage = (name, value, o = {}) =>
  span({
    name,
    worker: value,
    kind: STAGE_KINDS[value] || "CHAIN",
    attrs: { kestrel: { stage: value, agent_name: STAGE_AGENTS[value] || "talon" } },
    ...o,
  });
// A span nested INSIDE a stage: it inherits `kestrel.stage`, with its own name
// and its own kind.
const inStage = (name, value, kind, o = {}) =>
  span({ name, worker: value, kind, attrs: { kestrel: { stage: value } }, ...o });
// What the draw layer actually paints for a bar (timeline.js: `s.rLabel || s.name`).
const painted = (s) => s.rLabel || s.name;
const out = {};

{
  const impl = stage("implement", "implement", { spanId: "impl", start: 100, end: 500 });
  // Its twin marker, parented UNDER the bar (talon's shape) — paired away.
  const implMarker = stage("implement (started)", "implement", {
    spanId: "im2", parentId: "impl", start: 100, end: 100, marker: "start",
  });
  // …and an UNPAIRED one, the only case a "(started)" marker paints its own
  // band: the stage is still in flight, so its label is on screen.
  const liveMarker = stage("review (started)", "review", {
    spanId: "rv2", parentId: "not-loaded", start: 500, end: 500, marker: "start",
  });
  const review = stage("review", "review", { spanId: "rev", start: 500, end: 800 });
  const check = stage("completion-check", "completion-check", { spanId: "cc", start: 800, end: 900 });
  const coordinate = stage("coordinate", "coordinate", { spanId: "co", start: 900, end: 950 });
  const gate = stage("gate", "gate", { spanId: "ga", start: 950, end: 980 });
  // A refused stage composes its outcome onto the SAME prose base.
  const denied = stage("implement", "implement", {
    spanId: "dn", start: 990, end: 990,
    attrs: { kestrel: { stage: "implement", tool_outcome: "denied" } },
  });
  // Untouched: every span nested under a stage inherits its `kestrel.stage` —
  // the tool spans, the gate checks, and a gate's own "(waiting)" tick, whose
  // name is not a marker of its stage.
  const bash = inStage("Bash", "implement", "TOOL", { spanId: "bs", start: 110, end: 130 });
  const command = inStage("command_execution", "implement", "TOOL", { spanId: "ce", start: 140, end: 160 });
  const selfReview = inStage("self-review", "gate", "CHAIN", { spanId: "sr", start: 951, end: 960 });
  const ci = inStage("ci", "gate", "CHAIN", { spanId: "ci", start: 961, end: 970 });
  const ciWait = inStage("ci (waiting)", "gate", "TOOL", { spanId: "cw", start: 971, end: 975 });
  // …and a stage-LOOKING name with no `kestrel.stage` at all is just a name.
  const bare = span({ name: "review", spanId: "br", start: 200, end: 220 });

  const list = [impl, implMarker, liveMarker, review, check, coordinate, gate, denied,
                bash, command, selfReview, ci, ciWait, bare];
  annotateRenderModel(list, 10_000);
  out.bars = Object.fromEntries(list.map((s) => [s.spanId, painted(s)]));
  out.hidden = { implMarker: implMarker.rHide, liveMarker: liveMarker.rHide };
  out.liveMarkerOpen = liveMarker.rOpen;
  // The producer contract must survive the display pass untouched — including
  // the KINDS, which differ across the stage bars that all read as prose.
  out.contract = list.map((s) => ({
    spanId: s.spanId,
    name: s.name,
    kind: s.kind,
    stage: s.attrs.kestrel ? s.attrs.kestrel.stage || null : null,
    worker: s.worker,
  }));
}

// The gutter: a worker sub-lane label title-cases the WORKER segment only.
{
  const lanes = laneGroups([
    span({ agent: "talon", name: "run" }),
    stage("implement", "implement"),
    stage("completion-check", "completion-check"),
    span({ agent: "Emma", name: "Emma turn 1" }),
    stage("review", "review", { orchestrator: "Emma" }),
  ]);
  out.gutter = [...lanes.get("kestrel-fleet")].map((l) => ({
    label: l.label,
    level: l.level,
    agent: l.agent,
    worker: l.worker,
  }));
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_stage_labels_render_as_prose(tmp_path):
    """#104: talon stage bars, their markers and sub-lanes read as prose."""
    pkg = _module_dir(tmp_path)
    (pkg / "stages.mjs").write_text(_STAGE_LABEL_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / "stages.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(pkg),
    )
    r = json.loads(proc.stdout)

    bars = r["bars"]
    # The stage bars — and a hyphenated stage in SENTENCE case, not
    # Title-Case-Every-Word.
    assert bars["impl"] == "Implement"
    assert bars["rev"] == "Review"
    assert bars["cc"] == "Completion check"
    assert bars["co"] == "Coordinate"
    assert bars["ga"] == "Gate"

    # The "(started)" marker is the half #103's equality rule missed: it never
    # equals its own stage value, so the pair would have rendered inconsistently.
    # The suffix survives the casing.
    assert bars["im2"] == "Implement (started)"
    assert bars["rv2"] == "Review (started)"
    # …and the one that actually paints is the unpaired marker (its twin hasn't
    # landed): the paired one is still dropped, not relabeled into a second bar.
    assert r["hidden"] == {"implMarker": True, "liveMarker": False}
    assert r["liveMarkerOpen"] is True

    # A refused stage composes its outcome onto the same prose base (#84 label).
    assert bars["dn"] == "Implement · denied"

    # Everything else is untouched — every span nested under a stage carries the
    # SAME `kestrel.stage`, and a stage-looking name with no stage at all is a
    # name like any other.
    assert bars["bs"] == "Bash"
    assert bars["ce"] == "command_execution"
    assert bars["sr"] == "self-review"
    assert bars["ci"] == "ci"
    assert bars["cw"] == "ci (waiting)"
    assert bars["br"] == "review"

    # Display only: neither the span name nor the `kestrel.stage` value moved —
    # the attribute is a producer contract `workerOf` keys worker sub-lanes off.
    # And the KINDS: the stage bars that all read as prose are LLM, CHAIN and
    # AGENT, so no rule that recognizes them may key on the kind.
    assert r["contract"] == [
        {
            "spanId": "impl",
            "name": "implement",
            "kind": "LLM",
            "stage": "implement",
            "worker": "implement",
        },
        {
            "spanId": "im2",
            "name": "implement (started)",
            "kind": "LLM",
            "stage": "implement",
            "worker": "implement",
        },
        {
            "spanId": "rv2",
            "name": "review (started)",
            "kind": "LLM",
            "stage": "review",
            "worker": "review",
        },
        {"spanId": "rev", "name": "review", "kind": "LLM", "stage": "review", "worker": "review"},
        {
            "spanId": "cc",
            "name": "completion-check",
            "kind": "CHAIN",
            "stage": "completion-check",
            "worker": "completion-check",
        },
        {
            "spanId": "co",
            "name": "coordinate",
            "kind": "AGENT",
            "stage": "coordinate",
            "worker": "coordinate",
        },
        {"spanId": "ga", "name": "gate", "kind": "CHAIN", "stage": "gate", "worker": "gate"},
        {
            "spanId": "dn",
            "name": "implement",
            "kind": "LLM",
            "stage": "implement",
            "worker": "implement",
        },
        {"spanId": "bs", "name": "Bash", "kind": "TOOL", "stage": "implement", "worker": "implement"},
        {
            "spanId": "ce",
            "name": "command_execution",
            "kind": "TOOL",
            "stage": "implement",
            "worker": "implement",
        },
        {"spanId": "sr", "name": "self-review", "kind": "CHAIN", "stage": "gate", "worker": "gate"},
        {"spanId": "ci", "name": "ci", "kind": "CHAIN", "stage": "gate", "worker": "gate"},
        {"spanId": "cw", "name": "ci (waiting)", "kind": "TOOL", "stage": "gate", "worker": "gate"},
        {"spanId": "br", "name": "review", "kind": "TOOL", "stage": None, "worker": None},
    ]

    # The gutter: the WORKER segment reads as prose; the agent segment is a name
    # and keeps its own casing (`talon`, never `Talon`).
    assert [(lane["label"], lane["level"]) for lane in r["gutter"]] == [
        ("Emma", 1),
        ("Emma/talon", 2),
        ("talon/Review", 3),
        ("talon", 1),
        ("talon/Completion check", 2),
        ("talon/Implement", 2),
    ]
    # …and the lane's `worker` key — what scroll-to-lane matches on — is the raw
    # stage value, not the displayed one.
    assert sorted(lane["worker"] for lane in r["gutter"] if lane["worker"]) == [
        "completion-check",
        "implement",
        "review",
    ]


# The same rule over the checked-in REAL Talon trace (`fixtures/talon_trace.json`
# — Phoenix GraphQL nodes verbatim, `attributes` still the serialized JSON
# string). It is the shape a synthetic record can drift from: its stage bars are
# `implement` (LLM) with its `implement (started)` twin, `review` (LLM) and
# `gate` (CHAIN) — three kinds, one prose rule — while the children that inherit
# their stage token (`Bash`, `command_execution`, `self-review`) and the run root
# must not move at all.
_STAGE_FIXTURE_HARNESS = r"""
import { readFileSync } from "node:fs";
import { annotateRenderModel, laneGroups } from "./timeline.js";
import { parseAttributes, getAttr, baseAgentName, workerOf, sessionKeyOf, spanKindOf,
         ts } from "./phoenix.js";

// Mirrors timeline.js normalize() for the fields the label + lane paths read.
function normalize(raw, projectName) {
  const start = ts(raw.startTime);
  const rawEnd = ts(raw.endTime);
  const hasEnd = rawEnd != null && rawEnd >= start;
  const attrs = parseAttributes(raw.attributes);
  const agentRaw = getAttr(attrs, "kestrel.agent_name");
  const sess = sessionKeyOf(attrs);
  return {
    id: raw.id,
    name: raw.name || "(span)",
    start,
    end: hasEnd ? rawEnd : start,
    instant: hasEnd && rawEnd <= start,
    openEnded: !hasEnd,
    marker: getAttr(attrs, "kestrel.marker") || null,
    kind: spanKindOf(raw),
    status: raw.statusCode === "ERROR" ? "error" : "ok",
    agent: agentRaw ? baseAgentName(agentRaw) : "unknown",
    worker: workerOf(attrs),
    orchestrator: getAttr(attrs, "kestrel.orchestrator") || null,
    sessionId: sess ? sess.id : null,
    spanId: (raw.context && raw.context.spanId) || null,
    parentId: raw.parentId || null,
    traceId: (raw.context && raw.context.traceId) || null,
    projectId: "P",
    projectName,
    attrs,
  };
}

const fixture = JSON.parse(readFileSync(process.argv[2], "utf8"));
const spans = fixture.talon_trace.map((n) => normalize(n, "UncleSaurus/widget"));
annotateRenderModel(spans, ts("2026-07-18T20:20:00+00:00"));

const out = {
  bars: Object.fromEntries(spans.map((s) => [s.name, s.rLabel || s.name])),
  // The producer contract, read back off the fixture records after the pass.
  contract: spans.map((s) => ({
    name: s.name,
    kind: s.kind,
    stage: getAttr(s.attrs, "kestrel.stage") || null,
    worker: s.worker,
  })),
  gutter: [...laneGroups(spans).get("UncleSaurus/widget")].map((l) => ({
    label: l.label,
    level: l.level,
    worker: l.worker,
  })),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_stage_labels_over_real_talon_fixture(tmp_path):
    """#104: the REAL Talon stage bars read as prose, gutter and bar alike."""
    pkg = _module_dir(tmp_path)
    (pkg / "stage-fixture.mjs").write_text(_STAGE_FIXTURE_HARNESS, encoding="utf-8")
    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "talon_trace.json"
    proc = subprocess.run(
        [NODE, str(pkg / "stage-fixture.mjs"), str(fixture)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(pkg),
    )
    r = json.loads(proc.stdout)

    # The bars — an LLM stage, its LLM marker, and a CHAIN stage: one rule, and
    # not one of the three kinds a gate could have been written against.
    assert r["bars"]["implement"] == "Implement"
    assert r["bars"]["implement (started)"] == "Implement (started)"
    assert r["bars"]["review"] == "Review"
    assert r["bars"]["gate"] == "Gate"
    # Untouched: the children that inherit their stage's token, the event span
    # whose worker comes from a prefixed agent name alone, and the run root —
    # whose NAME holds a slash but which has no worker at all.
    assert r["bars"]["Bash"] == "Bash"
    assert r["bars"]["command_execution"] == "command_execution"
    assert r["bars"]["self-review"] == "self-review"
    assert r["bars"]["agent_response"] == "agent_response"
    assert r["bars"]["UncleSaurus/widget#7"] == "UncleSaurus/widget#7"

    # Display only: names, kinds and `kestrel.stage` are exactly as exported.
    assert r["contract"] == [
        {"name": "UncleSaurus/widget#7", "kind": "AGENT", "stage": None, "worker": None},
        {"name": "implement", "kind": "LLM", "stage": "implement", "worker": "implement"},
        {
            "name": "implement (started)",
            "kind": "LLM",
            "stage": "implement",
            "worker": "implement",
        },
        {"name": "Bash", "kind": "TOOL", "stage": "implement", "worker": "implement"},
        {
            "name": "command_execution",
            "kind": "TOOL",
            "stage": "implement",
            "worker": "implement",
        },
        {"name": "review", "kind": "LLM", "stage": "review", "worker": "review"},
        {"name": "agent_response", "kind": "TOOL", "stage": None, "worker": "review"},
        {"name": "gate", "kind": "CHAIN", "stage": "gate", "worker": "gate"},
        {"name": "self-review", "kind": "CHAIN", "stage": "gate", "worker": "gate"},
    ]

    # The gutter agrees with the bars, and keys off the raw token.
    assert r["gutter"] == [
        {"label": "talon", "level": 1, "worker": None},
        {"label": "talon/Gate", "level": 2, "worker": "gate"},
        {"label": "talon/Implement", "level": 2, "worker": "implement"},
        {"label": "talon/Review", "level": 2, "worker": "review"},
    ]


# ── Poll-walk resumption + coverage honesty (#109) ─────────────────────────
#
# A poll walk is capped at MAX_POLL_PAGES pages, so a backlog deeper than that
# does not drain in one tick. It used to be restarted every tick from a
# recomputed `startMs` — cursor thrown away, boundary advanced by +1 ms, and
# `watermarks`/`historyFloor` already claiming coverage the walk never ingested.
# The scenarios below mount the real view against a Phoenix double with real
# time-range and cursor semantics and hold the walk to its contract: resume, and
# claim only what you finished.

_TIMELINE_PRELUDE = r"""
import { FakeElement, installFakeDom } from "./fake-dom.mjs";
import {
  installFakePhoenix,
  rawSpan,
  settle,
  paintedText,
  lastFrame,
  forgetFrames,
  frameText,
} from "./fake-phoenix.mjs";

installFakeDom();
const MIN = 60 * 1000;
const T0 = Date.now();
const PROJECT = { id: "p1", name: "kestrel-fleet", traceCount: 1, endTime: new Date(T0).toISOString() };

// Claim the poll timer. The mount registers `() => pollTick(false)` on
// setInterval; these scenarios drive it by hand (`tick()`) so a tick is an
// explicit event with an exact call count, not an ambient 5s alarm that may or
// may not fire mid-scenario. A `tick()` is deliberately NOT a `[data-refresh]`
// click: the click polls with `manual: true`, which is precisely the flag a
// paused view's timer does not have.
let pollTimerFn = null;
globalThis.setInterval = (fn) => {
  pollTimerFn = fn;
  return 1;
};
globalThis.clearInterval = () => {
  pollTimerFn = null;
};
function tick() {
  if (!pollTimerFn) throw new Error("no poll timer registered");
  pollTimerFn();
}

// One long run root with a deep backlog of tool children beneath it, laid end to
// end so the lane packs into a couple of tracks.
function backlog({ agent, base, step, count, tailAgent = null, rootName = "backlog run" }) {
  const out = [
    rawSpan({
      id: "run-root",
      name: rootName,
      start: base - 1000,
      dur: 24 * MIN,
      agent,
      spanId: "run",
      kind: "AGENT",
      projectId: PROJECT.id,
    }),
  ];
  for (let i = 0; i < count; i++) {
    const last = i === count - 1;
    out.push(
      rawSpan({
        id: `tool-${String(i).padStart(5, "0")}`,
        name: `tool ${i}`,
        start: base + i * step,
        agent: last && tailAgent ? tailAgent : agent,
        spanId: `ts-${i}`,
        parentId: last && tailAgent ? null : "run",
        projectId: PROJECT.id,
      }),
    );
  }
  return out;
}

function callLog(calls) {
  return calls.map((c) => ({
    after: c.after,
    start: (c.timeRange && c.timeRange.start) || null,
    end: (c.timeRange && c.timeRange.end) || null,
    endCursor: c.endCursor,
    hasNext: c.hasNext,
    first: c.first,
  }));
}
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_truncated_poll_walk_resumes_from_its_cursor(tmp_path):
    """A backlog deeper than MAX_POLL_PAGES resumes; nothing is left behind."""
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-backlog.mjs",
        _TIMELINE_PRELUDE
        + r"""
const BASE = T0 - 25 * MIN;
const STEP = 400;
const COUNT = 3200; // 7 pages of 500 — one more than a single tick can drain
// The newest child gets its own lane, so its gutter label is proof the deepest
// page was not just fetched but ingested and painted.
const spans = backlog({ agent: "backlog", base: BASE, step: STEP, count: COUNT, tailAgent: "backlogtail" });
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");
const refresh = container.querySelector("[data-refresh]");

await settle(phoenix.calls);
const firstTick = phoenix.calls.length;
refresh.dispatch("click"); // second tick
await settle(phoenix.calls);
const secondTick = phoenix.calls.length;
refresh.dispatch("click"); // third tick — the backlog is drained by now
await settle(phoenix.calls);

const painted = paintedText(canvas);
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  firstTick,
  secondTick,
  total: spans.length,
  servedUnique: new Set(phoenix.served).size,
  missing: spans.map((s) => s.id).filter((id) => !phoenix.served.includes(id)).slice(0, 5),
  newestStart: new Date(BASE + (COUNT - 1) * STEP).toISOString(),
  laneLabels: [...new Set(painted.filter((t) => t.startsWith("backlog")))].sort(),
}));
""",
    )

    calls = result["calls"]
    # Tick 1 stops at the page cap, mid-backlog.
    assert result["firstTick"] == MAX_POLL_PAGES
    assert calls[0]["after"] is None
    assert all(c["first"] == PAGE_SIZE for c in calls)
    for i in range(1, MAX_POLL_PAGES):
        assert calls[i]["after"] == calls[i - 1]["endCursor"]
        assert calls[i]["start"] == calls[0]["start"]
    assert calls[MAX_POLL_PAGES - 1]["hasNext"] is True

    # Tick 2 RESUMES: the persisted cursor, on the bounds the walk started with.
    # Those bounds are also the proof that the watermark was NOT advanced by the
    # truncated walk — an advanced watermark is a different `timeRange.start`.
    resumed = calls[MAX_POLL_PAGES]
    assert resumed["after"] == calls[MAX_POLL_PAGES - 1]["endCursor"]
    assert resumed["start"] == calls[0]["start"]
    assert resumed["hasNext"] is False  # walk complete → coverage may commit
    assert result["secondTick"] == MAX_POLL_PAGES + 1

    # Only NOW does the watermark move — to the newest ingested start exactly,
    # not one millisecond past it.
    fresh = calls[MAX_POLL_PAGES + 1]
    assert fresh["after"] is None
    assert fresh["start"] == result["newestStart"]

    # Every span in the backlog was ingested, and the deepest page's span is on
    # screen in its own lane.
    assert result["missing"] == []
    assert result["servedUnique"] == result["total"]
    # ("backlog run" is the root's own bar label, in the "backlog" lane.)
    assert result["laneLabels"] == ["backlog", "backlog run", "backlogtail"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_millisecond_tie_across_the_page_bound_is_not_skipped(tmp_path):
    """A parent and child sharing one millisecond, split by the page cap."""
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-tie.mjs",
        _TIMELINE_PRELUDE
        + r"""
const BASE = T0 - 25 * MIN;
const STEP = 400;
const KIDS = 2998; // root + kids = 2999 spans, so the tie pair straddles page 6/7
const TIE = BASE + KIDS * STEP;
const spans = backlog({ agent: "tie", base: BASE, step: STEP, count: KIDS, rootName: "tie run" });
// Emitted in the same millisecond, on either side of the page bound: the CHILD
// is the last span of page 6, its PARENT the first of page 7. A walk that
// advanced past the boundary millisecond would lose the parent for good.
spans.push(rawSpan({
  id: "z-tie-child", name: "tie child", start: TIE, dur: 30,
  agent: "tie", spanId: "tiechild", parentId: "tieparent", projectId: PROJECT.id,
}));
spans.push(rawSpan({
  id: "zz-tie-parent", name: "tie parent band", start: TIE, dur: 5 * MIN,
  agent: "tie", spanId: "tieparent", parentId: "run", kind: "CHAIN", projectId: PROJECT.id,
}));
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");
const refresh = container.querySelector("[data-refresh]");

await settle(phoenix.calls);
const truncated = {
  calls: phoenix.calls.length,
  child: phoenix.served.includes("z-tie-child"),
  parent: phoenix.served.includes("zz-tie-parent"),
};
refresh.dispatch("click");
await settle(phoenix.calls);
const resumedCalls = phoenix.calls.length;

// A late arrival in the boundary millisecond itself: the watermark now sits on
// exactly this timestamp, and an exclusive (+1 ms) boundary would never ask for
// it again.
phoenix.add(rawSpan({
  id: "zzz-late-twin", name: "late twin", start: TIE, dur: 4 * MIN,
  agent: "latetwin", spanId: "latetwin", projectId: PROJECT.id,
}));
refresh.dispatch("click");
await settle(phoenix.calls);

const painted = paintedText(canvas);
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  truncated,
  resumedCalls,
  tie: new Date(TIE).toISOString(),
  served: {
    child: phoenix.served.includes("z-tie-child"),
    parent: phoenix.served.includes("zz-tie-parent"),
    lateTwin: phoenix.served.includes("zzz-late-twin"),
  },
  painted: [...new Set(painted)].filter((t) => t === "tie parent band" || t === "latetwin" || t === "tie"),
}));
""",
    )

    calls = result["calls"]
    # The cap really did split the tie pair.
    assert result["truncated"]["calls"] == MAX_POLL_PAGES
    assert result["truncated"]["child"] is True
    assert result["truncated"]["parent"] is False

    # Resumption picks the parent up on the very next page — neither half of the
    # pair is skipped, and the parent band paints.
    assert calls[MAX_POLL_PAGES]["after"] == calls[MAX_POLL_PAGES - 1]["endCursor"]
    assert calls[MAX_POLL_PAGES]["start"] == calls[0]["start"]
    assert result["resumedCalls"] == MAX_POLL_PAGES + 1
    assert result["served"]["parent"] is True
    assert "tie parent band" in result["painted"]

    # The committed watermark is the tie millisecond ITSELF: the next walk asks
    # from it inclusively, so a span landing in that same millisecond after the
    # walk finished is still pulled in.
    boundary = calls[MAX_POLL_PAGES + 1]
    assert boundary["after"] is None
    assert boundary["start"] == result["tie"]
    assert result["served"]["lateTwin"] is True
    assert "latetwin" in result["painted"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_truncated_history_walk_does_not_claim_the_gap(tmp_path):
    """historyFloor is a claim only a finished walk gets to make."""
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-history.mjs",
        _TIMELINE_PRELUDE
        + r"""
const GAP_BASE = T0 - 58 * MIN;
const STEP = 400;
const COUNT = 3200; // 7 pages once the run root is counted
const spans = backlog({ agent: "old", base: GAP_BASE, step: STEP, count: COUNT, rootName: "old run" });
// Live-window spans, so the boot poll finishes and commits a real floor for the
// history walk to page back from.
for (let i = 0; i < 3; i++) {
  spans.push(rawSpan({
    id: `recent-${i}`, name: `recent ${i}`, start: T0 - (20 - i * 4) * MIN,
    agent: "recent", spanId: `rs-${i}`, projectId: PROJECT.id,
  }));
}
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });

await settle(phoenix.calls);
const liveCalls = phoenix.calls.length;

// Pan back a window: a gap far deeper than one walk can drain.
const paused = {
  windowMs: 30 * MIN,
  viewEnd: T0 - 30 * MIN,
  live: false,
  laneScrollY: 0,
  collapsed: [],
  highlightedSpanId: null,
};
mounted.setState(paused);
await settle(phoenix.calls);
const afterTruncated = phoenix.calls.length;

mounted.setState(paused); // same window again — must RESUME, not skip as covered
await settle(phoenix.calls);
const afterResumed = phoenix.calls.length;

mounted.setState(paused); // and now the gap really is covered
await settle(phoenix.calls);
const afterCovered = phoenix.calls.length;

mounted.destroy();
const gapIds = spans.filter((s) => Date.parse(s.startTime) < T0 - 30 * MIN).map((s) => s.id);
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  liveCalls,
  afterTruncated,
  afterResumed,
  afterCovered,
  target: new Date(T0 - 60 * MIN).toISOString(),
  gapMissing: gapIds.filter((id) => !phoenix.served.includes(id)).slice(0, 5),
}));
""",
    )

    calls = result["calls"]
    # Boot: one live page over the visible window, which completes and commits.
    assert result["liveCalls"] == 1
    gap = calls[1:]
    # The gap walk stops at the page cap with the range still owed.
    assert result["afterTruncated"] - result["liveCalls"] == MAX_POLL_PAGES
    assert all(c["start"] == result["target"] for c in gap[:MAX_POLL_PAGES])
    assert all(c["end"] == gap[0]["end"] for c in gap[:MAX_POLL_PAGES])
    assert gap[MAX_POLL_PAGES - 1]["hasNext"] is True

    # Panning to the same window again re-enters the SAME walk on its cursor. A
    # `historyFloor` advanced by the truncated walk would have skipped the
    # project outright ("already covered") and issued nothing at all.
    assert result["afterResumed"] == result["afterTruncated"] + 1
    resumed = gap[MAX_POLL_PAGES]
    assert resumed["after"] == gap[MAX_POLL_PAGES - 1]["endCursor"]
    assert resumed["start"] == result["target"]
    assert resumed["end"] == gap[0]["end"]
    assert resumed["hasNext"] is False

    # Once the walk finished, the gap IS covered — and stops being refetched.
    assert result["afterCovered"] == result["afterResumed"]
    assert result["gapMissing"] == []


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_failed_page_leaves_the_walk_cursor_intact(tmp_path):
    """An error mid-walk is resumed on the next tick, not restarted."""
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-error.mjs",
        _TIMELINE_PRELUDE
        + r"""
const BASE = T0 - 25 * MIN;
const STEP = 400;
const COUNT = 3200;
const spans = backlog({ agent: "flaky", base: BASE, step: STEP, count: COUNT });
// The third page of the first walk fails, two pages in.
const phoenix = installFakePhoenix({ projects: [PROJECT], spans, failCalls: [2] });

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const refresh = container.querySelector("[data-refresh]");

await settle(phoenix.calls);
const afterFailure = phoenix.calls.length;
refresh.dispatch("click");
await settle(phoenix.calls);
const afterRetry = phoenix.calls.length;
refresh.dispatch("click");
await settle(phoenix.calls);

mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  afterFailure,
  afterRetry,
  total: spans.length,
  missing: spans.map((s) => s.id).filter((id) => !phoenix.served.includes(id)).slice(0, 5),
}));
""",
    )

    calls = result["calls"]
    # Two good pages, then the throw aborts the walk mid-flight.
    assert result["afterFailure"] == 3
    assert calls[2]["after"] == calls[1]["endCursor"]
    assert calls[2]["endCursor"] is None  # never answered

    # The next tick picks up exactly where the last GOOD page ended, on the same
    # bounds — it neither restarts at the top nor recomputes a moved boundary.
    retry = calls[3]
    assert retry["after"] == calls[1]["endCursor"]
    assert retry["start"] == calls[0]["start"]
    assert result["afterRetry"] > result["afterFailure"]
    assert result["missing"] == []


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_paused_view_resumes_its_truncated_live_walk(tmp_path):
    """A restored-PAUSED window still finishes the backlog its boot poll started.

    The initial fill is always a manual poll, so a window restored paused (#86)
    can end up owing a live walk it never asked for. The poll timer is the only
    thing that comes back — and while paused it starts no fresh walk, which is
    exactly why it must still resume the one already owed. Otherwise the pages
    past the cap wait for the operator to hit Refresh.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-paused-resume.mjs",
        _TIMELINE_PRELUDE
        + r"""
const STEP = 400;
const COUNT = 3200; // 7 pages once the run root is counted
const BASE = T0 - 89 * MIN;
const ROOT_START = BASE - 1000;
const TAIL = `tool-${String(COUNT - 1).padStart(5, "0")}`;
const spans = backlog({
  agent: "restored", base: BASE, step: STEP, count: COUNT,
  tailAgent: "restoredtail", rootName: "restored run",
});
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");

// The console restores the persisted view right after mount — BEFORE boot's
// initial poll — so that poll fills a window which is already paused. The window
// opens exactly on the run root, so a finished walk's floor covers its left edge
// and the view is left owing nothing at all.
mounted.setState({
  windowMs: 30 * MIN,
  viewEnd: ROOT_START + 30 * MIN,
  live: false,
  laneScrollY: 0,
  collapsed: [],
  highlightedSpanId: null,
});

await settle(phoenix.calls);
const boot = {
  calls: phoenix.calls.length,
  live: mounted.getState().live,
  tail: phoenix.served.includes(TAIL),
};

tick(); // the poll TIMER, paused: no fresh walk — but the owed one resumes
await settle(phoenix.calls);
const afterTick = phoenix.calls.length;

tick(); // and with nothing owed, a paused tick asks for nothing
await settle(phoenix.calls);
const afterIdleTick = phoenix.calls.length;

const painted = paintedText(canvas);
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  boot,
  afterTick,
  afterIdleTick,
  total: spans.length,
  servedUnique: new Set(phoenix.served).size,
  missing: spans.map((s) => s.id).filter((id) => !phoenix.served.includes(id)).slice(0, 5),
  laneLabels: [...new Set(painted.filter((t) => t.startsWith("restored")))].sort(),
}));
""",
    )

    calls = result["calls"]
    # Boot polls the restored window manually and is cut off by the page cap.
    assert result["boot"]["live"] is False
    assert result["boot"]["calls"] == MAX_POLL_PAGES
    assert calls[MAX_POLL_PAGES - 1]["hasNext"] is True
    assert result["boot"]["tail"] is False  # the deepest page was never reached
    assert all(c["end"] is None for c in calls[:MAX_POLL_PAGES])  # a live walk

    # The TIMER resumes it — on the persisted cursor and the bounds the walk
    # started with — even though the view is paused and no manual refresh, pan or
    # Live toggle ever happened.
    assert result["afterTick"] == MAX_POLL_PAGES + 1
    resumed = calls[MAX_POLL_PAGES]
    assert resumed["after"] == calls[MAX_POLL_PAGES - 1]["endCursor"]
    assert resumed["start"] == calls[0]["start"]
    assert resumed["end"] is None
    assert resumed["hasNext"] is False

    # Resuming is all it does: with the walk finished and its coverage committed,
    # the next paused tick starts no fresh live walk.
    assert result["afterIdleTick"] == result["afterTick"]

    # And the backlog is whole — including the last page, painted in its own lane.
    assert result["missing"] == []
    assert result["servedUnique"] == result["total"]
    assert result["laneLabels"] == ["restored", "restored run", "restoredtail"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_pan_during_an_in_flight_history_walk_still_loads_the_new_gap(tmp_path):
    """A pan that lands mid-fetch is serviced when that walk lands, not dropped.

    A walk's bounds are fixed for the life of its cursor, so it cannot widen to
    cover a viewport that moved under it — and the in-flight guard drops the
    concurrent call. The walk therefore has to re-derive the gap from the CURRENT
    view start when it finishes, or the newly exposed range waits for the next
    gesture that happens not to collide with a fetch.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-pan-midflight.mjs",
        _TIMELINE_PRELUDE
        + r"""
function band(prefix, agent, offsets) {
  return offsets.map((mins, i) => rawSpan({
    id: `${prefix}-${i}`, name: `${prefix} ${i}`, start: T0 - mins * MIN,
    agent, spanId: `${prefix}s-${i}`, projectId: PROJECT.id,
  }));
}
// Three bands: the live window, the gap one pan back, the gap two pans back.
const recent = band("recent", "recent", [20, 16, 12]);
const gapA = band("gapa", "gapa", [48, 44, 40]);
const gapB = band("gapb", "gapb", [78, 74, 70]);
// Park the FIRST page of the gap-A walk in flight.
const phoenix = installFakePhoenix({
  projects: [PROJECT],
  spans: [...recent, ...gapA, ...gapB],
  holdCalls: [1],
});

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");

await settle(phoenix.calls);
const bootCalls = phoenix.calls.length; // one live page, which finishes + commits

const windowA = {
  windowMs: 30 * MIN, viewEnd: T0 - 20 * MIN, live: false,
  laneScrollY: 0, collapsed: [], highlightedSpanId: null,
};
mounted.setState(windowA); // pan back one window → the gap-A walk, held mid-page
await settle(phoenix.calls);
const inFlight = { calls: phoenix.calls.length, held: phoenix.held() };

// Pan FARTHER left while that page is still in flight.
mounted.setState({ ...windowA, viewEnd: T0 - 50 * MIN });
await settle(phoenix.calls);
const suppressed = phoenix.calls.length;

phoenix.release(1); // the walk lands — no gesture and no tick after this point
await settle(phoenix.calls);
const afterRelease = phoenix.calls.length;

tick(); // and once it IS covered, a paused tick re-walks nothing
await settle(phoenix.calls);

const painted = paintedText(canvas);
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  bootCalls,
  inFlight,
  suppressed,
  afterRelease,
  aStart: new Date(T0 - 50 * MIN).toISOString(),
  aEnd: new Date(T0 - 20 * MIN).toISOString(),
  bStart: new Date(T0 - 80 * MIN).toISOString(),
  missingA: gapA.map((s) => s.id).filter((id) => !phoenix.served.includes(id)),
  missingB: gapB.map((s) => s.id).filter((id) => !phoenix.served.includes(id)),
  laneLabels: [...new Set(painted)].filter((t) => t === "gapa" || t === "gapb"),
}));
""",
    )

    calls = result["calls"]
    assert result["bootCalls"] == 1
    # The gap-A walk is issued and parked mid-page.
    assert result["inFlight"] == {"calls": 2, "held": [1]}
    assert calls[1]["start"] == result["aStart"]
    assert calls[1]["end"] == result["aEnd"]

    # Panning farther left while it is in flight issues nothing: the walk owns
    # the project and its bounds can no longer move.
    assert result["suppressed"] == 2

    # When the walk lands it re-derives the gap from where the view now is, and
    # loads it — no further pan, click or tick.
    assert result["afterRelease"] == 3
    assert len(calls) == 3  # the tick that followed added nothing: the gap is covered
    newly_exposed = calls[2]
    assert newly_exposed["after"] is None
    assert newly_exposed["start"] == result["bStart"]
    assert newly_exposed["end"] == result["aStart"]  # bounded by the committed floor
    assert result["missingA"] == []
    assert result["missingB"] == []
    assert "gapb" in result["laneLabels"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_history_rounds_are_bounded_and_the_paused_timer_finishes_them(tmp_path):
    """Re-deriving the gap is capped per pass; the paused timer picks up the rest.

    A pass re-checks the viewport after each walk, so a viewport that keeps
    moving during each fetch could otherwise chase a drag forever inside one
    call. The cap makes that bounded — which is only honest if the range the cap
    left behind is still owed by something, hence the paused timer's own check.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-history-rounds.mjs",
        _TIMELINE_PRELUDE
        + r"""
const MAX_HISTORY_ROUNDS = 4; // timeline.js tuning this scenario drives
function band(prefix, offsets) {
  return offsets.map((mins, i) => rawSpan({
    id: `${prefix}-${i}`, name: `${prefix} ${i}`, start: T0 - mins * MIN,
    agent: prefix, spanId: `${prefix}s-${i}`, projectId: PROJECT.id,
  }));
}
// One band per 30-minute window, walking back: the live one, then five gaps.
const bands = {
  recent: band("recent", [20, 16, 12]),
  gapa: band("gapa", [48, 44, 40]),
  gapb: band("gapb", [78, 74, 70]),
  gapc: band("gapc", [108, 104, 100]),
  gapd: band("gapd", [138, 134, 130]),
  gape: band("gape", [168, 164, 160]),
};
// Every gap page is parked, so the viewport can move during each one.
const phoenix = installFakePhoenix({
  projects: [PROJECT],
  spans: Object.values(bands).flat(),
  holdCalls: [1, 2, 3, 4, 5],
});

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });

await settle(phoenix.calls);
const bootCalls = phoenix.calls.length;

const panTo = async (minutesAgo) => {
  mounted.setState({
    windowMs: 30 * MIN, viewEnd: T0 - minutesAgo * MIN, live: false,
    laneScrollY: 0, collapsed: [], highlightedSpanId: null,
  });
  await settle(phoenix.calls);
};

// Pan one window back, then keep panning while each page is still in flight —
// every one of those calls is dropped by the in-flight guard, and every release
// makes the pass re-derive the gap and start ANOTHER round.
await panTo(20);
const rounds = [];
for (let held = 1; held <= MAX_HISTORY_ROUNDS; held++) {
  rounds.push({ calls: phoenix.calls.length, held: phoenix.held() });
  await panTo(20 + held * 30); // moves the viewport while page `held` is parked
  phoenix.release(held);
  await settle(phoenix.calls);
}
// The last pan landed during the last round the cap allows, so the pass ends
// here — no walk pending, no gesture coming, and the exposed gap unfetched.
const afterCap = {
  calls: phoenix.calls.length,
  held: phoenix.held(),
  gape: phoenix.served.includes("gape-0"),
};

tick(); // the paused timer: the visible left edge is still not covered
await settle(phoenix.calls);
const afterTick = { calls: phoenix.calls.length, held: phoenix.held() };
if (phoenix.held().includes(5)) {
  phoenix.release(5);
  await settle(phoenix.calls);
}
const afterFinish = phoenix.calls.length;

tick(); // covered at last — and quiet
await settle(phoenix.calls);

mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  bootCalls,
  rounds,
  afterCap,
  afterTick,
  afterFinish,
  idle: phoenix.calls.length,
  eStart: new Date(T0 - 170 * MIN).toISOString(),
  eEnd: new Date(T0 - 140 * MIN).toISOString(),
  missing: Object.values(bands).flat().map((s) => s.id)
    .filter((id) => !phoenix.served.includes(id)),
}));
""",
    )

    # One pass, one project: a walk per round, each one issued only because the
    # viewport moved while the previous page was in flight.
    assert result["bootCalls"] == 1
    assert len(result["rounds"]) == MAX_HISTORY_ROUNDS
    assert [r["calls"] for r in result["rounds"]] == [2, 3, 4, 5]
    assert [r["held"] for r in result["rounds"]] == [[1], [2], [3], [4]]
    # One round past the cap never starts — the pass stops with the gap the last
    # pan exposed still unfetched, and nothing pending to finish it.
    assert result["afterCap"] == {"calls": 5, "held": [], "gape": False}

    # The paused timer is what owes it: a finished walk's floor stops short of the
    # window on screen, and a paused view never drifts off it.
    assert result["afterTick"] == {"calls": 6, "held": [5]}
    gap_e = result["calls"][5]
    assert gap_e["start"] == result["eStart"]
    assert gap_e["end"] == result["eEnd"]
    assert result["afterFinish"] == 6
    assert result["missing"] == []
    # Covered now — the next tick asks for nothing.
    assert result["idle"] == 6


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_reveal_walk_past_the_page_cap_resumes_instead_of_reporting_missing(tmp_path):
    """A reveal target on page MAX_POLL_PAGES + 1 is found, not written off.

    A reveal opens the view PAUSED around its target, so nothing but the reveal
    walk itself will ever look for that span. Truncated by the page cap and
    dropped, the view reports a span that is right there on the next page as
    "could not be loaded" — permanently, since no pan, tick or Live toggle
    resumes a walk whose cursor was thrown away.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "poll-reveal-resume.mjs",
        _TIMELINE_PRELUDE
        + r"""
// A reveal centers a MIN_WINDOW_MS (60s) window on its target, so the whole
// backlog has to sit inside that one minute, ahead of the target.
const TARGET = T0 - 40 * MIN;
const AHEAD = 3000; // exactly MAX_POLL_PAGES pages of 500 before the target
const spans = [];
for (let i = 0; i < AHEAD; i++) {
  spans.push(rawSpan({
    id: `ahead-${String(i).padStart(5, "0")}`, name: `ahead ${i}`,
    start: TARGET - 29_500 + i * 9, dur: 5,
    agent: "revealbulk", spanId: `as-${i}`, projectId: PROJECT.id,
  }));
}
// Sorts last of the window — page MAX_POLL_PAGES + 1, all on its own.
spans.push(rawSpan({
  id: "zz-target", name: "the revealed tool", start: TARGET, dur: 8 * 1000,
  agent: "revealtarget", spanId: "wanted", projectId: PROJECT.id,
}));
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });

const highlights = (canvas) =>
  (canvas.context.frames || []).filter((frame) =>
    frame.operations.some((op) => op.type === "strokeRect" && op.strokeStyle === "#facc15"),
  ).length;

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, {
  openTrace() {}, openNavigator() {},
  revealTarget: {
    projectId: PROJECT.id,
    projectName: PROJECT.name,
    spanId: "wanted",
    startTime: TARGET,
  },
});
const canvas = container.querySelector("[data-canvas]");
const notice = container.querySelector("[data-reveal-notice]");

await settle(phoenix.calls);
const boot = {
  calls: phoenix.calls.length,
  live: mounted.getState().live,
  target: phoenix.served.includes("zz-target"),
  // Nothing is claimed either way while the walk is still owed: no "highlighted",
  // and — the actual bug — no "could not be loaded" about a span on the next page.
  notice: notice.textContent,
  fallback: notice.classList.values.has("obs-tl__reveal--fallback"),
  highlightFrames: highlights(canvas),
};

tick(); // the poll timer, paused: the reveal walk is owed, so it resumes
await settle(phoenix.calls);
const afterTick = {
  calls: phoenix.calls.length,
  target: phoenix.served.includes("zz-target"),
  notice: notice.textContent,
  fallback: notice.classList.values.has("obs-tl__reveal--fallback"),
  highlightFrames: highlights(canvas),
};

const painted = paintedText(canvas);
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls: callLog(phoenix.calls),
  boot,
  afterTick,
  windowStart: new Date(TARGET - 30 * 1000).toISOString(),
  windowEnd: new Date(TARGET + 30 * 1000).toISOString(),
  paintedTarget: painted.includes("the revealed tool"),
}));
""",
    )

    calls = result["calls"]
    # The reveal walk is bounded to its centered window and cut off by the cap.
    assert result["boot"]["live"] is False
    assert result["boot"]["calls"] == MAX_POLL_PAGES
    assert calls[0]["start"] == result["windowStart"]
    assert calls[0]["end"] == result["windowEnd"]
    assert all(c["end"] == calls[0]["end"] for c in calls[:MAX_POLL_PAGES])
    assert calls[MAX_POLL_PAGES - 1]["hasNext"] is True
    assert result["boot"]["target"] is False

    # An owed reveal reports NOTHING yet — the old behaviour called the span
    # missing here, on evidence it had not finished gathering.
    assert result["boot"]["notice"] == ""
    assert result["boot"]["fallback"] is False
    assert result["boot"]["highlightFrames"] == 0

    # The timer resumes it on its persisted cursor and the bounds it started
    # with, finds the target on the very next page, and only THEN reports.
    assert result["afterTick"]["calls"] == MAX_POLL_PAGES + 1
    resumed = calls[MAX_POLL_PAGES]
    assert resumed["after"] == calls[MAX_POLL_PAGES - 1]["endCursor"]
    assert resumed["start"] == result["windowStart"]
    assert resumed["end"] == result["windowEnd"]
    assert resumed["hasNext"] is False
    assert result["afterTick"]["target"] is True
    assert result["afterTick"]["notice"] == "Exact span wanted highlighted."
    assert result["afterTick"]["fallback"] is False
    # And the late page repaints: the highlight is on screen and the span drawn.
    assert result["afterTick"]["highlightFrames"] > 0
    assert result["paintedTarget"] is True


_WALK_BOUNDS_HARNESS = r"""
import { pollWalkBounds } from "./timeline.js";

const out = {};
// First pass for a project: the visible window's start, nothing to resume.
out.firstPass = pollWalkBounds({ viewStart: 1_000 });
// A watermark is walked from INCLUSIVELY — `+1` would skip its millisecond.
out.watermark = pollWalkBounds({ watermark: 5_000, viewStart: 1_000 });
// The still-open floor (#62 P1) still backs the boundary down when it is older.
out.openFloor = pollWalkBounds({ watermark: 5_000, openFloor: 4_000, viewStart: 1_000 });
out.laterFloor = pollWalkBounds({ watermark: 5_000, openFloor: 9_000, viewStart: 1_000 });
// An unfinished walk outranks all of it: same bounds, same cursor.
out.resumed = pollWalkBounds({
  pending: { startMs: 2_000, endMs: 3_000, after: "cursor-7" },
  watermark: 5_000,
  openFloor: 4_000,
  viewStart: 1_000,
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_poll_walk_bounds_are_resumable_and_tie_safe(tmp_path):
    """The pure boundary rules behind the live walk (#109)."""
    pkg = _module_dir(tmp_path)
    result = _run_scenario(pkg, "walk-bounds.mjs", _WALK_BOUNDS_HARNESS)

    assert result["firstPass"] == {
        "startMs": 1_000,
        "endMs": None,
        "after": None,
        "resumed": False,
    }
    # Inclusive: the boundary millisecond is re-pulled (merges are idempotent)
    # rather than skipped.
    assert result["watermark"]["startMs"] == 5_000
    assert result["openFloor"]["startMs"] == 4_000
    assert result["laterFloor"]["startMs"] == 5_000
    assert result["resumed"] == {
        "startMs": 2_000,
        "endMs": 3_000,
        "after": "cursor-7",
        "resumed": True,
    }


# ── Cap eviction: whole closed sessions, never individual spans (#111) ────────
#
# `pruneSpans` used to sort the store ascending by the RAW span end and drop that
# prefix. `normalize()` degrades an open-ended span to `end = start`, so a live
# talon run root or an in-flight turn sorted as the OLDEST thing in the store and
# was evicted BEFORE its own finished children; and individual eviction shredded
# the units the render model folds — a turn root separated from its "turn <n>
# summary" re-annotates as still running, a "(started)" marker separated from its
# twin repaints as a phantom open band.
#
# The unit is now the SESSION (`sessionKeyFor`: the stamped session id, else the
# trace). The fixtures below are the real cap — 60k spans, mounted view, real
# paging. `SPAN_CAP` is production tuning and gets no test seam, so the store is
# proved through what the canvas paints: a lane's gutter label is painted iff the
# store still holds a span for that agent, a band's label iff that span survived,
# and the cyan right-edge cap iff a band is rendering as still-running.

SPAN_CAP = 60_000

_EVICTION_PRELUDE = r"""
const hex = (n) => n.toString(16).padStart(8, "0");

// The cyan cap timeline.js paints at the right edge of an OPEN bar, and only
// there — the one unambiguous "this band is rendering as still-running" signal.
const OPEN_EDGE_COLOR = "#22d3ee";
const openEdges = (ops) =>
  ops.filter((o) => o.type === "fillRect" && o.fillStyle === OPEN_EDGE_COLOR).length;

// Drive ticks until the project's walk runs out of pages, keeping only the
// CURRENT tick's canvas frames (a cap-sized store paints tens of thousands of
// operations per frame, and every tick repaints). Eviction happens at the end of
// the tick whose walk DRAINED, so that tick's snapshot is the post-eviction
// paint. Returns one snapshot per tick: pages fetched, whether the walk drained,
// and which watched lanes its final paint still shows.
async function drainTicks(phoenix, canvas, refresh, watched, maxTicks = 45) {
  const snapshot = () => {
    const seen = new Set(frameText(lastFrame(canvas)));
    const lanes = {};
    for (const name of watched) lanes[name] = seen.has(name);
    return lanes;
  };
  const ticks = [];
  for (let i = 0; i < maxTicks; i++) {
    const before = phoenix.calls.length;
    forgetFrames(canvas);
    refresh.dispatch("click");
    await settle(phoenix.calls);
    const last = phoenix.calls[phoenix.calls.length - 1];
    const drained = Boolean(last && last.hasNext === false);
    ticks.push({ pages: phoenix.calls.length - before, drained, lanes: snapshot() });
    if (drained) break;
  }
  return ticks;
}

// Filler spans: one closed session of leaves laid end to end, newer than
// everything the fixture cares about, so it is never the eviction candidate.
function filler(count, { session, agent, from, step = 8 }) {
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push(rawSpan({
      id: `zz-${hex(i)}`, name: `fill ${i}`, start: from + i * step, dur: 4,
      agent, spanId: `fl${hex(i)}`, session, traceId: session, projectId: PROJECT.id,
    }));
  }
  return out;
}
"""


_LIVE_VS_CLOSED_FIXTURE = r"""
// 60_100 spans = the cap + 100, in three sessions:
//
//   - a LIVE session (`S-live`): a still-running root — Phoenix reports a null
//     endTime, so `normalize()` gives it `end = start`, and its start is the
//     OLDEST in the store. Under the old order that made it the first thing
//     evicted, ahead of its own finished children.
//   - a CLOSED session (`S-victim`): 100 leaves, and the oldest session END in
//     the store — the one candidate, and exactly the overage.
//   - the filler (`S-fill`): closed, newer, and never a candidate here.
const spans = [
  rawSpan({ id: "a0-live-root", name: "live run root", start: T0 - 19 * MIN, open: true,
            agent: "livework", spanId: "liveroot", kind: "AGENT",
            session: "S-live", traceId: "S-live", projectId: PROJECT.id }),
  rawSpan({ id: "a1-live-a", name: "live tool a", start: T0 - 18 * MIN, dur: 4 * MIN,
            agent: "livework", spanId: "livea", parentId: "liveroot",
            session: "S-live", traceId: "S-live", projectId: PROJECT.id }),
  rawSpan({ id: "a2-live-b", name: "live tool b", start: T0 - 12 * MIN, dur: 4 * MIN,
            agent: "livework", spanId: "liveb", parentId: "liveroot",
            session: "S-live", traceId: "S-live", projectId: PROJECT.id }),
  rawSpan({ id: "a3-live-c", name: "live tool c", start: T0 - 6 * MIN, dur: 4 * MIN,
            agent: "livework", spanId: "livec", parentId: "liveroot",
            session: "S-live", traceId: "S-live", projectId: PROJECT.id }),
];
for (let i = 0; i < 100; i++) {
  spans.push(rawSpan({
    id: `b-${hex(i)}`, name: `victim ${i}`, start: T0 - 17 * MIN + i * 500, dur: 4,
    agent: "victim", spanId: `vc${hex(i)}`, session: "S-victim", traceId: "S-victim",
    projectId: PROJECT.id,
  }));
}
spans.push(...filler(59_996, { session: "S-fill", agent: "filler", from: T0 - 15 * MIN }));
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_cap_evicts_the_closed_session_whole_and_keeps_the_live_one(tmp_path):
    """At the cap: the open session survives, the closed one goes whole — twice.

    Three claims off one cap-sized fixture:

    - the LIVE session is not a candidate at all. Its root is open (null
      ``endTime`` → ``end == start``, the oldest sort key in the store), which is
      exactly what the old order evicted first, ahead of its own finished
      children; here it and all three children survive, still rendering open.
    - the closed session is evicted WHOLE — its lane goes dark, all 100 spans.
    - and the policy keeps no memory of that. The next cycle's walk backs down to
      the live root's start (the #62 re-fetch floor), so the evicted session is
      served again: it merges back as an ordinary session mid-walk — no
      tombstone, no suppression table — and is evicted again when that cycle
      completes. The cap binds every cycle; retention grows nothing.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "cap-live-vs-closed.mjs",
        _TIMELINE_PRELUDE
        + _EVICTION_PRELUDE
        + _LIVE_VS_CLOSED_FIXTURE
        + r"""
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });
const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");
const refresh = container.querySelector("[data-refresh]");
const LANES = ["livework", "victim", "filler"];

await settle(phoenix.calls); // boot pass
const first = await drainTicks(phoenix, canvas, refresh, LANES);
const firstOps = lastFrame(canvas);
const firstText = frameText(firstOps);
// The next cycle starts a FRESH walk, backed down to the live root's start, so
// the evicted session is served all over again.
const second = await drainTicks(phoenix, canvas, refresh, LANES);

mounted.destroy();
const BARS = ["live run root", "live tool a", "live tool b", "live tool c"];
process.stdout.write(JSON.stringify({
  total: spans.length,
  firstDrained: first[first.length - 1].drained,
  firstPages: first.map((t) => t.pages),
  settled: first[first.length - 1].lanes,
  midWalk: first.length >= 2 ? first[first.length - 2].lanes : null,
  bars: [...new Set(firstText)].filter((t) => BARS.includes(t)).sort(),
  openEdges: openEdges(firstOps),
  abandoned: firstText.some((t) => t.startsWith("⚠")),
  reFetched: second[0].lanes,
  reSettled: second[second.length - 1].lanes,
  secondDrained: second[second.length - 1].drained,
}));
""",
    )

    assert result["total"] == SPAN_CAP + 100

    # The walk really was paged over the cap and really did finish: only a
    # drained walk is allowed to evict anything.
    assert result["firstDrained"] is True
    assert result["firstPages"][0] == MAX_POLL_PAGES

    # Mid-walk — the store already past the cap, one page still owed — nothing
    # has been evicted: a session's closing summary can be on the page still to
    # come, so eviction is not a per-page decision.
    assert result["midWalk"]["victim"] is True

    # Drained → the closed session is gone WHOLE, and only it.
    assert result["settled"]["victim"] is False
    assert result["settled"]["livework"] is True
    assert result["settled"]["filler"] is True

    # The open root — `end == start`, the very first thing the old sort dropped —
    # survives with every child, and still renders as running (the cyan cap at
    # the live edge, no "⚠" abandonment).
    assert result["bars"] == ["live run root", "live tool a", "live tool b", "live tool c"]
    assert result["openEdges"] >= 1
    assert result["abandoned"] is False

    # No memory: re-served, the evicted session merges back as an ordinary
    # session — and is evicted again when that cycle completes, so the cap binds
    # every cycle rather than being spent once.
    assert result["reFetched"]["victim"] is True
    assert result["secondDrained"] is True
    assert result["reSettled"]["victim"] is False
    assert result["reSettled"]["livework"] is True
    assert result["reSettled"]["filler"] is True


_UNIT_FIXTURE = r"""
// 61_000 spans = the cap + 1_000, in three sessions. The overage is HALF the
// oldest session, so a policy that evicted by span count would stop in the
// middle of it — and the 1_000 oldest RAW ends are not that session's leaves
// but the KEPT session's instants (a turn root, a session marker, a refused
// tool and its marker all sit at their start), which is precisely the shredding.
const kept = [
  // The session marker root: an instant, and the oldest end in the whole store.
  rawSpan({ id: "a0-session-root", name: "claude-code", start: T0 - 19 * MIN, dur: 0,
            agent: "kept", spanId: "sessionroot", kind: "AGENT",
            session: "S-kept", traceId: "S-kept", projectId: PROJECT.id }),
  // Turn 1: a PARENTLESS instant turn root closed only by its summary CHILD.
  rawSpan({ id: "a1-turn1", name: "claude-code turn 1", start: T0 - 19 * MIN + 1000, dur: 0,
            agent: "kept", spanId: "turnroot", kind: "AGENT", marker: "start",
            session: "S-kept", traceId: "S-kept", kestrel: { turn_index: 1 },
            projectId: PROJECT.id }),
  rawSpan({ id: "a2-turn1-summary", name: "turn 1 summary", start: T0 - 19 * MIN + 1000,
            dur: 10 * MIN, agent: "kept", spanId: "turnsum", parentId: "turnroot",
            kind: "CHAIN", session: "S-kept", traceId: "S-kept",
            kestrel: { turn_index: 1, tool_count: 2, duration_ms: 600000 },
            projectId: PROJECT.id }),
  // A classifier-refused Bash (#84): the terminal span is zero-duration at the
  // start its marker recorded, so the two tie on end AND on start — page order
  // alone decided which a span-level eviction took first, and taking the twin
  // leaves the marker to repaint as a phantom open band.
  rawSpan({ id: "a3-bash-real", name: "Bash", start: T0 - 18 * MIN, dur: 0,
            agent: "kept", spanId: "bashreal", parentId: "turnroot",
            session: "S-kept", traceId: "S-kept", kestrel: { tool_outcome: "denied" },
            projectId: PROJECT.id }),
  rawSpan({ id: "a4-bash-mark", name: "Bash (started)", start: T0 - 18 * MIN, dur: 0,
            agent: "kept", spanId: "bashmark", parentId: "turnroot", marker: "start",
            session: "S-kept", traceId: "S-kept", projectId: PROJECT.id }),
  // Turn 2: also parentless, and its closing summary is parented to the SESSION
  // MARKER — related to the turn it closes by nothing but the session key.
  rawSpan({ id: "a5-turn2", name: "claude-code turn 2", start: T0 - 8 * MIN, dur: 0,
            agent: "kept", spanId: "turn2root", kind: "AGENT", marker: "start",
            session: "S-kept", traceId: "S-kept", kestrel: { turn_index: 2 },
            projectId: PROJECT.id }),
  rawSpan({ id: "a6-turn2-summary", name: "turn 2 summary", start: T0 - 8 * MIN, dur: 3 * MIN,
            agent: "kept", spanId: "turn2sum", parentId: "sessionroot", kind: "CHAIN",
            session: "S-kept", traceId: "S-kept",
            kestrel: { turn_index: 2, tool_count: 1, duration_ms: 180000 },
            projectId: PROJECT.id }),
  rawSpan({ id: "a7-session-summary", name: "session summary", start: T0 - 19 * MIN,
            dur: 15 * MIN, agent: "kept", spanId: "sesssum", parentId: "sessionroot",
            kind: "CHAIN", session: "S-kept", traceId: "S-kept",
            kestrel: { turn_count: 2, tool_count: 3, duration_ms: 900000 },
            projectId: PROJECT.id }),
];
// The victim: the oldest session END in the store, and the same shape at its
// head — a parentless turn root whose closing summary hangs off the session
// marker. It goes whole, that head included.
const victim = [
  rawSpan({ id: "b0-session-root", name: "victim-agent", start: T0 - 17 * MIN, dur: 0,
            agent: "victim", spanId: "vroot", kind: "AGENT",
            session: "S-victim", traceId: "S-victim", projectId: PROJECT.id }),
  rawSpan({ id: "b1-turn", name: "victim turn 1", start: T0 - 17 * MIN, dur: 0,
            agent: "victim", spanId: "vturn", kind: "AGENT", marker: "start",
            session: "S-victim", traceId: "S-victim", kestrel: { turn_index: 1 },
            projectId: PROJECT.id }),
  rawSpan({ id: "b2-turn-summary", name: "turn 1 summary", start: T0 - 17 * MIN, dur: 1 * MIN,
            agent: "victim", spanId: "vturnsum", parentId: "vroot", kind: "CHAIN",
            session: "S-victim", traceId: "S-victim",
            kestrel: { turn_index: 1, tool_count: 1, duration_ms: 60000 },
            projectId: PROJECT.id }),
  rawSpan({ id: "b3-session-summary", name: "session summary", start: T0 - 17 * MIN,
            dur: 9.5 * MIN, agent: "victim", spanId: "vsesssum", parentId: "vroot",
            kind: "CHAIN", session: "S-victim", traceId: "S-victim",
            kestrel: { turn_count: 1, tool_count: 1, duration_ms: 570000 },
            projectId: PROJECT.id }),
];
for (let i = 0; i < 1_996; i++) {
  victim.push(rawSpan({
    id: `bb-${hex(i)}`, name: `victim ${i}`, start: T0 - 16.9 * MIN + i * 290, dur: 4,
    agent: "victim", spanId: `vc${hex(i)}`, session: "S-victim", traceId: "S-victim",
    projectId: PROJECT.id,
  }));
}
const spans = [...kept, ...victim,
               ...filler(58_992, { session: "S-fill", agent: "filler", from: T0 - 6 * MIN, step: 4 })];
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_cap_never_splits_a_session_unit(tmp_path):
    """The eviction unit is the session — the render model's folds stay intact.

    The overage is 1_000 spans and the oldest session holds 2_000, so the unit is
    dropped WHOLE and the store lands a thousand spans under the cap rather than
    stopping halfway through a session. What that buys, asserted on the retained
    session:

    - a completed turn is never separated from its ``turn <n> summary``: it still
      paints its folded label and nothing in the frame renders as still-running;
    - a ``(started)`` marker is never separated from its twin: the refused Bash
      still reads as the one terminal stub it is, with no phantom open band;
    - a parentless turn root whose closing summary is parented to the SESSION
      MARKER — related to it by nothing but the session key — is retained with
      it. The victim session carries that same shape and is dropped with it.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "cap-session-unit.mjs",
        _TIMELINE_PRELUDE
        + _EVICTION_PRELUDE
        + _UNIT_FIXTURE
        + r"""
const phoenix = installFakePhoenix({ projects: [PROJECT], spans });
const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");
const refresh = container.querySelector("[data-refresh]");

await settle(phoenix.calls); // boot pass
const ticks = await drainTicks(phoenix, canvas, refresh, ["kept", "victim", "filler"]);
const ops = lastFrame(canvas);
const text = frameText(ops);
mounted.destroy();

process.stdout.write(JSON.stringify({
  total: spans.length,
  keptSize: kept.length,
  victimSize: victim.length,
  drained: ticks[ticks.length - 1].drained,
  settled: ticks[ticks.length - 1].lanes,
  // Folded from its summary the turn 1 band reads "turn 1 · 2 tools · …";
  // separated from it, with nothing left to fold, it falls back to the bare span
  // name and runs open-ended to the right edge.
  foldedTurn: text.filter((t) => t.startsWith("turn 1")),
  reopenedTurn: text.includes("claude-code turn 1"),
  // Turn 2 is closed by the session summary alone — its own summary hangs off
  // the session marker — so its band is the bare name, and closed.
  turn2: text.includes("claude-code turn 2"),
  refusalStub: text.includes("Bash · denied"),
  phantomBand: text.includes("Bash (started)"),
  victimTurn: text.includes("victim turn 1"),
  openEdges: openEdges(ops),
}));
""",
    )

    assert result["total"] == SPAN_CAP + 1_000
    assert result["victimSize"] == 2_000  # twice the overage: half of it is spare
    assert result["drained"] is True

    # The unit went whole — head, leaves and the summary that hangs off its
    # session marker — and nothing of it is left behind.
    assert result["settled"]["victim"] is False
    assert result["victimTurn"] is False
    assert result["settled"]["kept"] is True
    assert result["settled"]["filler"] is True

    # Turn 1 kept its summary: the folded label, no bare name, and not one
    # open-edge cap painted anywhere in the frame.
    assert result["foldedTurn"] and result["foldedTurn"][0].startswith("turn 1 · 2 tools")
    assert result["reopenedTurn"] is False
    assert result["openEdges"] == 0

    # Turn 2 — parentless, closed only through the session key — is retained with
    # the session marker its summary hangs off.
    assert result["turn2"] is True

    # The refused call kept its twin: one terminal stub, no phantom open band.
    assert result["refusalStub"] is True
    assert result["phantomBand"] is False


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_a_session_larger_than_the_cap_exceeds_it_rather_than_splitting(tmp_path):
    """One session bigger than the whole cap: the explicit, logged branch.

    Whole-unit eviction cannot satisfy the cap here — the store is over it
    because of ONE session, and that session cannot be split. The old policy's
    answer (take the oldest-ending spans, wherever they live) is exactly the
    behaviour being removed, so there is no fallback to it: the cycle evicts the
    one closed session it CAN take whole, logs why it is still over, and leaves
    the oversized session untouched. Its oldest span — the first casualty of any
    span-level policy — is still painted.

    The decision is a function of the state, so a second cycle that re-reaches it
    does not re-log: an operator gets a record, not a stream.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "cap-oversized-session.mjs",
        _TIMELINE_PRELUDE
        + _EVICTION_PRELUDE
        + r"""
// 60_250 spans = the cap + 250. One session holds 60_200 of them — more than
// the whole cap — and the other 50 are a small closed session that CAN go whole.
const spans = [];
for (let i = 0; i < 50; i++) {
  spans.push(rawSpan({
    id: `a-${hex(i)}`, name: `small ${i}`, start: T0 - 19 * MIN + i * 200, dur: 4,
    agent: "small", spanId: `sm${hex(i)}`, session: "S-small", traceId: "S-small",
    projectId: PROJECT.id,
  }));
}
// The oldest span in the oversized session, wide enough to read: the very first
// thing a span-level eviction would take.
spans.push(rawSpan({
  id: "b0-mono-head", name: "mono head", start: T0 - 20 * MIN, dur: 3 * MIN,
  agent: "mono", spanId: "monohead", kind: "AGENT", session: "S-mono",
  traceId: "S-mono", projectId: PROJECT.id,
}));
spans.push(...filler(60_199, { session: "S-mono", agent: "mono", from: T0 - 16 * MIN }));

const warnings = [];
console.warn = (...args) => warnings.push(args.map(String).join(" "));

const phoenix = installFakePhoenix({ projects: [PROJECT], spans });
const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");
const refresh = container.querySelector("[data-refresh]");

await settle(phoenix.calls); // boot pass
const ticks = await drainTicks(phoenix, canvas, refresh, ["small", "mono"]);
const text = frameText(lastFrame(canvas));
const afterFirst = warnings.length;
// A second cycle reaches the same decision about the same store.
await drainTicks(phoenix, canvas, refresh, ["small", "mono"]);
mounted.destroy();

process.stdout.write(JSON.stringify({
  total: spans.length,
  drained: ticks[ticks.length - 1].drained,
  settled: ticks[ticks.length - 1].lanes,
  monoHead: text.includes("mono head"),
  warnings,
  afterFirst,
}));
""",
    )

    assert result["total"] == SPAN_CAP + 250
    assert result["drained"] is True

    # The closed session small enough to take went whole; the oversized one was
    # left entirely alone — including the span every span-level order eats first.
    assert result["settled"]["small"] is False
    assert result["settled"]["mono"] is True
    assert result["monoHead"] is True

    # And the store staying over the cap is a decision on the record, not an
    # accident — logged once, with the reason and the promise it does not break.
    assert result["afterFirst"] == 1
    assert len(result["warnings"]) == 1
    notice = result["warnings"][0]
    assert f"{SPAN_CAP + 200} spans over the {SPAN_CAP} cap" in notice
    assert "1 larger than the cap" in notice
    assert "spans are never evicted individually" in notice


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_the_cap_binds_again_on_every_cycle_as_new_sessions_arrive(tmp_path):
    """Repeated forced evictions: retention never accumulates.

    Whole-unit eviction retains more than the old policy did — a unit is dropped
    whole, and a live one is not dropped at all — so the question it has to answer
    is whether the cap still binds under continuous ingestion. It does, once per
    completed cycle: five closed sessions of 400 spans sit behind a cap-filling
    store, and each arriving wave of 400 costs exactly one of them, oldest first.
    The store returns under the cap every cycle and no cycle needs the
    over-cap branch.
    """
    pkg = _poll_pkg(tmp_path)
    result = _run_scenario(
        pkg,
        "cap-repeated.mjs",
        _TIMELINE_PRELUDE
        + _EVICTION_PRELUDE
        + r"""
// A session of 400 leaves under its own agent, so its lane label answers
// "is this session still in the store?".
function cohort(tag, start) {
  const out = [];
  for (let i = 0; i < 400; i++) {
    out.push(rawSpan({
      id: `${tag}-${hex(i)}`, name: `${tag} ${i}`, start: start + i * 100, dur: 4,
      agent: tag, spanId: `${tag}${hex(i)}`, session: `S-${tag}`, traceId: `S-${tag}`,
      projectId: PROJECT.id,
    }));
  }
  return out;
}

// 60_100 = the cap + 100: the store opens one eviction over, and every wave
// below puts it right back over by the same 100.
const spans = [];
for (let k = 0; k < 5; k++) spans.push(...cohort(`gen${k}`, T0 - (19 - k) * MIN));
spans.push(...filler(58_100, { session: "S-fill", agent: "filler", from: T0 - 13 * MIN }));

const warnings = [];
console.warn = (...args) => warnings.push(args.map(String).join(" "));

const phoenix = installFakePhoenix({ projects: [PROJECT], spans });
const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, { openTrace() {}, openNavigator() {} });
const canvas = container.querySelector("[data-canvas]");
const refresh = container.querySelector("[data-refresh]");
const LANES = ["gen0", "gen1", "gen2", "gen3", "gen4", "filler",
               "wave1", "wave2", "wave3", "wave4"];

await settle(phoenix.calls); // boot pass
const base = await drainTicks(phoenix, canvas, refresh, LANES);
const cycles = [{ ticks: base.length, lanes: base[base.length - 1].lanes }];
for (let k = 1; k <= 4; k++) {
  // Newer than every span the base walk covered, so the forward-only live poll
  // picks the wave up on its next tick.
  phoenix.add(...cohort(`wave${k}`, T0 - (5 - k) * MIN));
  const wave = await drainTicks(phoenix, canvas, refresh, LANES);
  cycles.push({ ticks: wave.length, lanes: wave[wave.length - 1].lanes });
}
mounted.destroy();

process.stdout.write(JSON.stringify({ total: spans.length, cycles, warnings }));
""",
    )

    assert result["total"] == SPAN_CAP + 100

    # Every wave is one page: the walk drains inside a single tick, so each cycle
    # is a complete ingestion and gets to bind the cap.
    for cycle in result["cycles"][1:]:
        assert cycle["ticks"] == 1

    # Cycle by cycle, exactly one more session is gone — oldest first — and the
    # arriving waves are all retained.
    for index, cycle in enumerate(result["cycles"]):
        lanes = cycle["lanes"]
        for k in range(5):
            assert lanes[f"gen{k}"] is (k > index), (index, k, lanes)
        for k in range(1, 5):
            assert lanes[f"wave{k}"] is (k <= index), (index, k, lanes)
        assert lanes["filler"] is True

    # The cap was reachable every time, so nothing took the over-cap branch.
    assert result["warnings"] == []
