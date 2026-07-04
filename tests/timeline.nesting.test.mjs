/**
 * node --test suite for the fleet swimlane grouping + nesting logic.
 *
 * Imports the pure, DOM-free exports of `static/timeline.js` (buildLanes,
 * nestLanes, subtreeAgentNames, buildParentMap, lineageParents). The module must
 * import cleanly under node — its DOM/host code is guarded behind `window`.
 *
 * The first block ports the framework-agnostic `buildLanes` assertions from
 * kestrel-claws `dashboard/tests/timeline.test.ts`; the rest cover the new
 * sublane-nesting logic (issue #11).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildLanes,
  nestLanes,
  buildParentMap,
  lineageParents,
  subtreeAgentNames,
} from "../kestrel_feature_observability/static/timeline.js";

const now = Date.now();
const iso = (offsetMs) => new Date(now + offsetMs).toISOString();

const MOCK_EVENTS = [
  { event_id: "e1", timestamp: iso(-5000), agent_name: "Kestrel", session_id: "sess-A", event_type: "tool_call", tool_name: "Task" },
  { event_id: "e2", timestamp: iso(-4500), agent_name: "Kestrel", session_id: "sess-A", event_type: "tool_call", tool_name: "search" },
  { event_id: "e3", timestamp: iso(-4000), agent_name: "Kestrel", session_id: "sess-A", event_type: "tool_response", tool_name: "search", duration_ms: 500, success: true },
  { event_id: "e4", timestamp: iso(-3500), agent_name: "Kestrel", session_id: "sess-A", event_type: "tool_response", tool_name: "Task", duration_ms: 1500, success: true },
  { event_id: "e5", timestamp: iso(-2000), agent_name: "Talon", session_id: "sess-B", event_type: "error", tool_name: "deploy", success: false, error_message: "boom", metadata: { hook_event_type: "PreToolUse" } },
];

// ── Ported buildLanes tests (from claws timeline.test.ts) ─────

test("buildLanes: one lane per agent, sorted", () => {
  const lanes = buildLanes(MOCK_EVENTS);
  assert.deepEqual(lanes.map((l) => l.agentName), ["Kestrel", "Talon"]);
});

test("buildLanes: groups events into sessions per lane", () => {
  const lanes = buildLanes(MOCK_EVENTS);
  const kestrel = lanes.find((l) => l.agentName === "Kestrel");
  assert.equal(kestrel.sessions.length, 1);
  assert.equal(kestrel.sessions[0].sessionId, "sess-A");
});

test("buildLanes: pairs tool_call/tool_response and nests under Task", () => {
  const lanes = buildLanes(MOCK_EVENTS);
  const sess = lanes.find((l) => l.agentName === "Kestrel").sessions[0];
  assert.equal(sess.tasks.length, 1);
  assert.ok(sess.tasks[0].children.map((c) => c.toolName).includes("search"));
});

test("buildLanes: marks a session with an error as failed", () => {
  const lanes = buildLanes(MOCK_EVENTS);
  const talon = lanes.find((l) => l.agentName === "Talon");
  assert.equal(talon.sessions[0].status, "failed");
});

// ── Sublane nesting (issue #11) ───────────────────────────────

const TREE = {
  agent_name: "Meridian",
  children: [
    { agent_name: "Claw", children: [{ agent_name: "talon-job-1", children: [] }] },
    { agent_name: "sub-A", children: [] },
  ],
};

test("buildParentMap: flattens the agent tree into child→parent", () => {
  const parent = buildParentMap(TREE);
  assert.equal(parent.get("Claw"), "Meridian");
  assert.equal(parent.get("talon-job-1"), "Claw");
  assert.equal(parent.get("sub-A"), "Meridian");
  assert.equal(parent.get("Meridian"), undefined);
});

test("lineageParents: reads parent_agent/driven_by from event metadata", () => {
  const parent = lineageParents([
    { agent_name: "talon-job-9", metadata: { parent_agent: "Claw" } },
    { agent_name: "sub-Z", metadata: { driven_by: "Meridian" } },
    { agent_name: "Meridian", metadata: {} },
  ]);
  assert.equal(parent.get("talon-job-9"), "Claw");
  assert.equal(parent.get("sub-Z"), "Meridian");
  assert.equal(parent.get("Meridian"), undefined);
});

test("nestLanes: indents talon/subagent lanes under their driver via the tree", () => {
  const lanes = [
    { agentName: "Meridian", sessions: [] },
    { agentName: "Claw", sessions: [] },
    { agentName: "talon-job-1", sessions: [] },
    { agentName: "sub-A", sessions: [] },
  ];
  const nested = nestLanes(lanes, TREE, []);
  const byName = Object.fromEntries(nested.map((l) => [l.agentName, l]));

  assert.equal(byName["Meridian"].depth, 0);
  assert.equal(byName["Claw"].depth, 1);
  assert.equal(byName["Claw"].parentAgent, "Meridian");
  assert.equal(byName["talon-job-1"].depth, 2);
  assert.equal(byName["talon-job-1"].parentAgent, "Claw");
  assert.equal(byName["sub-A"].depth, 1);
});

test("nestLanes: emits parent immediately before its children (DFS order)", () => {
  const lanes = [
    { agentName: "talon-job-1", sessions: [] },
    { agentName: "Meridian", sessions: [] },
    { agentName: "Claw", sessions: [] },
  ];
  const order = nestLanes(lanes, TREE, []).map((l) => l.agentName);
  assert.deepEqual(order, ["Meridian", "Claw", "talon-job-1"]);
});

test("nestLanes: falls back to event lineage when the tree omits a child", () => {
  const lanes = [
    { agentName: "Claw", sessions: [] },
    { agentName: "talon-adhoc", sessions: [] },
  ];
  const events = [{ agent_name: "talon-adhoc", metadata: { driven_by: "Claw" } }];
  const nested = nestLanes(lanes, null, events);
  const talon = nested.find((l) => l.agentName === "talon-adhoc");
  assert.equal(talon.depth, 1);
  assert.equal(talon.parentAgent, "Claw");
});

test("nestLanes: re-attaches to nearest ancestor with a lane when parent has none", () => {
  // Claw has no lane of its own; talon-job-1 should hang off Meridian.
  const lanes = [
    { agentName: "Meridian", sessions: [] },
    { agentName: "talon-job-1", sessions: [] },
  ];
  const nested = nestLanes(lanes, TREE, []);
  const talon = nested.find((l) => l.agentName === "talon-job-1");
  assert.equal(talon.depth, 1);
});

test("nestLanes: agents with no parent are roots at depth 0", () => {
  const lanes = [
    { agentName: "Solo", sessions: [] },
    { agentName: "Other", sessions: [] },
  ];
  const nested = nestLanes(lanes, null, []);
  assert.ok(nested.every((l) => l.depth === 0));
  assert.deepEqual(nested.map((l) => l.agentName), ["Other", "Solo"]);
});

test("nestLanes: does not drop lanes on a lineage cycle", () => {
  const lanes = [
    { agentName: "A", sessions: [] },
    { agentName: "B", sessions: [] },
  ];
  const events = [
    { agent_name: "A", metadata: { parent_agent: "B" } },
    { agent_name: "B", metadata: { parent_agent: "A" } },
  ];
  const nested = nestLanes(lanes, null, events);
  assert.equal(nested.length, 2);
  assert.deepEqual(nested.map((l) => l.agentName).sort(), ["A", "B"]);
});

test("subtreeAgentNames: collects an agent plus all descendants", () => {
  const names = subtreeAgentNames(TREE, "Claw");
  assert.deepEqual([...names].sort(), ["Claw", "talon-job-1"]);

  const root = subtreeAgentNames(TREE, "Meridian");
  assert.deepEqual([...root].sort(), ["Claw", "Meridian", "sub-A", "talon-job-1"]);

  assert.equal(subtreeAgentNames(TREE, "nope").size, 0);
});

test("buildLanes + nestLanes integration nests a talon sublane under Claw", () => {
  const events = [
    { event_id: "k1", timestamp: iso(-5000), agent_name: "Claw", session_id: "s1", event_type: "tool_call", tool_name: "Task" },
    { event_id: "k2", timestamp: iso(-4000), agent_name: "Claw", session_id: "s1", event_type: "tool_response", tool_name: "Task", success: true },
    { event_id: "t1", timestamp: iso(-4500), agent_name: "talon-job-1", session_id: "s2", event_type: "tool_call", tool_name: "deploy", metadata: { parent_agent: "Claw" } },
    { event_id: "t2", timestamp: iso(-4000), agent_name: "talon-job-1", session_id: "s2", event_type: "tool_response", tool_name: "deploy", success: true, metadata: { parent_agent: "Claw" } },
  ];
  const nested = nestLanes(buildLanes(events), null, events);
  const talon = nested.find((l) => l.agentName === "talon-job-1");
  assert.equal(talon.depth, 1);
  assert.equal(talon.parentAgent, "Claw");
});
