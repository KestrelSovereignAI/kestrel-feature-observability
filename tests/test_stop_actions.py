"""Cooperative Stop actions in the Timeline and Navigator (#115).

The rules (kestrel-sovereign#3158, and the #115 ruling), executed under node
against the shipped ``stop_actions.js`` / ``timeline.js`` / ``navigator.js``:

1. The target is the turn span's own ``kestrel.turn_id``: never a span id,
   ``kestrel.orchestrator`` or display ancestry. No ``turn_id`` or no DID → no
   Stop; the control is shown disabled, with the reason.
2. One door: ``API.requestForAgent('/api/agent/stop', POST {turn_id,
   expected_agent_id, correlation_id}, agentName)``. A 409
   ``agent_identity_changed`` reads "agent identity changed; not stopped".
3. A ``correlation_id`` is minted once per (gesture, target) and reused on
   every retry of that target.
4. Stop is enabled only for a live turn. An ended turn keeps its state visible
   with Stop disabled, and ``already_complete`` is not a failure.
5. Selection is keyed by (agent DID, ``turn_id``) and survives redraws and polls
   that reorder turns. A confirmed multi-Stop sends exactly the selected
   targets, and every target keeps its own retryable outcome row.
6. No optimistic state: a reply never paints the turn as stopped. The views
   re-read spans and receipts as soon as a Stop settles.
"""

from __future__ import annotations

import pytest

from test_lifecycle_render_model import NODE, _module_dir, _run

pytestmark = pytest.mark.skipif(NODE is None, reason="node runtime not available")


# The door double shared by every harness below: records each call, and answers
# from `globalThis.__stopReplies` — agent name → a list of replies consumed in
# order (a reply is a payload, or `{error: {...}}` to reject with an ApiError).
_DOOR = r"""
export const doorCalls = [];
export function apiError(status, code, details = null) {
  const error = new Error(`HTTP ${status}`);
  error.status = status;
  error.code = code;
  error.body = { error: { code, message: "x", ...(details ? { details } : {}) } };
  return error;
}
export function outcome(disposition, receipt) {
  return { scope: "turn", disposition, agent_id: "did:key:x", receipt_id: receipt, detail: null };
}
globalThis.__stopReplies = {};
globalThis.__requestForAgent = async (path, options, agent) => {
  doorCalls.push({ path, method: options.method, headers: options.headers, body: JSON.parse(options.body), agent });
  const queue = globalThis.__stopReplies[agent] || [];
  const reply = queue.length > 1 ? queue.shift() : queue[0];
  if (reply && reply.error) throw reply.error;
  if (reply && reply.networkDown) throw new TypeError("Failed to fetch");
  return reply || { success: true, stop_outcomes: [outcome("stopped", "rcpt-default")] };
};
"""


_PURE = r"""
import { doorCalls, apiError, outcome } from "./door.mjs";
import {
  stopTargetOf, stopAvailability, isTurnSpan, readStopReply, readStopError,
  createStopController, handleStopBarClick, renderStopBarHtml, renderTurnStopHtml,
} from "./stop_actions.js";

const out = {};
const attrs = (kestrel, extra = {}) => ({ kestrel, ...extra });
const root = (kestrel, extra) => ({ name: "Claw turn 3", attributes: JSON.stringify(attrs({ marker: "start", ...kestrel }, extra)) });
const full = { agent_name: "Claw", agent_did: "did:key:claw", turn_id: "turn-3" };

// Rule 1: the span's own attributes, nothing else.
out.target = stopTargetOf(root(full));
out.coreTarget = stopTargetOf({ name: "agent.process_input", attributes: { kestrel: { agent_name: "Emma", turn_id: "t-9" }, agent: { did: "did:key:emma" } } });
out.isTurn = {
  root: isTurnSpan(root(full)),
  core: isTurnSpan({ name: "agent.process_input_streaming", attributes: "{}" }),
  tool: isTurnSpan({ name: "Bash", attributes: JSON.stringify(attrs({ turn_id: "turn-3" }, { openinference: { span: { kind: "TOOL" } } })) }),
  started: isTurnSpan({ name: "Bash (started)", attributes: JSON.stringify(attrs({ marker: "start", turn_id: "turn-3" })) }),
};
const avail = (kestrel, opts = { open: true }) => stopAvailability(stopTargetOf(root(kestrel)), opts);
out.avail = {
  live: avail(full),
  noTurnId: avail({ ...full, turn_id: undefined }),
  // An orchestrator names who launched the run — never whom to stop.
  orchestratorOnly: avail({ agent_name: "Claw", turn_id: "turn-3", orchestrator: "did:key:boss" }),
  placeholderDid: avail({ ...full, agent_did: "local-agent" }),
  noName: avail({ ...full, agent_name: undefined }),
  closed: avail(full, { open: false }),
  outcome: avail(full, { open: true, lifecycle: { spanOutcome: "completed", label: "completed", state: "completed" } }),
  receiptStopped: avail(full, { open: true, lifecycle: { spanOutcome: null, label: "stopped", state: "stopped" } }),
  unreachable: avail(full, { open: true, lifecycle: { spanOutcome: null, label: "stop unreachable", state: "unreachable" } }),
};

// Rule 2/4: replies and errors.
out.replies = {
  stopped: readStopReply({ stop_outcomes: [outcome("stopped", "r1")] }),
  complete: readStopReply({ stop_outcomes: [outcome("already_complete", "r2")] }),
  empty: readStopReply({ success: true }),
  identity: readStopError(apiError(409, "agent_identity_changed")),
  unreachable: readStopError(apiError(503, "stop_not_confirmed", [outcome("unreachable", "r3")])),
  refused: readStopError(apiError(503, "stop_not_confirmed", [outcome("refused", "r4")])),
  server: readStopError(apiError(500, "internal_error")),
  network: readStopError(new TypeError("Failed to fetch")),
};

// The controller, against the REAL door (./phoenix.js requestAgentStop).
let settled = 0;
let n = 0;
const ctl = createStopController({ concurrency: 2, mintId: () => `cid-${++n}`, onSettled: () => settled++ });
const target = (agent, turn) => stopTargetOf(root({ agent_name: agent, agent_did: `did:key:${agent.toLowerCase()}`, turn_id: turn }));

// A single Stop: the door, its path, its body.
globalThis.__stopReplies.Claw = [{ error: apiError(503, "stop_not_confirmed", [outcome("unreachable", null)]) }, { success: true, stop_outcomes: [outcome("stopped", "rc")] }];
await ctl.stopOne(target("Claw", "turn-3"));
out.single = { calls: doorCalls.splice(0), attempt: { ...ctl.attempt(target("Claw", "turn-3").key) }, settled };
// Rule 3: a retry reuses the attempt's correlation id; a NEW gesture mints one.
await ctl.retry(target("Claw", "turn-3").key);
out.retry = { calls: doorCalls.splice(0), attempt: { ...ctl.attempt(target("Claw", "turn-3").key) } };
out.retryStoppedIsNoop = ctl.retry(target("Claw", "turn-3").key) === null;
await ctl.stopOne(target("Claw", "turn-3"));
out.newGesture = doorCalls.splice(0).map((c) => c.body.correlation_id);

// Rule 5: multi-select. Selecting twice is one entry; a keyless target is refused.
globalThis.__stopReplies = {
  A: [{ success: true, stop_outcomes: [outcome("stopped", "ra")] }],
  B: [{ success: true, stop_outcomes: [outcome("already_complete", "rb")] }],
  C: [{ error: apiError(503, "stop_not_confirmed", [outcome("unreachable", null)]) }, { success: true, stop_outcomes: [outcome("stopped", "rc2")] }],
  D: [{ error: apiError(409, "agent_identity_changed") }],
  E: [{ networkDown: true }],
};
for (const a of ["A", "B", "C", "D", "E"]) ctl.select(target(a, `t-${a}`));
ctl.select(target("A", "t-A"));
ctl.select(stopTargetOf(root({ agent_name: "X", turn_id: "t-X" })));
ctl.deselect(target("E", "t-E").key);
out.selectedBefore = ctl.selected().map((t) => t.turnId);
// Confirm needs the confirm step first — like Stop All.
out.unconfirmed = ctl.confirmSelected() === null && doorCalls.length === 0;
ctl.select(target("E", "t-E"));
handleStopBarClick(ctl, { target: { closest: (s) => (s === "[data-stop-selected]" ? {} : null) } });
out.confirmHtml = renderStopBarHtml(ctl);
let inflight = 0;
let peak = 0;
const door = globalThis.__requestForAgent;
globalThis.__requestForAgent = async (...args) => {
  inflight++;
  peak = Math.max(peak, inflight);
  await new Promise((r) => setTimeout(r, 5));
  try {
    return await door(...args);
  } finally {
    inflight--;
  }
};
const settledBefore = settled;
await ctl.confirmSelected();
out.multi = {
  calls: doorCalls.splice(0).map((c) => [c.agent, c.body.turn_id, c.body.expected_agent_id]),
  peak,
  settledOnce: settled - settledBefore,
  selectedAfter: ctl.selected().length,
  rows: ctl.batch().map((a) => [a.target.turnId, a.status, a.detail]),
  html: renderStopBarHtml(ctl),
};
const cidC = ctl.attempt(target("C", "t-C").key).correlationId;
// Retry from the bar: only the failed row's own id.
handleStopBarClick(ctl, { target: { closest: (s) => (s === "[data-stop-retry]" ? { dataset: { stopRetry: target("C", "t-C").key } } : null) } });
await new Promise((r) => setTimeout(r, 30));
out.barRetry = { calls: doorCalls.splice(0).map((c) => [c.agent, c.body.correlation_id]), cidC, status: ctl.attempt(target("C", "t-C").key).status };
out.identityRetryNoop = ctl.retry(target("D", "t-D").key) === null;
out.completeRetryNoop = ctl.retry(target("B", "t-B").key) === null;
// Rule 5: a target already queued or in flight is never sent twice — a new
// gesture joins the reserved attempt instead of minting a second correlation id.
const ok = (r) => [{ success: true, stop_outcomes: [outcome("stopped", r)] }];
globalThis.__stopReplies = { F: ok("rf"), G: ok("rg"), H: ok("rh"), I: ok("ri") };
const ctl2 = createStopController({ concurrency: 1, mintId: () => `dup-${++n}` });
const inFlight = ctl2.stopOne(target("F", "t-F"));
ctl2.select(target("F", "t-F"));
ctl2.select(target("G", "t-G"));
ctl2.askConfirm();
const joined = ctl2.confirmSelected();
await Promise.all([inFlight, joined]);
out.dupInFlight = {
  calls: doorCalls.splice(0).map((c) => [c.agent, c.body.correlation_id]),
  batch: ctl2.batch().map((a) => [a.target.turnId, a.correlationId, a.status]),
};
ctl2.select(target("H", "t-H"));
ctl2.select(target("I", "t-I"));
ctl2.askConfirm();
const queuedRun = ctl2.confirmSelected();
out.dupQueuedRefused = ctl2.stopOne(target("I", "t-I")) === null;
await queuedRun;
out.dupQueuedCalls = doorCalls.splice(0).map((c) => c.agent).sort();
// The control shows the reason and the reply, escaped.
out.controlDisabled = renderTurnStopHtml(ctl, target("B", "t-B"), { enabled: false, reason: "turn has ended (<b>completed</b>)" });
process.stdout.write(JSON.stringify(out));
"""


def _pkg(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "door.mjs").write_text(_DOOR, encoding="utf-8")
    return pkg


def test_stop_target_door_correlation_and_multi_select(tmp_path):
    out = _run(_pkg(tmp_path), "pure.mjs", _PURE)

    # Rule 1: the target is the span's own address and DID — core's `agent.did`
    # spelling included — and only a turn's own span is a turn.
    assert out["target"] == {
        "key": '["did:key:claw","turn-3"]',
        "turnId": "turn-3",
        "agentDid": "did:key:claw",
        "agentName": "Claw",
        "label": "Claw turn 3",
    }
    assert out["coreTarget"]["agentDid"] == "did:key:emma"
    assert out["coreTarget"]["key"] == '["did:key:emma","t-9"]'
    assert out["isTurn"] == {"root": True, "core": True, "tool": False, "started": False}
    avail = out["avail"]
    assert avail["live"] == {"enabled": True, "reason": None}
    assert avail["noTurnId"] == {
        "enabled": False,
        "reason": "no canonical turn address (kestrel.turn_id) on this turn",
    }
    assert avail["orchestratorOnly"] == {"enabled": False, "reason": "no agent DID recorded on this turn"}
    assert avail["placeholderDid"]["enabled"] is False
    assert avail["noName"]["reason"] == "no agent name recorded to route the Stop to"
    # Rule 4: an ended turn declines Stop and says why; a Stop that could not
    # reach the turn leaves it live, so Stop stays available.
    assert avail["closed"] == {"enabled": False, "reason": "turn has ended"}
    assert avail["outcome"] == {"enabled": False, "reason": "turn has ended (completed)"}
    assert avail["receiptStopped"] == {"enabled": False, "reason": "turn has ended (stopped)"}
    assert avail["unreachable"]["enabled"] is True

    replies = out["replies"]
    assert replies["stopped"] == {"status": "stopped", "detail": None, "receiptIds": ["r1"]}
    assert replies["complete"]["status"] == "already_complete"
    assert replies["empty"]["status"] == "error"
    assert replies["identity"]["status"] == "identity_changed"
    assert replies["unreachable"] == {"status": "unreachable", "detail": None, "receiptIds": ["r3"]}
    assert replies["refused"]["status"] == "refused"
    assert replies["server"] == {"status": "error", "detail": "HTTP 500 · internal_error", "receiptIds": []}
    assert replies["network"] == {"status": "error", "detail": "request failed", "receiptIds": []}

    # Rule 2: the one door, routed by the span's agent name, with the DID check.
    single = out["single"]
    assert single["calls"] == [
        {
            "path": "/api/agent/stop",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": {"turn_id": "turn-3", "expected_agent_id": "did:key:claw", "correlation_id": "cid-1"},
            "agent": "Claw",
        }
    ]
    assert single["attempt"]["status"] == "unreachable"
    assert single["settled"] == 1
    # Rule 3: the retry replays the same correlation id; a new gesture mints one.
    assert [c["body"]["correlation_id"] for c in out["retry"]["calls"]] == ["cid-1"]
    assert out["retry"]["attempt"]["status"] == "stopped"
    assert out["retryStoppedIsNoop"] is True
    # A target already in flight joins a batch under its reserved attempt; a
    # target still queued in a batch refuses a second single Stop.
    dup = out["dupInFlight"]
    assert sorted(dup["calls"]) == [["F", dup["batch"][0][1]], ["G", dup["batch"][1][1]]]
    assert [row[0] for row in dup["batch"]] == ["t-F", "t-G"]
    assert all(row[2] == "stopped" for row in dup["batch"])
    assert out["dupQueuedRefused"] is True
    assert out["dupQueuedCalls"] == ["H", "I"]
    assert out["newGesture"] == ["cid-2"]

    # Rule 5: exactly the selected targets, once each, after the confirm step.
    assert out["selectedBefore"] == ["t-A", "t-B", "t-C", "t-D"]
    assert out["unconfirmed"] is True
    assert "Stop 5 turns selected?" in out["confirmHtml"]
    assert "data-stop-confirm" in out["confirmHtml"]
    multi = out["multi"]
    assert sorted(multi["calls"]) == [
        ["A", "t-A", "did:key:a"],
        ["B", "t-B", "did:key:b"],
        ["C", "t-C", "did:key:c"],
        ["D", "t-D", "did:key:d"],
        ["E", "t-E", "did:key:e"],
    ]
    assert multi["peak"] == 2  # bounded concurrency
    assert multi["settledOnce"] == 1
    assert multi["selectedAfter"] == 0
    # Every target keeps its own outcome — partial results never collapse.
    assert multi["rows"] == [
        ["t-A", "stopped", None],
        ["t-B", "already_complete", None],
        ["t-C", "unreachable", None],
        ["t-D", "identity_changed", None],
        ["t-E", "error", "request failed"],
    ]
    html = multi["html"]
    assert "Stop request: agent identity changed; not stopped" in html
    assert "Stop request: already complete" in html
    # Only the failed and unreachable rows offer a retry.
    assert html.count("data-stop-retry=") == 2
    assert out["barRetry"]["calls"] == [["C", out["barRetry"]["cidC"]]]
    assert out["barRetry"]["status"] == "stopped"
    assert out["identityRetryNoop"] is True
    assert out["completeRetryNoop"] is True

    control = out["controlDisabled"]
    assert "data-stop-turn disabled" in control
    assert "Stop unavailable: turn has ended (&lt;b&gt;completed&lt;/b&gt;)" in control
    assert 'data-stop-result="already_complete"' in control


# The mounted views' shared Phoenix + host doubles: every host read is logged,
# the receipt feed serves `receipts`, and Phoenix serves `spans`.
_HOSTS = r"""
import { NOW, turnSpans, summariesByParent } from "./fixture.mjs";
export const MIN = 60_000;
export const spans = [];
export const receipts = [];
export const hostReads = [];
export const gqlReads = [];
// A live turn: its root only — no summary yet.
export function liveTurn(agent, did, n, start, session, { dropTurnId = false } = {}) {
  const [root] = turnSpans(agent, did, n, start, null, session);
  if (dropTurnId) {
    const a = JSON.parse(root.attributes);
    delete a.kestrel.turn_id;
    root.attributes = JSON.stringify(a);
  }
  return root;
}
globalThis.__requestHost = async (path) => {
  const url = new URL(path, "http://host");
  hostReads.push(url.pathname);
  if (url.pathname === "/api/host/phoenix/session") return {};
  if (url.pathname === "/api/host/stop/receipts") return { schema_version: 1, receipts, next_cursor: null };
  if (url.pathname === "/api/host/hold/receipts") return { schema_version: 1, receipts: [], next_cursor: null };
  if (url.pathname === "/api/host/hold") return { can_hold: true, host_hold: null, agents: [] };
  throw new Error(`unexpected host path ${path}`);
};
const project = { id: "p1", name: "kestrel-fleet", traceCount: 2, endTime: new Date(NOW).toISOString() };
const edges = (list) => ({ node: { spans: { edges: list.map((node) => ({ node })), pageInfo: { hasNextPage: false, endCursor: null } } } });
globalThis.fetch = async (_url, options) => {
  const { query, variables = {} } = JSON.parse(options.body);
  gqlReads.push(query.includes("NavigatorProjects") ? "projects" : "spans");
  let data;
  if (query.includes("NavigatorProjects")) data = { projects: { edges: [{ node: project }] } };
  else if (query.includes("NavigatorTraceSpans")) {
    data = { node: { trace: { spans: { edges: spans.filter((s) => s.context.traceId === variables.traceId).map((node) => ({ node })) } } } };
  } else {
    const summaries = summariesByParent(spans, variables.filter || "");
    if (summaries) data = edges(summaries);
    else if (variables.rootOnly) data = edges(spans.filter((s) => !s.parentId));
    else data = edges(spans);
  }
  return { status: 200, ok: true, json: async () => ({ data }) };
};
// A synthetic click whose target sits inside the given `data-*` controls.
export function clickOn(el, attrs) {
  el.dispatch("click", {
    target: {
      closest: (sel) => {
        const m = /^\[([a-z-]+)\]$/.exec(sel);
        if (!m || !(m[1] in attrs)) return null;
        return { dataset: attrs[m[1]] || {} };
      },
    },
  });
}
"""


_MOUNTED_TIMELINE = r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";
import { NOW, turnSpans, stopReceipt, rootId } from "./fixture.mjs";
import { doorCalls, apiError, outcome } from "./door.mjs";
import { MIN, spans, receipts, hostReads, liveTurn, clickOn } from "./hosts.mjs";

installFakeDom();
spans.push(
  ...turnSpans("Claw", "did:key:claw", 4, NOW - 10 * MIN, "completed", "sess-claw"),
  liveTurn("Claw", "did:key:claw", 5, NOW - 3 * MIN, "sess-claw"),
  liveTurn("Emma", "did:key:emma", 1, NOW - 2 * MIN, "sess-emma"),
  liveTurn("Ghost", null, 1, NOW - 4 * MIN, "sess-ghost"),
  liveTurn("Nott", "did:key:nott", 1, NOW - 5 * MIN, "sess-nott", { dropTurnId: true }),
);
const out = {};
const { mount } = await import("./timeline.js");
const tl = new FakeElement("div");
const timeline = mount(tl, { openTrace() {}, openNavigator() {} });
const canvas = tl.querySelector("[data-canvas]");
const pop = tl.querySelector("[data-pop]");
const bar = tl.querySelector("[data-stop-bar]");
const frame = () => canvas.context.frames[canvas.context.frames.length - 1];
const texts = () => (frame() ? frame().operations.filter((o) => o.type === "fillText") : []);
const label = (text) => texts().find((o) => String(o.args[0]) === text);
const settle = () => new Promise((r) => setTimeout(r, 40));
await waitFor(() => label("Emma turn 1") && label("Claw turn 5"), "Timeline never painted the live turns");
const click = async (text, wait) => {
  const op = label(text);
  const pointer = { button: 0, pointerId: 1, clientX: op.args[1] + 4, clientY: op.args[2], offsetX: op.args[1] + 4, offsetY: op.args[2] };
  canvas.dispatch("pointerdown", pointer);
  canvas.dispatch("pointerup", pointer);
  await waitFor(() => !pop.hidden && pop.innerHTML.includes(wait), `popover for ${text} did not open`);
  return pop.innerHTML;
};
// Outlines drawn for selected turns, and which painted labels each encloses.
const outlined = () =>
  frame().operations
    .filter((o) => o.type === "strokeRect" && o.strokeStyle === "#f472b6")
    .map((o) => {
      const [x, y, w, h] = o.args;
      return texts()
        .filter((t) => t.args[1] >= x && t.args[1] <= x + w && t.args[2] >= y && t.args[2] <= y + h)
        .map((t) => String(t.args[0]))
        .sort()
        .join("|");
    })
    .sort();

// Rule 4 / 1: ended turns and turns without an address show Stop disabled,
// with the reason — and the ended turn's lifecycle stays visible.
out.completed = await click("turn 4 · 4m 0s", "data-stop-control");
out.ghost = await click("Ghost turn 1", "data-stop-control");
out.nott = await click("Nott turn 1", "data-stop-control");
out.live = await click("Claw turn 5", "data-stop-control");

// Rule 5: select two live turns from their popovers.
clickOn(pop, { "data-stop-select": {} });
await click("Emma turn 1", "data-stop-control");
clickOn(pop, { "data-stop-select": {} });
await waitFor(() => bar.innerHTML.includes('data-stop-count="2"'), "bar never counted the selection");
out.emmaSelected = pop.innerHTML;
await settle();
out.outlinesBefore = outlined();
out.emmaYBefore = label("Emma turn 1").args[2];
// A poll that reorders the lanes (Abe sorts first) never moves the selection.
spans.push(liveTurn("Abe", "did:key:abe", 1, NOW - 2.5 * MIN, "sess-abe"));
tl.querySelector("[data-refresh]").dispatch("click");
await waitFor(() => label("Abe turn 1"), "Abe never landed");
await settle();
out.emmaYAfter = label("Emma turn 1").args[2];
out.outlinesAfter = outlined();
out.barSelected = bar.innerHTML;

// The confirmed multi-Stop: exactly the selected targets, with partial replies.
globalThis.__stopReplies = {
  Claw: [{ error: apiError(503, "stop_not_confirmed", [outcome("unreachable", null)]) }, { success: true, stop_outcomes: [outcome("stopped", "R5")] }],
  Emma: [{ success: true, stop_outcomes: [outcome("already_complete", "RE1")] }],
  Abe: [{ error: apiError(409, "agent_identity_changed") }],
};
clickOn(bar, { "data-stop-selected": {} });
await waitFor(() => bar.innerHTML.includes("data-stop-confirm"), "confirm step never shown");
out.confirm = bar.innerHTML;
const readsBefore = hostReads.filter((p) => p === "/api/host/stop/receipts").length;
clickOn(bar, { "data-stop-confirm": {} });
await waitFor(() => bar.innerHTML.includes("Stop results") && !bar.innerHTML.includes('data-stop-result="pending"'), "multi-Stop never settled");
out.multiCalls = doorCalls.splice(0).map((c) => ({ agent: c.agent, path: c.path, body: c.body }));
out.barResults = bar.innerHTML;
// Rule 6: the settle re-reads the receipts at once …
await waitFor(() => hostReads.filter((p) => p === "/api/host/stop/receipts").length > readsBefore, "no refresh after the Stop");
await settle();
// … and the reply paints nothing: the turn is not stopped until its span or a
// receipt says so.
out.labelsAfterReply = texts().map((o) => String(o.args[0])).filter((t) => t.startsWith("Claw turn 5") || t.startsWith("Emma turn 1"));
out.clawAfterReply = await click("Claw turn 5", "data-stop-control");

// Retry the unreachable row from the bar: the same correlation id.
const clawKey = JSON.stringify(["did:key:claw", "Claw#5"]);
clickOn(bar, { "data-stop-retry": { stopRetry: clawKey } });
await waitFor(() => doorCalls.length === 1 && !bar.innerHTML.includes('data-stop-result="pending"'), "retry never settled");
out.retryCall = doorCalls.splice(0)[0].body;
out.barAfterRetry = bar.innerHTML;
// The host's receipt lands: now — and only now — the turn is stopped.
receipts.push(stopReceipt("R5", { scope: "turn", at: NOW - MIN, target: "did:key:claw", trace: "trace-Claw-5", span: rootId("Claw", 5), outcomes: [["stopped", "did:key:claw"]] }));
tl.querySelector("[data-refresh]").dispatch("click");
await waitFor(() => texts().some((o) => String(o.args[0]) === "Claw turn 5 · stopped"), "receipt never stopped the turn");
out.clawStopped = await click("Claw turn 5 · stopped", "data-stop-control");

// A single Stop from the popover, whose agent identity changed.
await click("Abe turn 1", "data-stop-control");
clickOn(pop, { "data-stop-turn": {} });
await waitFor(() => pop.innerHTML.includes('data-stop-result="identity_changed"'), "identity change never shown");
out.abeCall = doorCalls.splice(0).map((c) => ({ agent: c.agent, body: c.body }));
out.abe = pop.innerHTML;
out.state = JSON.stringify(timeline.getState());
timeline.destroy();
process.stdout.write(JSON.stringify(out));
"""


def _mounted_pkg(tmp_path):
    pkg = _pkg(tmp_path)
    (pkg / "hosts.mjs").write_text(_HOSTS, encoding="utf-8")
    return pkg


def test_timeline_popover_stop_selection_and_bar(tmp_path):
    out = _run(_mounted_pkg(tmp_path), "timeline_stop.mjs", _MOUNTED_TIMELINE)

    # An ended turn: Stop disabled with the reason, its lifecycle still shown.
    assert "data-stop-turn disabled" in out["completed"]
    assert "Stop unavailable: turn has ended (completed)" in out["completed"]
    assert 'data-lifecycle-state="completed"' in out["completed"]
    assert "data-stop-select aria-pressed=\"false\" disabled" in out["completed"]
    # No DID, no turn_id: no Stop, with the reason.
    assert "data-stop-turn disabled" in out["ghost"]
    assert "Stop unavailable: no agent DID recorded on this turn" in out["ghost"]
    assert "Stop unavailable: no canonical turn address (kestrel.turn_id) on this turn" in out["nott"]
    # A live turn: Stop enabled.
    assert "data-stop-turn>Stop turn" in out["live"]
    assert "data-stop-reason" not in out["live"]

    assert 'aria-pressed="true"' in out["emmaSelected"] and "Selected for Stop ✓" in out["emmaSelected"]
    # The selection outlines exactly the selected turns, and follows them when
    # a poll reorders the lanes.
    assert out["outlinesBefore"] == ["Claw turn 5", "Emma turn 1"]
    assert out["emmaYAfter"] != out["emmaYBefore"]
    assert out["outlinesAfter"] == ["Claw turn 5", "Emma turn 1"]
    assert 'data-stop-count="2"' in out["barSelected"]
    assert "Abe turn 1" not in out["barSelected"]

    assert "Stop 2 turns selected?" in out["confirm"]
    calls = sorted(out["multiCalls"], key=lambda c: c["agent"])
    assert [(c["agent"], c["path"], c["body"]["turn_id"], c["body"]["expected_agent_id"]) for c in calls] == [
        ("Claw", "/api/agent/stop", "Claw#5", "did:key:claw"),
        ("Emma", "/api/agent/stop", "Emma#1", "did:key:emma"),
    ]
    results = out["barResults"]
    assert 'data-stop-result="unreachable"' in results
    assert 'data-stop-result="already_complete"' in results
    assert results.count("data-stop-retry=") == 1
    # No optimistic state.
    assert out["labelsAfterReply"] == ["Claw turn 5", "Emma turn 1"]
    assert 'data-lifecycle-state="stopped"' not in out["clawAfterReply"]
    assert 'data-stop-result="unreachable"' in out["clawAfterReply"]

    claw_cid = next(c for c in out["multiCalls"] if c["agent"] == "Claw")["body"]["correlation_id"]
    assert out["retryCall"] == {"turn_id": "Claw#5", "expected_agent_id": "did:key:claw", "correlation_id": claw_cid}
    assert 'data-stop-result="stopped"' in out["barAfterRetry"]
    # The receipt, not the reply, stops the turn; Stop is then declined.
    assert 'data-lifecycle-state="stopped"' in out["clawStopped"]
    assert "Stop unavailable: turn has ended (stopped)" in out["clawStopped"]

    assert out["abeCall"] == [
        {"agent": "Abe", "body": {"turn_id": "Abe#1", "expected_agent_id": "did:key:abe", "correlation_id": out["abeCall"][0]["body"]["correlation_id"]}}
    ]
    assert "Stop request: agent identity changed; not stopped" in out["abe"]
    assert "data-stop-retry" not in out["abe"]
    # Selection and replies never enter the persisted view state.
    assert "Claw#5" not in out["state"] and "did:key" not in out["state"]


_MOUNTED_NAVIGATOR = r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";
import { NOW, turnSpans, rootId } from "./fixture.mjs";
import { doorCalls, apiError, outcome } from "./door.mjs";
import { MIN, spans, hostReads, liveTurn, clickOn } from "./hosts.mjs";

installFakeDom();
spans.push(
  ...turnSpans("Claw", "did:key:claw", 4, NOW - 10 * MIN, "completed", "sess-claw"),
  liveTurn("Claw", "did:key:claw", 5, NOW - 3 * MIN, "sess-claw"),
);
const out = {};
const { mount } = await import("./navigator.js");
const nc = new FakeElement("div");
const nav = mount(nc, {
  revealTarget: { projectId: "p1", projectName: "kestrel-fleet", agentName: "Claw", sessionId: "sess-claw", traceId: "trace-Claw-5", spanId: rootId("Claw", 5) },
});
const inspector = nc.querySelector("[data-inspector]");
const spacer = nc.querySelector("[data-spacer]");
const bar = nc.querySelector("[data-stop-bar]");
const settle = () => new Promise((r) => setTimeout(r, 40));
const rowOf = (label) => spacer.innerHTML.split('<div class="obs-nav__row').find((c) => c.includes(`>${label}<`)) || null;
const rowIndex = (label) => {
  const row = rowOf(label);
  return row ? /data-i="(\d+)"/.exec(row)[1] : null;
};
const checkRow = (label) =>
  spacer.dispatch("click", {
    target: {
      closest: (sel) =>
        sel === "[data-i]" ? { dataset: { i: rowIndex(label) } } : sel === "[data-stop-check]" ? {} : null,
    },
  });
await waitFor(() => inspector.innerHTML.includes("data-stop-control") && rowOf("Claw turn 5"), "Navigator never inspected the live turn");
await settle();
out.inspectLive = inspector.innerHTML;
out.rowLive = rowOf("Claw turn 5");
out.rowDone = rowOf("Claw turn 4");

// Rule 5: select from the row checkbox — its own row, wherever focus is.
nc.querySelector("[data-scroll]").dispatch("keydown", { key: "ArrowUp" });
checkRow("Claw turn 5");
await waitFor(() => bar.innerHTML.includes('data-stop-count="1"'), "bar never counted the row selection");
await settle();
out.indexBefore = rowIndex("Claw turn 5");
out.inspectSelected = inspector.innerHTML;
// A refresh that inserts an EARLIER turn reorders the rows: the check stays on
// the same turn, never on whatever now sits at its old position.
spans.push(...turnSpans("Claw", "did:key:claw", 3, NOW - 20 * MIN, "completed", "sess-claw"));
nc.querySelector("[data-refresh]").dispatch("click");
await waitFor(() => rowOf("Claw turn 3"), "the earlier turn never landed");
await settle();
out.indexAfter = rowIndex("Claw turn 5");
out.checked = spacer.innerHTML
  .split('<div class="obs-nav__row')
  .filter((c) => c.includes('aria-checked="true"'))
  .map((c) => /obs-nav__label" title="([^"]*)"/.exec(c)[1]);

// A single Stop from the inspector: unreachable, then retried under its id.
globalThis.__stopReplies = {
  Claw: [{ error: apiError(503, "stop_not_confirmed", [outcome("unreachable", null)]) }, { success: true, stop_outcomes: [outcome("stopped", "R5")] }],
};
const readsBefore = hostReads.filter((p) => p === "/api/host/stop/receipts").length;
clickOn(inspector, { "data-stop-turn": {} });
await waitFor(() => inspector.innerHTML.includes('data-stop-result="unreachable"'), "inspector never showed the reply");
await waitFor(() => hostReads.filter((p) => p === "/api/host/stop/receipts").length > readsBefore, "no refresh after the Stop");
await settle();
out.inspectUnreachable = inspector.innerHTML;
clickOn(inspector, { "data-stop-retry": {} });
await waitFor(() => inspector.innerHTML.includes('data-stop-result="stopped"'), "retry never settled");
await settle();
out.calls = doorCalls.splice(0).map((c) => ({ agent: c.agent, path: c.path, body: c.body }));
out.inspectAfterReply = inspector.innerHTML;
out.rowAfterReply = rowOf("Claw turn 5");

// The span's own outcome lands: the turn has ended, and Stop is declined.
const [, summary] = turnSpans("Claw", "did:key:claw", 5, NOW - 3 * MIN, "stopped", "sess-claw");
spans.push(summary);
nc.querySelector("[data-refresh]").dispatch("click");
await waitFor(() => inspector.innerHTML.includes('data-lifecycle-state="stopped"'), "the span outcome never landed");
await settle();
out.inspectEnded = inspector.innerHTML;
out.rowEnded = rowOf("Claw turn 5");

// The still-selected, now-ended turn goes through the bar: already complete.
globalThis.__stopReplies = { Claw: [{ success: true, stop_outcomes: [outcome("already_complete", "R5")] }] };
clickOn(bar, { "data-stop-selected": {} });
clickOn(bar, { "data-stop-confirm": {} });
await waitFor(() => bar.innerHTML.includes('data-stop-result="already_complete"'), "bar never showed already complete");
out.barCalls = doorCalls.splice(0).map((c) => c.body.turn_id);
out.bar = bar.innerHTML;
nav.destroy();
process.stdout.write(JSON.stringify(out));
"""


def test_navigator_inspector_stop_row_selection_and_bar(tmp_path):
    out = _run(_mounted_pkg(tmp_path), "navigator_stop.mjs", _MOUNTED_NAVIGATOR)

    assert "data-stop-turn>Stop turn" in out["inspectLive"]
    assert 'data-stop-check role="checkbox" aria-checked="false"' in out["rowLive"]
    assert " disabled>" not in out["rowLive"]
    # The ended turn's row keeps its lifecycle and declines selection.
    assert 'data-lifecycle-state="completed"' in out["rowDone"]
    assert "Stop unavailable: turn has ended (completed)" in out["rowDone"]
    assert " disabled>☐" in out["rowDone"]

    assert "Selected for Stop ✓" in out["inspectSelected"]
    assert out["indexAfter"] != out["indexBefore"]
    assert out["checked"] == ["Claw turn 5"]

    assert 'data-stop-result="unreachable"' in out["inspectUnreachable"]
    assert "data-stop-retry" in out["inspectUnreachable"]
    assert [c["path"] for c in out["calls"]] == ["/api/agent/stop"] * 2
    assert out["calls"][0]["agent"] == "Claw"
    assert out["calls"][0]["body"]["turn_id"] == "Claw#5"
    assert out["calls"][0]["body"]["expected_agent_id"] == "did:key:claw"
    assert out["calls"][0]["body"]["correlation_id"] == out["calls"][1]["body"]["correlation_id"]
    # No optimistic state: the reply is shown as a reply; the turn is not stopped.
    assert 'data-stop-result="stopped"' in out["inspectAfterReply"]
    assert "data-lifecycle-state" not in out["inspectAfterReply"]
    assert "data-lifecycle-state" not in out["rowAfterReply"]

    assert 'data-lifecycle-state="stopped"' in out["inspectEnded"]
    assert "Stop unavailable: turn has ended (stopped)" in out["inspectEnded"]
    assert 'data-lifecycle-state="stopped"' in out["rowEnded"]
    # Still selected, so it can still be deselected — never silently dropped.
    assert 'aria-checked="true"' in out["rowEnded"]

    assert out["barCalls"] == ["Claw#5"]
    assert "Stop request: already complete" in out["bar"]
    assert "data-stop-retry" not in out["bar"]
