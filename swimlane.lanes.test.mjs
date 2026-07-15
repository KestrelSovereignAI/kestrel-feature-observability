// Unit tests for the pure swimlane grouping logic (node --test).
//
// Covers buildLanes (per (agent, orchestrator) lane + session/tool nesting) and
// nestLanes (orchestrator grouping with a Direct node). DOM-free — imports the
// same module the browser panel uses.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildLanes,
  nestLanes,
  pairToolCalls,
  sessionStatus,
  ts,
  laneKey,
} from "./kestrel_feature_observability/fleet/static/swimlane.lanes.js";

// Build a minimal event at `t` seconds past a fixed epoch.
const T0 = Date.parse("2026-07-09T00:00:00.000Z");
function ev(overrides) {
  const sec = overrides.sec ?? 0;
  const e = {
    id: overrides.id ?? `e${Math.round(sec * 1000)}`,
    agent_name: overrides.agent_name ?? "planner",
    orchestrator: "orchestrator" in overrides ? overrides.orchestrator : null,
    session_id: overrides.session_id ?? "s1",
    event_type: overrides.event_type ?? "metric",
    tool_name: overrides.tool_name ?? null,
    success: overrides.success ?? null,
    metadata: overrides.metadata ?? {},
    ts: new Date(T0 + sec * 1000).toISOString(),
  };
  return e;
}

test("ts parses ISO strings and tolerates junk", () => {
  assert.equal(ts({ ts: "2026-07-09T00:00:00.000Z" }), T0);
  assert.equal(ts({ ts: "not-a-date" }), 0);
  assert.equal(ts({ ts: T0 }), T0);
});

test("laneKey distinguishes null orchestrator from named", () => {
  assert.notEqual(laneKey("a", null), laneKey("a", "boss"));
  assert.equal(laneKey("a", null), laneKey("a", undefined));
});

test("buildLanes splits the same agent across orchestrators (Q1)", () => {
  const events = [
    ev({ agent_name: "worker", orchestrator: null, sec: 1 }),
    ev({ agent_name: "worker", orchestrator: "boss", sec: 2, session_id: "s2" }),
  ];
  const lanes = buildLanes(events);
  assert.equal(lanes.length, 2, "one lane per (agent, orchestrator)");
  const direct = lanes.find((l) => l.orchestrator === null);
  const boss = lanes.find((l) => l.orchestrator === "boss");
  assert.ok(direct && boss);
  assert.equal(direct.agentName, "worker");
  assert.equal(boss.agentName, "worker");
});

test("buildLanes groups events into sessions with time bounds", () => {
  const events = [
    ev({ session_id: "s1", sec: 5 }),
    ev({ session_id: "s1", sec: 1 }),
    ev({ session_id: "s2", sec: 3 }),
  ];
  const lanes = buildLanes(events);
  assert.equal(lanes.length, 1);
  const [lane] = lanes;
  assert.equal(lane.sessions.length, 2);
  // sessions sorted by start
  assert.equal(lane.sessions[0].sessionId, "s1");
  assert.equal(lane.sessions[0].start, T0 + 1000);
  assert.equal(lane.sessions[0].end, T0 + 5000);
});

test("pairToolCalls pairs tool_call with tool_response in window", () => {
  const sorted = [
    ev({ event_type: "tool_call", tool_name: "Read", sec: 1 }),
    ev({ event_type: "tool_response", tool_name: "Read", success: true, sec: 2 }),
  ];
  const { toolCalls, looseEvents } = pairToolCalls(sorted);
  assert.equal(looseEvents.length, 0);
  assert.equal(toolCalls.length, 1);
  assert.equal(toolCalls[0].toolName, "Read");
  assert.equal(toolCalls[0].events.length, 2);
  assert.equal(toolCalls[0].success, true);
  assert.equal(toolCalls[0].end - toolCalls[0].start, 1000);
});

test("pairToolCalls leaves an unmatched tool_call open (running)", () => {
  const sorted = [ev({ event_type: "tool_call", tool_name: "Bash", sec: 1 })];
  const { toolCalls } = pairToolCalls(sorted);
  assert.equal(toolCalls.length, 1);
  assert.equal(toolCalls[0].success, null);
  assert.equal(toolCalls[0].events.length, 1);
});

test("pairToolCalls pairs subagent_call/subagent_response", () => {
  const sorted = [
    ev({ event_type: "subagent_call", tool_name: "Task", sec: 1 }),
    ev({ event_type: "subagent_response", tool_name: "Task", success: true, sec: 3 }),
  ];
  const { toolCalls } = pairToolCalls(sorted);
  assert.equal(toolCalls.length, 1);
  assert.equal(toolCalls[0].events.length, 2);
});

test("error event pairs with an open call and marks failure", () => {
  const sorted = [
    ev({ event_type: "tool_call", tool_name: "Bash", sec: 1 }),
    ev({ event_type: "error", tool_name: "Bash", sec: 2 }),
  ];
  const { toolCalls } = pairToolCalls(sorted);
  assert.equal(toolCalls.length, 1);
  assert.equal(toolCalls[0].success, false);
});

test("Task tool-calls nest their children as tasks", () => {
  const events = [
    ev({ event_type: "tool_call", tool_name: "Task", sec: 0 }),
    ev({ event_type: "tool_call", tool_name: "Read", sec: 1 }),
    ev({ event_type: "tool_response", tool_name: "Read", success: true, sec: 2 }),
    ev({ event_type: "tool_response", tool_name: "Task", success: true, sec: 5 }),
  ];
  const [lane] = buildLanes(events);
  const [session] = lane.sessions;
  assert.equal(session.tasks.length, 1);
  assert.equal(session.tasks[0].children.length, 1);
  assert.equal(session.tasks[0].children[0].toolName, "Read");
  assert.equal(session.toolCalls.length, 0, "Read is claimed by the Task");
});

test("sessionStatus reflects failure, running, completed", () => {
  assert.equal(sessionStatus([{ event_type: "error" }], []), "failed");
  assert.equal(
    sessionStatus([{ event_type: "tool_call" }], [
      { events: [{ event_type: "tool_call" }] },
    ]),
    "running"
  );
  assert.equal(
    sessionStatus([{ event_type: "metric" }], [
      { events: [{ event_type: "tool_call" }, { event_type: "tool_response" }] },
    ]),
    "completed"
  );
});

test("nestLanes groups lanes under orchestrators with Direct first", () => {
  const events = [
    ev({ agent_name: "a", orchestrator: null, sec: 1 }),
    ev({ agent_name: "b", orchestrator: "zeus", sec: 2, session_id: "s2" }),
    ev({ agent_name: "c", orchestrator: "atlas", sec: 3, session_id: "s3" }),
  ];
  const groups = nestLanes(buildLanes(events));
  assert.equal(groups.length, 3);
  assert.equal(groups[0].label, "Direct");
  assert.equal(groups[0].isDirect, true);
  // named orchestrators sorted alphabetically after Direct
  assert.equal(groups[1].orchestrator, "atlas");
  assert.equal(groups[2].orchestrator, "zeus");
});

test("nestLanes counts events per orchestrator group", () => {
  const events = [
    ev({ agent_name: "a", orchestrator: "boss", sec: 1 }),
    ev({ agent_name: "a", orchestrator: "boss", sec: 2 }),
  ];
  const groups = nestLanes(buildLanes(events));
  assert.equal(groups.length, 1);
  assert.equal(groups[0].eventCount, 2);
});

test("empty input yields empty lanes and groups", () => {
  assert.deepEqual(buildLanes([]), []);
  assert.deepEqual(nestLanes([]), []);
  assert.deepEqual(buildLanes(undefined), []);
});
