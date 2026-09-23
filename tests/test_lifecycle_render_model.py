"""Stop / Hold / Resume receipts rendered as lifecycle events (#118).

The render rules (kestrel-sovereign#3159 R1/R5/R6, and #118's rulings) are
executed under node against the shipped ``lifecycle.js`` / ``timeline.js`` /
``navigator.js``:

1. The receipts decide what happened; spans are only matched to them — a
   turn-scope Stop by exact ``(trace_id, span_id)``, an agent/host-scope Stop by
   DID at ``occurred_at``, and a receipt with no DID is "agent not recorded".
2. A turn's picture is a pure function of (spans, receipts, latches): shuffled,
   duplicated, late and reloaded fetches produce an IDENTICAL model.
3. A held agent always has a visible state; host and agent latches are drawn
   independently; a Resume is its own event and never edits the Hold.
4. An unknown ``schema_version`` is shown as "unrecognized receipt" and never
   reclassifies a turn.
5. Receipt reason/actor render escaped and clipped, and never leak into view
   state.

The mounted tests drive both views against the same Phoenix + host doubles and
assert they state the same lifecycle with the same receipt correlation.
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

pytestmark = pytest.mark.skipif(NODE is None, reason="node runtime not available")


def _module_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """The shipped modules, with the console API client routed to a host double.

    ``globalThis.__requestHost(path, options)`` answers every host-root request
    (the Phoenix embed mint and the three lifecycle reads).
    """
    pkg = tmp_path / "lifecycle"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    phoenix = (STATIC / "phoenix.js").read_text(encoding="utf-8")
    routed = phoenix.replace(
        'import API from "/js/api.js";',
        "const API = { requestHost: (path, options) => globalThis.__requestHost(path, options) };",
    )
    assert "globalThis.__requestHost" in routed
    (pkg / "phoenix.js").write_text(routed, encoding="utf-8")
    for name in ("lifecycle.js", "timeline.js", "navigator.js"):
        (pkg / name).write_text((STATIC / name).read_text(encoding="utf-8"), encoding="utf-8")
    _write_fake_dom(pkg)
    (pkg / "fixture.mjs").write_text(_FIXTURE, encoding="utf-8")
    return pkg


def _run(pkg: pathlib.Path, name: str, source: str) -> dict:
    (pkg / name).write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(pkg / name)],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# One agent fleet, every receipt shape. Times are relative to NOW so the mounted
# views (which read the wall clock) and the pure model see the same picture.
_FIXTURE = r"""
export const NOW = Date.now();
const MIN = 60_000;
export const DID_CLAW = "did:key:claw";
export const DID_EMMA = "did:key:emma";
export const DID_IDLE = "did:key:idle";
export const ACTOR = "did:key:sovereign-secret-actor";
export const REASON = "<script>alert(1)</script> private operator reason";
const iso = (ms) => new Date(ms).toISOString();
// OTel span ids are hex, and the exact-id filters drop anything else — so the
// fixture's readable labels are hex-encoded.
const hexId = (label) => Buffer.from(label).toString("hex");
export const rootId = (agent, n) => hexId(`root-${agent}-${n}`);

// ── Phoenix spans (raw GraphQL shape) ──
function attrs(agent, did, extra = {}) {
  return {
    openinference: { span: { kind: extra.kind || "AGENT" } },
    session: { id: extra.session || "sess-claw" },
    kestrel: {
      agent_name: agent,
      agent_did: did,
      session_id: extra.session || "sess-claw",
      ...(extra.kestrel || {}),
    },
  };
}
export function turnSpans(agent, did, n, startMs, outcome, session) {
  const trace = `trace-${agent}-${n}`;
  const root = {
    id: `node-${agent}-root-${n}`,
    name: `${agent} turn ${n}`,
    spanKind: "agent",
    startTime: iso(startMs),
    endTime: iso(startMs),
    latencyMs: 0,
    statusCode: "OK",
    parentId: null,
    attributes: JSON.stringify(
      attrs(agent, did, { session, kestrel: { marker: "start", turn_index: n, turn_id: `${agent}#${n}` } }),
    ),
    context: { spanId: rootId(agent, n), traceId: trace },
  };
  const summaryKestrel = { turn_index: n, turn_id: `${agent}#${n}`, duration_ms: 4 * MIN };
  if (outcome != null) summaryKestrel.turn = { outcome };
  const summary = {
    id: `node-${agent}-summary-${n}`,
    name: `turn ${n} summary`,
    spanKind: "chain",
    startTime: iso(startMs),
    endTime: iso(startMs + 4 * MIN),
    latencyMs: 4 * MIN,
    statusCode: "OK",
    parentId: root.context.spanId,
    attributes: JSON.stringify(attrs(agent, did, { session, kind: "CHAIN", kestrel: summaryKestrel })),
    context: { spanId: hexId(`summary-${agent}-${n}`), traceId: trace },
  };
  return [root, summary];
}
// The Phoenix double's answer to the Navigator's exact summary read:
// `(parent_id in ['…', …]) and ('summary' in name)`. Null for any other filter.
export function summariesByParent(spans, filter) {
  const m = /^\(parent_id in \[([^\]]*)\]\) and \('summary' in name\)$/.exec(filter || "");
  if (!m) return null;
  const parents = new Set(m[1].split(",").map((x) => x.trim().replace(/^'|'$/g, "")));
  return spans.filter((s) => parents.has(s.parentId) && s.name.includes("summary"));
}
export function turnStart(n) {
  return NOW - 58 * MIN + n * 6 * MIN;
}

// The normalize() read-model shape the Timeline's pure functions consume.
export function record(raw) {
  const a = JSON.parse(raw.attributes);
  const start = Date.parse(raw.startTime);
  const end = Date.parse(raw.endTime);
  return {
    id: raw.id,
    name: raw.name,
    start,
    end,
    instant: end <= start,
    openEnded: false,
    marker: a.kestrel.marker || null,
    kind: raw.spanKind.toUpperCase(),
    status: "ok",
    agent: a.kestrel.agent_name,
    agentDid: a.kestrel.agent_did,
    worker: null,
    orchestrator: null,
    sessionId: a.kestrel.session_id,
    spanId: raw.context.spanId,
    parentId: raw.parentId,
    traceId: raw.context.traceId,
    projectId: "p1",
    projectName: "kestrel-fleet",
    attrs: a,
  };
}

// ── Receipts (schema_version 1, the core wire shape) ──
let seq = 0;
export function stopReceipt(id, { scope, at, target = null, trace = null, span = null, outcomes, reason = REASON }) {
  seq += 1;
  return {
    feed_seq: seq,
    receipt_id: id,
    scope,
    actor_id: ACTOR,
    target_agent_id: target,
    reason,
    cascade: true,
    occurred_at: iso(at),
    trace_id: trace,
    span_id: span,
    outcomes: outcomes.map(([disposition, agent], ordinal) => ({
      ordinal,
      disposition,
      detail: null,
      agent_id: agent,
      resolved_target: null,
    })),
  };
}
export function holdReceipt(id, { action, scope, target, at, prior = "", resulting = "", expected = "", disposition = "applied" }) {
  seq += 1;
  return {
    feed_seq: seq,
    receipt_id: id,
    operation_id: `op-${id}`,
    action,
    disposition,
    scope,
    target_id: target,
    reason: REASON,
    actor_id: ACTOR,
    occurred_at: iso(at),
    expected_hold_receipt_id: expected,
    prior_hold_receipt_id: prior,
    resulting_hold_receipt_id: resulting,
  };
}
export function latch(scope, target, receiptId, at) {
  return { scope, target_id: target, reason: REASON, actor_id: ACTOR, set_at: iso(at), hold_receipt_id: receiptId, revision: 1 };
}
"""


_PURE = r"""
import {
  NOW, DID_CLAW, DID_EMMA, DID_IDLE, REASON, ACTOR,
  turnSpans, turnStart, record, stopReceipt, holdReceipt, latch, rootId,
} from "./fixture.mjs";
import {
  createLifecycleStore, ingestReceiptPage, ingestHoldState, recordFeedFailure, lifecycleIndex,
  lifecycleFeedNotices, renderStopReceiptHtml, renderLifecycleEventHtml, renderTurnLifecycleHtml,
} from "./lifecycle.js";
import { annotateRenderModel, annotateLifecycle, laneGroups, lifecycleLaneModel } from "./timeline.js";

const MIN = 60_000;
// Claw: t1 stopped (exact receipt), t2 stopped per span only, t3 completed but
// Stop arrived late, t4 failed, t5 disconnected, t6 interrupted, t7 stopped by an
// agent-scope Stop, t8 an outcome this build does not know. Emma: e1 stopped by
// a host fan-out.
const outcomes = ["stopped", "stopped", "completed", "failed", "disconnected", "interrupted", "stopped", "paused"];
const raw = [];
outcomes.forEach((o, i) => raw.push(...turnSpans("Claw", DID_CLAW, i + 1, turnStart(i), o)));
raw.push(...turnSpans("Emma", DID_EMMA, 1, turnStart(2) + 30_000, "stopped", "sess-emma"));

const stopRows = [
  stopReceipt("R1", { scope: "turn", at: turnStart(0) + MIN, target: DID_CLAW, trace: "trace-Claw-1", span: rootId("Claw", 1), outcomes: [["stopped", DID_CLAW]] }),
  stopReceipt("R3", { scope: "turn", at: turnStart(2) + 5 * MIN, target: DID_CLAW, trace: "trace-Claw-3", span: rootId("Claw", 3), outcomes: [["already_complete", DID_CLAW]] }),
  stopReceipt("R7", { scope: "agent", at: turnStart(6) + MIN, target: DID_CLAW, outcomes: [["stopped", null]] }),
  stopReceipt("RH", { scope: "host", at: turnStart(2) + MIN, outcomes: [["stopped", DID_EMMA], ["already_complete", DID_CLAW], ["unreachable", DID_IDLE], ["unreachable", null]] }),
  stopReceipt("R0", { scope: "agent", at: turnStart(0), target: null, outcomes: [["stopped", null]] }),
];
const late = stopReceipt("R2", { scope: "turn", at: turnStart(1) + MIN, target: DID_CLAW, trace: "trace-Claw-2", span: rootId("Claw", 2), outcomes: [["stopped", DID_CLAW]] });
const holdRows = [
  holdReceipt("H1", { action: "hold", scope: "host", target: "host", at: NOW - 50 * MIN, resulting: "H1" }),
  holdReceipt("H2", { action: "hold", scope: "agent", target: DID_IDLE, at: NOW - 40 * MIN, resulting: "H2" }),
  holdReceipt("H3", { action: "release", scope: "agent", target: DID_IDLE, at: NOW - 30 * MIN, prior: "H2", expected: "H2" }),
  holdReceipt("H4", { action: "hold", scope: "agent", target: DID_IDLE, at: NOW - 20 * MIN, resulting: "H4" }),
];
const holdState = {
  can_hold: true,
  host_hold: latch("host", "host", "H1", NOW - 50 * MIN),
  agents: [
    { agent_id: DID_CLAW, held: true, sources: ["host"], agent_hold: null },
    { agent_id: DID_EMMA, held: true, sources: ["host"], agent_hold: null },
    { agent_id: DID_IDLE, held: true, sources: ["host", "agent"], agent_hold: latch("agent", DID_IDLE, "H4", NOW - 20 * MIN) },
  ],
};
const page = (receipts, next = null, version = 1) => ({ schema_version: version, receipts, next_cursor: next });

// The render model both views state: per span, lifecycle; per lane, its events.
function model(store, spanRaws) {
  const spans = spanRaws.map(record);
  annotateRenderModel(spans, NOW);
  const index = lifecycleIndex(store);
  annotateLifecycle(spans, index, NOW);
  const lanes = laneGroups(spans);
  const keys = new Set(spans.map((s) => `${s.traceId} ${s.spanId}`));
  const laneModel = lifecycleLaneModel(lanes, index, keys);
  const turns = {};
  for (const s of [...spans].sort((a, b) => (a.id < b.id ? -1 : 1))) {
    if (!s.rLifecycle) continue;
    turns[s.name + (s.agent === "Emma" ? " (Emma)" : "")] = {
      state: s.rLifecycle.state,
      label: s.rLifecycle.label,
      tone: s.rLifecycle.tone,
      markers: s.rLifecycle.markers,
      receipts: s.rLifecycle.receiptIds,
      outcomes: s.rLifecycle.receipts.map((e) => e.outcomes.map((o) => o.disposition)),
    };
  }
  const describe = (lc) => ({
    held: lc.holder ? lc.holder.held : null,
    sources: lc.holder ? lc.holder.sources : null,
    episodes: lc.episodes.map((e) => ({ hold: e.hold && e.hold.receiptId, end: e.end && e.end.receiptId, open: e.open, scope: e.scope })),
    events: lc.events.map((e) => ({ id: e.id, kind: e.kind, disposition: e.disposition, action: e.receipt && e.receipt.action })),
  });
  const homes = {};
  for (const [lane, lc] of laneModel.homes) homes[lane.label] = describe(lc);
  const synthetic = laneModel.synthetic.map((l) => ({ key: l.key, label: l.label, ...describe(l.lifecycle) }));
  return { turns, homes, synthetic, notices: lifecycleFeedNotices(store) };
}

function load(order) {
  const store = createLifecycleStore();
  for (const step of order) step(store);
  return store;
}
const steps = {
  stopA: (st) => ingestReceiptPage(st, "stop", page(stopRows.slice(0, 2), "2")),
  stopB: (st) => ingestReceiptPage(st, "stop", page(stopRows.slice(2))),
  holdA: (st) => ingestReceiptPage(st, "hold", page(holdRows.slice(0, 3), "3")),
  holdB: (st) => ingestReceiptPage(st, "hold", page(holdRows.slice(3))),
  latches: (st) => ingestHoldState(st, holdState),
};
const out = {};
out.base = model(load([steps.stopA, steps.stopB, steps.holdA, steps.holdB, steps.latches]), raw);

// Shuffled, duplicated and reloaded arrival — spans reversed too.
const orders = [
  [steps.latches, steps.holdB, steps.stopB, steps.holdA, steps.stopA],
  [steps.stopB, steps.stopB, steps.holdA, steps.latches, steps.stopA, steps.holdB, steps.holdA],
  [steps.holdB, steps.latches, steps.stopA, steps.stopB, steps.holdA, steps.latches],
];
out.shuffled = orders.map((o, i) => JSON.stringify(model(load(o), i % 2 ? [...raw].reverse() : raw)));
out.baseJson = JSON.stringify(out.base);

// A late receipt converges "stopped (receipt pending)" → "stopped".
const lateStore = load([steps.stopA, steps.stopB, steps.holdA, steps.holdB, steps.latches]);
ingestReceiptPage(lateStore, "stop", page([late]));
out.late = model(lateStore, raw).turns["Claw turn 2"];

// A span that arrives AFTER its receipt: the receipt is shown on the lane until
// the span lands, then joins the span exactly.
const early = model(load([steps.stopA, steps.stopB]), raw.filter((s) => !s.id.includes("Claw-root-1") && !s.id.includes("Claw-summary-1")));
out.receiptBeforeSpan = early.homes.Claw.events.map((e) => e.id);

// An unknown schema_version is shown, never used to classify.
const unknown = load([steps.stopA, steps.stopB]);
ingestReceiptPage(unknown, "stop", page([{ ...late, feed_seq: 99, receipt_id: "R-future" }], null, 2));
out.unknown = model(unknown, raw);

// A v1 row that breaks the contract is unrecognized too.
const malformed = createLifecycleStore();
ingestReceiptPage(malformed, "stop", page([{ ...late, outcomes: [{ ordinal: 0, disposition: "vaporized" }] }]));
out.malformed = model(malformed, raw);

// A refused feed is reported, never rendered as "nothing happened".
const refused = createLifecycleStore();
recordFeedFailure(refused, "stop", Object.assign(new Error("forbidden"), { status: 403 }));
recordFeedFailure(refused, "latches", Object.assign(new Error("down"), { status: 503 }));
out.refused = lifecycleFeedNotices(refused);

// Privacy: escaped and clipped.
const html = renderStopReceiptHtml(lateStore.stop.get("R1"));
out.escaped = !html.includes("<script>") && html.includes("&lt;script&gt;");
const longReason = stopReceipt("RL", { scope: "agent", at: NOW, target: DID_CLAW, outcomes: [["stopped", null]], reason: "x".repeat(9000) });
const longStore = createLifecycleStore();
ingestReceiptPage(longStore, "stop", page([longReason]));
const longHtml = renderStopReceiptHtml(longStore.stop.get("RL"));
out.clipped = longHtml.includes("(truncated)") && !longHtml.includes("x".repeat(4001));
out.holdHtml = renderLifecycleEventHtml({ kind: "resume", receipt: lateStore.hold.get("H3") });
out.pendingHtml = renderTurnLifecycleHtml(null);
process.stdout.write(JSON.stringify(out));
"""


def test_render_rules_are_a_pure_function_of_spans_receipts_and_latches(tmp_path):
    pkg = _module_dir(tmp_path)
    out = _run(pkg, "pure.mjs", _PURE)
    turns = out["base"]["turns"]

    # Rule 1/2: an exact turn-scope receipt stops its turn, receipt inspectable.
    assert turns["Claw turn 1"]["state"] == "stopped"
    assert turns["Claw turn 1"]["receipts"] == ["R1"]
    # The span alone says stopped → pending until the receipt lands.
    assert turns["Claw turn 2"] == {
        "state": "stopped_pending",
        "label": "stopped (receipt pending)",
        "tone": "pending",
        "markers": [],
        "receipts": [],
        "outcomes": [],
    }
    assert out["late"]["state"] == "stopped" and out["late"]["receipts"] == ["R2"]
    # already_complete keeps the turn's own outcome and adds the marker.
    assert turns["Claw turn 3"]["state"] == "completed"
    assert turns["Claw turn 3"]["markers"] == ["Stop arrived after completion"]
    assert turns["Claw turn 3"]["receipts"] == ["R3"]
    # disconnected / interrupted / failed are distinct; only failed is error-red.
    states = {name: t["state"] for name, t in turns.items()}
    assert states["Claw turn 4"] == "failed"
    assert states["Claw turn 5"] == "disconnected"
    assert states["Claw turn 6"] == "interrupted"
    assert [n for n, t in turns.items() if t["tone"] == "error"] == ["Claw turn 4"]
    # Agent scope joins by DID at occurred_at; host scope by each outcome's DID.
    assert turns["Claw turn 7"]["state"] == "stopped" and turns["Claw turn 7"]["receipts"] == ["R7"]
    assert turns["Emma turn 1 (Emma)"]["state"] == "stopped"
    assert turns["Emma turn 1 (Emma)"]["receipts"] == ["RH"]
    assert turns["Emma turn 1 (Emma)"]["outcomes"] == [["stopped"]]
    # An outcome this build does not know is shown as such, not mapped.
    assert turns["Claw turn 8"]["state"] == "unrecognized_outcome"
    assert turns["Claw turn 8"]["label"] == 'unrecognized outcome "paused"'

    # Rule 2: shuffled, duplicated, reloaded arrival → identical model.
    assert out["shuffled"] == [out["baseJson"]] * 3

    # A receipt whose span is not loaded yet shows on the lane, then joins it.
    assert "stop:R1:did:key:claw" in out["receiptBeforeSpan"]
    homes = out["base"]["homes"]
    claw_events = [e["id"] for e in homes["Claw"]["events"]]
    assert "stop:R1:did:key:claw" not in claw_events  # joined to its loaded turn
    # Per-target partial outcomes stay inspectable on each agent's own lane.
    assert "stop:RH:did:key:claw" in claw_events
    rh_claw = next(e for e in homes["Claw"]["events"] if e["id"] == "stop:RH:did:key:claw")
    assert rh_claw["disposition"] == "already_complete"
    rh_emma = next(e for e in homes["Emma"]["events"] if e["id"] == "stop:RH:did:key:emma")
    assert rh_emma["disposition"] == "stopped"

    synthetic = {lane["key"]: lane for lane in out["base"]["synthetic"]}
    # Rule 3: a held agent with no spans still has a lane and a resting state.
    idle = synthetic["did:did:key:idle"]
    assert idle["held"] is True and idle["sources"] == ["agent", "host"]
    assert [e["open"] for e in idle["episodes"]] == [False, True]
    assert idle["episodes"][0] == {"hold": "H2", "end": "H3", "open": False, "scope": "agent"}
    # Resume is its own event; the Hold receipt it ended is unchanged.
    kinds = [(e["id"], e["kind"], e["action"]) for e in idle["events"] if e["kind"] != "stop"]
    assert kinds == [
        ("hold:H2", "hold", "hold"),
        ("hold:H3", "resume", "release"),
        ("hold:H4", "hold", "hold"),
    ]
    assert any(e["id"] == "stop:RH:did:key:idle" and e["disposition"] == "unreachable" for e in idle["events"])
    # Host and agent latches are independent: releasing the agent hold (H3)
    # never ended the host episode.
    host = synthetic["host"]
    assert host["episodes"] == [{"hold": "H1", "end": None, "open": True, "scope": "host"}]
    # A receipt with no DID is "agent not recorded" — never an inferred agent.
    unplaced = synthetic["unplaced"]
    assert unplaced["label"] == "agent not recorded"
    assert {e["id"] for e in unplaced["events"]} == {"stop:R0:-", "stop:RH:-"}
    assert out["base"]["notices"] == []

    # Rule 4: unknown schema_version → unrecognized, turn 2 NOT reclassified.
    unknown = out["unknown"]
    assert unknown["turns"]["Claw turn 2"]["state"] == "stopped_pending"
    assert any(l["key"] == "unrecognized" and l["events"] for l in unknown["synthetic"])
    assert unknown["notices"] == [
        {"feed": "stop", "status": "unrecognized", "text": "Stop receipts unrecognized schema_version 2"}
    ]
    malformed = out["malformed"]
    assert malformed["turns"]["Claw turn 2"]["state"] == "stopped_pending"
    assert any(l["key"] == "unrecognized" for l in malformed["synthetic"])

    assert out["refused"] == [
        {"feed": "stop", "status": "unavailable", "text": "Stop receipts unavailable (HTTP 403)"},
        {"feed": "latches", "status": "unavailable", "text": "Hold state unavailable (HTTP 503)"},
    ]

    # Rule 5: reason/actor escaped and clipped.
    assert out["escaped"] is True
    assert out["clipped"] is True
    assert "ends hold H2" in out["holdHtml"]
    assert out["pendingHtml"] == ""


_FEED = r"""
import { createLifecycleFeed, lifecycleFeedNotices, lifecycleBacklog } from "./lifecycle.js";
import { stopReceipt, NOW } from "./fixture.mjs";

const rows = [];
for (let i = 0; i < 450; i++) {
  rows.push(stopReceipt(`R${i}`, { scope: "agent", at: NOW, target: "did:key:a", outcomes: [["stopped", null]] }));
}
const calls = [];
let failStop = false;
async function request(path) {
  calls.push(path);
  const url = new URL(path, "http://host");
  if (url.pathname === "/api/host/hold") return { can_hold: false, host_hold: null, agents: [] };
  if (url.pathname === "/api/host/hold/receipts") return { schema_version: 1, receipts: [], next_cursor: null };
  if (failStop) throw Object.assign(new Error("Durable Stop evidence is unavailable."), { status: 503 });
  const after = Number(url.searchParams.get("cursor") || 0);
  const limit = Number(url.searchParams.get("limit"));
  const pageRows = rows.filter((r) => r.feed_seq > after).slice(0, limit + 1);
  const more = pageRows.length > limit;
  const served = pageRows.slice(0, limit);
  return {
    schema_version: 1,
    receipts: served,
    next_cursor: more ? String(served[served.length - 1].feed_seq) : null,
  };
}
const out = {};
const feed = createLifecycleFeed({ request, maxPages: 2 });
out.firstChanged = await feed.poll();
out.afterFirst = feed.store.stop.size;
out.firstCalls = calls.splice(0);
out.secondChanged = await feed.poll();
out.afterSecond = feed.store.stop.size;
out.secondCalls = calls.splice(0);
out.idleChanged = await feed.poll();
out.idleCalls = calls.splice(0);
failStop = true;
out.failChanged = await feed.poll();
out.failNotices = lifecycleFeedNotices(feed.store);
out.keptReceipts = feed.store.stop.size;
failStop = false;

// With a view to tell, the feed drains a deep history on its own — no second
// view tick is needed — and says the history is partial while it does.
const settle = async (pred) => {
  for (let i = 0; i < 400 && !pred(); i++) await new Promise((r) => setTimeout(r, 5));
};
const updates = [];
const drained = createLifecycleFeed({
  request,
  maxPages: 2,
  backlogMs: 0,
  onUpdate: (changed) => updates.push({ changed, size: drained.store.stop.size }),
});
await drained.poll();
out.drainFirst = { size: drained.store.stop.size, notices: lifecycleFeedNotices(drained.store) };
await settle(() => drained.store.stop.size === 450 && !lifecycleBacklog(drained.store));
out.drainDone = {
  size: drained.store.stop.size,
  backlog: lifecycleBacklog(drained.store),
  notices: lifecycleFeedNotices(drained.store),
  updates,
};
// A destroyed feed stops draining.
const stopped = createLifecycleFeed({ request, maxPages: 2, backlogMs: 0, onUpdate: () => {} });
await stopped.poll();
stopped.destroy();
await new Promise((r) => setTimeout(r, 30));
out.destroyedSize = stopped.store.stop.size;
process.stdout.write(JSON.stringify(out));
"""


def test_feed_pages_on_the_core_cursor_and_reports_failures(tmp_path):
    pkg = _module_dir(tmp_path)
    out = _run(pkg, "feed.mjs", _FEED)
    stop_calls = lambda calls: [c for c in calls if c.startswith("/api/host/stop/receipts")]  # noqa: E731

    # Page cap per poll: two pages now, the rest resumes on the next poll.
    assert out["firstChanged"] is True and out["afterFirst"] == 400
    first_seq = stop_calls(out["firstCalls"])
    first = first_seq[0]
    assert "cursor=" not in first and "limit=200" in first
    assert "cursor=200" in first_seq[1]
    assert out["secondChanged"] is True and out["afterSecond"] == 450
    # Nothing new: the next poll asks strictly after the newest feed_seq held.
    assert out["idleChanged"] is False
    (idle,) = stop_calls(out["idleCalls"])
    assert "cursor=450" in idle
    assert any(c == "/api/host/hold" for c in out["idleCalls"])
    # A 503 is visible, and the receipts already held stay held.
    assert out["failChanged"] is True
    assert out["failNotices"] == [
        {"feed": "stop", "status": "unavailable", "text": "Stop receipts unavailable (HTTP 503)"}
    ]
    assert out["keptReceipts"] == 450

    # A deep history keeps draining after one poll, and says so meanwhile.
    assert out["drainFirst"]["size"] == 400
    assert out["drainFirst"]["notices"] == [
        {"feed": "stop", "status": "loading", "text": "Stop receipts loading older history…"}
    ]
    assert out["drainDone"]["size"] == 450
    assert out["drainDone"]["backlog"] is False
    assert out["drainDone"]["notices"] == []
    assert out["drainDone"]["updates"] == [{"changed": True, "size": 450}]
    assert out["destroyedSize"] == 400


_MOUNTED = r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";
import {
  NOW, DID_CLAW, DID_IDLE, ACTOR, REASON,
  turnSpans, turnStart, stopReceipt, holdReceipt, latch, rootId, summariesByParent,
} from "./fixture.mjs";

installFakeDom();
const MIN = 60_000;
// Claw: turn 1 stopped with its receipt; turn 2 stopped per the span only.
// DID_IDLE is held and has NO spans at all.
const spans = [
  ...turnSpans("Claw", DID_CLAW, 1, NOW - 20 * MIN, "stopped"),
  ...turnSpans("Claw", DID_CLAW, 2, NOW - 12 * MIN, "stopped"),
];
const receipts = [
  stopReceipt("R1", { scope: "turn", at: NOW - 19 * MIN, target: DID_CLAW, trace: "trace-Claw-1", span: rootId("Claw", 1), outcomes: [["stopped", DID_CLAW]] }),
];
const holds = [holdReceipt("H4", { action: "hold", scope: "agent", target: DID_IDLE, at: NOW - 25 * MIN, resulting: "H4" })];
globalThis.__requestHost = async (path) => {
  const url = new URL(path, "http://host");
  if (url.pathname === "/api/host/phoenix/session") return {};
  if (url.pathname === "/api/host/stop/receipts") return { schema_version: 1, receipts, next_cursor: null };
  if (url.pathname === "/api/host/hold/receipts") return { schema_version: 1, receipts: holds, next_cursor: null };
  if (url.pathname === "/api/host/hold") {
    return {
      can_hold: true,
      host_hold: null,
      agents: [
        { agent_id: DID_CLAW, held: false, sources: [], agent_hold: null },
        { agent_id: DID_IDLE, held: true, sources: ["agent"], agent_hold: latch("agent", DID_IDLE, "H4", NOW - 25 * MIN) },
      ],
    };
  }
  throw new Error(`unexpected host path ${path}`);
};
const project = { id: "p1", name: "kestrel-fleet", traceCount: 2, endTime: new Date(NOW).toISOString() };
const edges = (list) => ({ node: { spans: { edges: list.map((node) => ({ node })), pageInfo: { hasNextPage: false, endCursor: null } } } });
globalThis.fetch = async (_url, options) => {
  const { query, variables = {} } = JSON.parse(options.body);
  let data;
  if (query.includes("NavigatorProjects")) data = { projects: { edges: [{ node: project }] } };
  else if (query.includes("NavigatorTraceSpans")) {
    data = { node: { trace: { spans: { edges: spans.filter((s) => s.context.traceId === variables.traceId).map((node) => ({ node })) } } } };
  } else if (query.includes("NavigatorSpanPage")) {
    const f = variables.filter || "";
    const summaries = summariesByParent(spans, f);
    if (summaries) data = edges(summaries);
    else if (variables.rootOnly) data = edges(spans.filter((s) => !s.parentId));
    else data = edges(spans);
  } else throw new Error("unexpected GraphQL operation");
  return { status: 200, ok: true, json: async () => ({ data }) };
};

const out = {};
const { mount: mountTimeline } = await import("./timeline.js");
const { mount: mountNavigator } = await import("./navigator.js");

// ── Timeline ──
const tlContainer = new FakeElement("div");
const timeline = mountTimeline(tlContainer, { openTrace() {}, openNavigator() {} });
const canvas = tlContainer.querySelector("[data-canvas]");
const texts = () => {
  const frame = canvas.context.frames[canvas.context.frames.length - 1];
  return frame ? frame.operations.filter((o) => o.type === "fillText") : [];
};
await waitFor(() => texts().some((o) => String(o.args[0]).includes("turn 1 · 4m 0s · stopped")), "Timeline never painted the stopped turn");
out.tlTexts = texts().map((o) => String(o.args[0]));
const pop = tlContainer.querySelector("[data-pop]");
const click = async (label, wait) => {
  const op = texts().find((o) => String(o.args[0]).includes(label));
  const pointer = { button: 0, pointerId: 1, clientX: op.args[1] + 4, clientY: op.args[2], offsetX: op.args[1] + 4, offsetY: op.args[2] };
  canvas.dispatch("pointerdown", pointer);
  canvas.dispatch("pointerup", pointer);
  await waitFor(() => pop.innerHTML.includes(wait), `popover for ${label} did not open`);
  return pop.innerHTML;
};
out.tlTurn1 = await click("turn 1 · 4m 0s · stopped", "R1");
out.tlTurn2 = await click("turn 2 · 4m 0s · stopped (receipt pending)", "receipt pending");
out.tlIdle = await click("⏸ did:key:idle", "H4");
out.tlState = JSON.stringify(timeline.getState());
timeline.destroy();

// ── Navigator: the same turns through the reveal path ──
async function navigatorTurn(traceId, spanId) {
  const container = new FakeElement("div");
  const nav = mountNavigator(container, {
    revealTarget: { projectId: "p1", projectName: "kestrel-fleet", agentName: "Claw", sessionId: "sess-claw", traceId, spanId },
  });
  const inspector = container.querySelector("[data-inspector]");
  await waitFor(() => inspector.innerHTML.includes(spanId) && inspector.innerHTML.includes("obs-lifecycle"), `Navigator never inspected ${spanId}`);
  const html = { inspector: inspector.innerHTML, tree: container.querySelector("[data-spacer]").innerHTML };
  return { nav, container, html };
}
const n1 = await navigatorTurn("trace-Claw-1", rootId("Claw", 1));
out.navTurn1 = n1.html;
n1.nav.destroy();
const n2 = await navigatorTurn("trace-Claw-2", rootId("Claw", 2));
out.navTurn2 = n2.html;
n2.nav.destroy();

// The held agent with no spans: the lifecycle group → its holder node.
const container = new FakeElement("div");
const nav = mountNavigator(container, {});
const spacer = container.querySelector("[data-spacer]");
const rowIndex = (label) => {
  for (const chunk of spacer.innerHTML.split('<div class="obs-nav__row')) {
    if (chunk.includes(label)) return /data-i="(\d+)"/.exec(chunk)[1];
  }
  return null;
};
await waitFor(() => rowIndex("Stop · Hold · Resume") != null, "Navigator lifecycle group missing");
const clickRow = (label, caret) =>
  spacer.dispatch("click", {
    target: {
      closest: (sel) =>
        sel === "[data-i]" ? { dataset: { i: rowIndex(label) } } : sel === "[data-caret]" && caret ? {} : null,
    },
  });
clickRow("Stop · Hold · Resume", true);
await waitFor(() => rowIndex(DID_IDLE) != null, "held agent node never appeared");
out.navGroupTree = spacer.innerHTML;
clickRow(DID_IDLE, false);
const inspector = container.querySelector("[data-inspector]");
await waitFor(() => inspector.innerHTML.includes("H4"), "holder inspector never rendered");
out.navIdle = inspector.innerHTML;
nav.destroy();
process.stdout.write(JSON.stringify(out));
"""


def test_timeline_and_navigator_state_the_same_lifecycle(tmp_path):
    pkg = _module_dir(tmp_path)
    out = _run(pkg, "mounted.mjs", _MOUNTED)

    # Timeline: stopped vs pending, painted and inspectable.
    assert "turn 1 · 4m 0s · stopped" in out["tlTexts"]
    assert "turn 2 · 4m 0s · stopped (receipt pending)" in out["tlTexts"]
    assert 'data-receipt-id="R1"' in out["tlTurn1"]
    assert 'data-lifecycle-state="stopped"' in out["tlTurn1"]
    assert 'data-lifecycle-state="stopped_pending"' in out["tlTurn2"]
    assert "data-receipt-id" not in out["tlTurn2"]
    # The held agent with no spans has a lane and a resting state.
    assert "⏸ did:key:idle" in out["tlTexts"]
    assert "Stop · Hold · Resume" in " ".join(out["tlTexts"])
    assert "held (agent)" in out["tlIdle"] and 'data-receipt-id="H4"' in out["tlIdle"]

    # Navigator: the SAME state and the SAME receipt for the same turns.
    assert 'data-lifecycle-state="stopped"' in out["navTurn1"]["inspector"]
    assert 'data-receipt-id="R1"' in out["navTurn1"]["inspector"]
    assert 'data-lifecycle-state="stopped"' in out["navTurn1"]["tree"]
    assert 'data-lifecycle-state="stopped_pending"' in out["navTurn2"]["inspector"]
    assert "stopped (receipt pending)" in out["navTurn2"]["tree"]
    assert "data-receipt-id" not in out["navTurn2"]["inspector"]
    # Turn 2 was never selected, so its trace never loaded: the Turn level's own
    # outcome read is what lets its row already say "receipt pending".
    assert "stopped (receipt pending)" in out["navTurn1"]["tree"]
    # The shared span detail's `state` IS the lifecycle, in both views — never
    # "completed"/"point event" beside a Stop.
    state_row = (
        '<span class="obs-detail__key">state</span>'
        '<span class="obs-detail__value">stopped</span>'
    )
    assert state_row in out["tlTurn1"]
    assert state_row in out["navTurn1"]["inspector"]
    assert "did:key:idle" in out["navGroupTree"]
    assert "held (agent)" in out["navIdle"] and 'data-receipt-id="H4"' in out["navIdle"]

    # Privacy: rendered escaped; never persisted into the view state.
    for html in (out["tlTurn1"], out["navTurn1"]["inspector"], out["navIdle"]):
        assert "<script>" not in html and "&lt;script&gt;" in html
    assert "sovereign-secret-actor" not in out["tlState"]
    assert "private operator reason" not in out["tlState"]


# Phoenix down, a receipt history deeper than one poll, and a session with more
# Turns than one outcome page: the boot and paging paths the mounted views take
# (#118 self-review).
_MOUNTED_EDGES = r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";
import {
  NOW, DID_CLAW, DID_IDLE, turnSpans, stopReceipt, holdReceipt, latch, rootId, summariesByParent,
} from "./fixture.mjs";

installFakeDom();
const MIN = 60_000;
const out = {};
const idleHold = [holdReceipt("H4", { action: "hold", scope: "agent", target: DID_IDLE, at: NOW - 25 * MIN, resulting: "H4" })];
const holdState = {
  can_hold: true,
  host_hold: null,
  agents: [{ agent_id: DID_IDLE, held: true, sources: ["agent"], agent_hold: latch("agent", DID_IDLE, "H4", NOW - 25 * MIN) }],
};
let stopRows = [];
let phoenixUp = false;
let gqlCalls = 0;
const stopCursors = [];
function stopPage(url) {
  const after = Number(url.searchParams.get("cursor") || 0);
  stopCursors.push(after);
  const limit = Number(url.searchParams.get("limit"));
  const rows = stopRows.filter((r) => r.feed_seq > after);
  const served = rows.slice(0, limit);
  return {
    schema_version: 1,
    receipts: served,
    next_cursor: rows.length > limit ? String(served[served.length - 1].feed_seq) : null,
  };
}
globalThis.__requestHost = async (path) => {
  const url = new URL(path, "http://host");
  if (url.pathname === "/api/host/phoenix/session") {
    if (!phoenixUp) throw Object.assign(new Error("phoenix disabled"), { status: 503 });
    return {};
  }
  if (url.pathname === "/api/host/stop/receipts") return stopPage(url);
  if (url.pathname === "/api/host/hold/receipts") return { schema_version: 1, receipts: idleHold, next_cursor: null };
  if (url.pathname === "/api/host/hold") return holdState;
  throw new Error(`unexpected host path ${path}`);
};

// A paging Phoenix double: `first`/`after`/sort are honored, so a session with
// more Turns than one page really is read a page at a time.
let spans = [];
const summaryReads = [];
const project = { id: "p1", name: "kestrel-fleet", traceCount: 1, endTime: new Date(NOW).toISOString() };
function paged(list, variables) {
  const sorted = [...list].sort((a, b) => Date.parse(a.startTime) - Date.parse(b.startTime) || (a.id < b.id ? -1 : 1));
  if (variables.sort && variables.sort.dir === "desc") sorted.reverse();
  const start = Number(variables.after || 0);
  const page = sorted.slice(start, start + variables.first);
  const hasNextPage = start + variables.first < sorted.length;
  return {
    node: {
      spans: {
        edges: page.map((node) => ({ node })),
        pageInfo: { hasNextPage, endCursor: hasNextPage ? String(start + variables.first) : null },
      },
    },
  };
}
globalThis.fetch = async (_url, options) => {
  gqlCalls += 1;
  const { query, variables = {} } = JSON.parse(options.body);
  let data;
  if (query.includes("NavigatorProjects")) data = { projects: { edges: [{ node: project }] } };
  else if (query.includes("NavigatorTraceSpans")) {
    data = { node: { trace: { spans: { edges: spans.filter((s) => s.context.traceId === variables.traceId).map((node) => ({ node })) } } } };
  } else if (query.includes("NavigatorSpanPage")) {
    const f = variables.filter || "";
    const summaries = summariesByParent(spans, f);
    if (summaries) {
      summaryReads.push((f.match(/'[0-9a-f]+'/g) || []).length);
      data = paged(summaries, variables);
    } else if (variables.rootOnly) data = paged(spans.filter((s) => !s.parentId), variables);
    else data = paged(spans, variables);
  } else throw new Error("unexpected GraphQL operation");
  return { status: 200, ok: true, json: async () => ({ data }) };
};

const { mount: mountTimeline } = await import("./timeline.js");
const { mount: mountNavigator } = await import("./navigator.js");

function navRows(container) {
  const spacer = container.querySelector("[data-spacer]");
  return spacer.innerHTML.split('<div class="obs-nav__row').slice(1);
}
function clickRow(container, label, caret) {
  const spacer = container.querySelector("[data-spacer]");
  const chunk = navRows(container).find((c) => c.includes(label));
  const index = /data-i="(\d+)"/.exec(chunk)[1];
  spacer.dispatch("click", {
    target: {
      closest: (sel) => (sel === "[data-i]" ? { dataset: { i: index } } : sel === "[data-caret]" && caret ? {} : null),
    },
  });
}

// ── 1. Phoenix down: the held agent still renders in both views ──
{
  const tl = new FakeElement("div");
  const timeline = mountTimeline(tl, { openTrace() {}, openNavigator() {} });
  const canvas = tl.querySelector("[data-canvas]");
  const texts = () => {
    const frame = canvas.context.frames[canvas.context.frames.length - 1];
    return frame ? frame.operations.filter((o) => o.type === "fillText").map((o) => String(o.args[0])) : [];
  };
  await waitFor(() => texts().some((t) => t.includes("⏸ did:key:idle")), "Timeline dropped the held agent with Phoenix down");
  out.tlDown = {
    texts: texts(),
    notice: tl.querySelector("[data-phoenix-notice]").innerHTML,
  };
  timeline.destroy();

  const nc = new FakeElement("div");
  const nav = mountNavigator(nc, {});
  await waitFor(() => navRows(nc).some((c) => c.includes("Stop · Hold · Resume")), "Navigator dropped the lifecycle group with Phoenix down");
  clickRow(nc, "Stop · Hold · Resume", true);
  await waitFor(() => navRows(nc).some((c) => c.includes(DID_IDLE)), "held agent node missing with Phoenix down");
  out.navDown = {
    tree: nc.querySelector("[data-spacer]").innerHTML,
    notice: nc.querySelector("[data-phoenix-notice]").innerHTML,
  };
  nav.destroy();
  out.gqlWhileDown = gqlCalls;
}

// ── 2. Navigator, Live off: a history past one poll's page cap is drained ──
{
  phoenixUp = true;
  stopRows = [];
  for (let i = 0; i < 1100; i++) {
    stopRows.push(stopReceipt(`D${i}`, { scope: "agent", at: NOW - 30 * MIN + i, target: DID_CLAW, outcomes: [["stopped", null]] }));
  }
  const base = stopRows[0].feed_seq - 1;
  stopCursors.length = 0;
  const nc = new FakeElement("div");
  const nav = mountNavigator(nc, {});
  const notices = nc.querySelector("[data-lifecycle-notices]");
  const groupMeta = () => (navRows(nc).find((c) => c.includes("Stop · Hold · Resume")) || "");
  await waitFor(() => groupMeta().includes("1,001 receipts") || groupMeta().includes("1001 receipts"), "first poll never rendered");
  out.drainPartial = { notices: notices.innerHTML, group: groupMeta() };
  await waitFor(() => groupMeta().includes("1,101 receipts") || groupMeta().includes("1101 receipts"), "Navigator left the receipt history partly unread");
  out.drainDone = { notices: notices.innerHTML, group: groupMeta(), cursors: stopCursors.map((c) => (c === 0 ? 0 : c - base)) };
  nav.destroy();
}

// ── 3. Navigator: older Turns keep their outcome past one outcome page ──
{
  stopRows = [];
  spans = [];
  for (let n = 1; n <= 150; n++) {
    spans.push(...turnSpans("Claw", DID_CLAW, n, NOW - 200 * MIN + n * MIN, n === 1 ? "stopped" : "completed"));
  }
  summaryReads.length = 0;
  const nc = new FakeElement("div");
  const nav = mountNavigator(nc, {
    revealTarget: { projectId: "p1", projectName: "kestrel-fleet", agentName: "Claw", sessionId: "sess-claw", traceId: "trace-Claw-2", spanId: rootId("Claw", 2) },
  });
  const inspector = nc.querySelector("[data-inspector]");
  await waitFor(() => inspector.innerHTML.includes(rootId("Claw", 2)), "Navigator never revealed turn 2");
  const turn1 = navRows(nc).find((c) => c.includes(">Claw turn 1<"));
  out.olderTurn = { row: turn1 || null, summaryReads: [...summaryReads] };
  nav.destroy();
}
process.stdout.write(JSON.stringify(out));
"""


def test_lifecycle_survives_phoenix_down_and_deep_paging(tmp_path):
    pkg = _module_dir(tmp_path)
    out = _run(pkg, "mounted_edges.mjs", _MOUNTED_EDGES)

    # 1. Phoenix down costs the spans, not the lifecycle — in both views. The
    # held lane is painted on the canvas, so the notice did not replace it.
    down = out["tlDown"]
    assert "⏸ did:key:idle" in down["texts"]
    assert "Phoenix is not running on this host" in down["notice"] and "data-retry" in down["notice"]
    assert "did:key:idle" in out["navDown"]["tree"]
    assert "held" in out["navDown"]["tree"]
    assert "Phoenix is not running on this host" in out["navDown"]["notice"]
    assert out["gqlWhileDown"] == 0

    # 2. The first poll reads five pages and says the history is partial; the
    # rest is read without Live or a Refresh, and the notice clears.
    assert "Stop receipts loading older history" in out["drainPartial"]["notices"]
    assert "loading older history" not in out["drainDone"]["notices"]
    assert out["drainDone"]["cursors"] == [0, 200, 400, 600, 800, 1000]

    # 3. Turn 1 sits outside the newest 100 summaries, and still shows its outcome;
    # the summaries are read for the loaded roots, at most one batch per read.
    assert out["olderTurn"]["row"] is not None
    assert "stopped (receipt pending)" in out["olderTurn"]["row"]
    assert out["olderTurn"]["summaryReads"] and max(out["olderTurn"]["summaryReads"]) <= 100
