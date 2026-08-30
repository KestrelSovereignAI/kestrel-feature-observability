"""Executable contracts for Timeline/Navigator cooperative Stop actions (#115)."""

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
    pkg = tmp_path / "stop-actions"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    phoenix = (STATIC / "phoenix.js").read_text(encoding="utf-8").replace(
        'import API from "/js/api.js";',
        "const API = { requestHost: async () => ({}) };",
    )
    assert "const API" in phoenix
    (pkg / "phoenix.js").write_text(phoenix, encoding="utf-8")
    (pkg / "stop-actions.js").write_text(
        (STATIC / "stop-actions.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return pkg


def _run(pkg: pathlib.Path, name: str, source: str) -> dict:
    script = pkg / name
    script.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(script)],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_trace_inventory_walks_past_first_thousand_and_fails_closed_on_bad_cursor(
    tmp_path,
):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "trace-pagination.mjs",
        r"""
const calls = [];
let mode = "complete";
globalThis.fetch = async (_url, options) => {
  const { variables } = JSON.parse(options.body);
  calls.push(variables.after ?? null);
  const first = variables.after == null;
  const nodes = first
    ? Array.from({ length: 1000 }, (_unused, index) => ({ id: `span-${index}` }))
    : [{ id: "turn-summary" }];
  const pageInfo = first
    ? { hasNextPage: true, endCursor: "cursor-1" }
    : mode === "complete"
      ? { hasNextPage: false, endCursor: null }
      : { hasNextPage: true, endCursor: "cursor-1" };
  return {
    ok: true,
    status: 200,
    json: async () => ({
      data: {
        node: {
          trace: { spans: { edges: nodes.map((node) => ({ node })), pageInfo } },
        },
      },
    }),
  };
};
const { walkTraceSpans } = await import("./phoenix.js");
const complete = await walkTraceSpans("project-1", "trace-1");
const completeCalls = calls.splice(0);
mode = "repeated";
const repeated = await walkTraceSpans("project-1", "trace-1");
process.stdout.write(JSON.stringify({
  complete: {
    complete: complete.complete,
    count: complete.spans.length,
    last: complete.spans.at(-1).id,
    calls: completeCalls,
  },
  repeated: {
    complete: repeated.complete,
    count: repeated.spans.length,
    calls,
  },
}));
""",
    )

    assert result["complete"] == {
        "complete": True,
        "count": 1001,
        "last": "turn-summary",
        "calls": [None, "cursor-1"],
    }
    assert result["repeated"] == {
        "complete": False,
        "count": 1001,
        "calls": [None, "cursor-1"],
    }


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_trace_inventory_walk_forwards_abort_signal_across_pages(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "trace-pagination-abort.mjs",
        r"""
const calls = [];
let releaseSecond;
globalThis.fetch = async (_url, options) => {
  const { variables } = JSON.parse(options.body);
  calls.push({ after: variables.after ?? null, signal: Boolean(options.signal) });
  if (variables.after != null) {
    return new Promise((_resolve, reject) => {
      releaseSecond = () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      options.signal.addEventListener("abort", releaseSecond, { once: true });
    });
  }
  return {
    ok: true,
    status: 200,
    json: async () => ({
      data: {
        node: {
          trace: {
            spans: {
              edges: [{ node: { id: "span-1" } }],
              pageInfo: { hasNextPage: true, endCursor: "cursor-1" },
            },
          },
        },
      },
    }),
  };
};
const { walkTraceSpans } = await import("./phoenix.js");
const abort = new AbortController();
const pending = walkTraceSpans("project-1", "trace-1", { signal: abort.signal });
while (calls.length < 2) await new Promise((resolve) => setTimeout(resolve, 0));
abort.abort();
let errorName = null;
try {
  await pending;
} catch (error) {
  errorName = error.name;
}
process.stdout.write(JSON.stringify({ calls, errorName, secondReleased: Boolean(releaseSecond) }));
""",
    )

    assert result == {
        "calls": [
            {"after": None, "signal": True},
            {"after": "cursor-1", "signal": True},
        ],
        "errorName": "AbortError",
        "secondReleased": True,
    }


def test_views_abort_their_focused_trace_inventory_walks_on_teardown():
    navigator = (STATIC / "navigator.js").read_text(encoding="utf-8")
    timeline = (STATIC / "timeline.js").read_text(encoding="utf-8")

    navigator_load = navigator[
        navigator.index("async function loadEvents") : navigator.index(
            "// ── Virtualized rows"
        )
    ]
    navigator_teardown = navigator[
        navigator.index("function teardown()") : navigator.index(
            "function renderNotice()"
        )
    ]
    assert "new AbortController()" in navigator_load
    assert "{ signal: traceWalkAbort.signal }" in navigator_load
    assert "traceWalkControllers.add(traceWalkAbort)" in navigator_load
    assert "traceWalkControllers.delete(traceWalkAbort)" in navigator_load
    assert "traceWalkAbort.abort()" in navigator_teardown

    timeline_load = timeline[
        timeline.index("function loadTurnCompletion") : timeline.index(
            "function retryActivePopoverTurnCompletion"
        )
    ]
    timeline_teardown = timeline[
        timeline.index("function teardown()") : timeline.index("boot();")
    ]
    assert "new AbortController()" in timeline_load
    assert "{ signal: traceWalkAbort.signal }" in timeline_load
    assert "turnCompletionAborts.set(key, traceWalkAbort)" in timeline_load
    assert "turnCompletionAborts.delete(key)" in timeline_load
    assert "traceWalkAbort.abort()" in timeline_teardown


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_stop_fails_closed_when_turn_stop_capability_is_not_advertised(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "stop-capability-negotiation.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
const calls = [];
const controller = createStopController({
  api: {
    async requestForAgent(path, options, agent) {
      calls.push({ path, method: options.method, agent });
      if (options.method === "GET") {
        throw Object.assign(new Error("not found"), { status: 404 });
      }
      throw new Error("unsafe Stop POST reached an older host");
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#legacy",
}, { completionKnown: true, completed: false });
const outcome = await controller.stopOne(target);
process.stdout.write(JSON.stringify({ calls, outcome }));
""",
    )

    assert result["calls"] == [
        {
            "path": "/api/agent/stop/capabilities",
            "method": "GET",
            "agent": "Emma",
        }
    ]
    assert result["outcome"]["state"] == "indeterminate"
    assert result["outcome"]["code"] == "turn_stop_not_advertised"
    assert "no stop request was sent" in result["outcome"]["message"].lower()


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_stop_negotiates_once_per_agent_route_before_posting(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "stop-capability-positive.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
const calls = [];
const api = {
  async requestForAgent(path, options, agent) {
    calls.push({ path, method: options.method, agent });
    if (options.method === "GET") {
      return {
        protocol: "kestrel.cooperative_stop",
        version: 1,
        scopes: ["agent", "turn"],
        turn_address: "turn_id",
        typed_outcomes: true,
        durable_receipts: true,
      };
    }
    const body = JSON.parse(options.body);
    return {
      turn_id: body.turn_id,
      stop_outcomes: [{
        scope: "turn",
        requested_target: body.turn_id,
        resolved_target: body.turn_id,
        agent_id: "did:kestrel:emma",
        disposition: "stopped",
        correlation_id: body.correlation_id,
      }],
    };
  },
};
let correlation = 0;
const controller = createStopController({
  api,
  correlationIdFactory: () => `corr-${++correlation}`,
});
for (const turnId of ["emma#one", "emma#two"]) {
  await controller.stopOne(stopTargetFromDetail({
    agent: "Emma", agentDid: "did:kestrel:emma", turnId,
  }, { completionKnown: true, completed: false }));
}
process.stdout.write(JSON.stringify(calls));
""",
    )

    assert result == [
        {
            "path": "/api/agent/stop/capabilities",
            "method": "GET",
            "agent": "Emma",
        },
        {"path": "/api/agent/stop", "method": "POST", "agent": "Emma"},
        {"path": "/api/agent/stop", "method": "POST", "agent": "Emma"},
    ]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_stop_rejects_an_unrecognized_capability_version_without_posting(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "stop-capability-version.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
const calls = [];
const controller = createStopController({
  api: {
    async requestForAgent(path, options) {
      calls.push({ path, method: options.method });
      if (options.method === "POST") throw new Error("incompatible Stop POST sent");
      return {
        protocol: "kestrel.cooperative_stop",
        version: 2,
        scopes: ["agent", "turn"],
        turn_address: "turn_id",
        typed_outcomes: true,
        durable_receipts: true,
      };
    },
  },
});
const outcome = await controller.stopOne(stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#future",
}, { completionKnown: true, completed: false }));
process.stdout.write(JSON.stringify({ calls, outcome }));
""",
    )

    assert result["calls"] == [
        {"path": "/api/agent/stop/capabilities", "method": "GET"}
    ]
    assert result["outcome"]["code"] == "turn_stop_not_advertised"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_single_stop_pins_canonical_agent_route_and_verifies_receipt(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "single.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";

const calls = [];
const api = {
  async requestForAgent(path, options, agent) {
    calls.push({ path, options, agent, body: JSON.parse(options.body) });
    return {
      success: true,
      turn_id: "session-a#7",
      message: "Request cancelled",
      stop_outcomes: [{
        scope: "turn",
        requested_target: "session-a#7",
        resolved_target: "private-request-92",
        agent_id: "did:kestrel:emma",
        disposition: "stopped",
        correlation_id: "corr-one",
        receipt_id: "receipt-1",
        detail: null,
      }],
    };
  },
};
const controller = createStopController({
  api,
  correlationIdFactory: () => "corr-one",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
});
const target = stopTargetFromDetail({
  agent: "Emma",
  agentDid: "did:kestrel:emma",
  turnId: "session-a#7",
  traceId: "a".repeat(32),
  spanId: "b".repeat(16),
  orchestrator: "talon",
});
const outcome = await controller.stopOne(target);
const redraw = stopTargetFromDetail({
  agent: "Emma",
  agentDid: "did:kestrel:emma",
  turnId: "session-a#7",
  orchestrator: "a-different-display-parent",
});
process.stdout.write(JSON.stringify({ calls, target, outcome, redrawKey: redraw.key }));
""",
    )

    assert result["calls"] == [
        {
            "path": "/api/agent/stop",
            "options": {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": '{"turn_id":"session-a#7","correlation_id":"corr-one"}',
            },
            "agent": "Emma",
            "body": {"turn_id": "session-a#7", "correlation_id": "corr-one"},
        }
    ]
    assert result["target"]["key"] == json.dumps(
        ["did:kestrel:emma", "session-a#7"], separators=(",", ":")
    )
    assert result["redrawKey"] == result["target"]["key"]
    assert result["outcome"]["state"] == "stopped"
    assert result["outcome"]["receiptId"] == "receipt-1"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_single_stop_rejects_a_different_endpoint_operation_identity(tmp_path):
    """A stale same-target receipt cannot confirm this durable operation."""

    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "server-correlation.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
const controller = createStopController({
  correlationIdFactory: () => "client-correlation",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      const body = JSON.parse(options.body);
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: "private-request-92", agent_id: "did:kestrel:emma",
          disposition: "stopped", correlation_id: "server-correlation",
          receipt_id: "receipt-server",
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "session-a#server",
});
process.stdout.write(JSON.stringify(await controller.stopOne(target)));
""",
    )

    assert result["state"] == "indeterminate"
    assert result["correlationId"] == "client-correlation"
    assert result["receiptId"] is None


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_multi_stop_preserves_success_and_typed_failure_per_exact_target(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "multi.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";

let correlation = 0;
const api = {
  async requestForAgent(_path, options, agent) {
    const body = JSON.parse(options.body);
    const target = agent === "Emma"
      ? { did: "did:kestrel:emma", disposition: "stopped" }
      : { did: "did:kestrel:claw", disposition: "unreachable" };
    const outcome = {
      scope: "turn",
      requested_target: body.turn_id,
      resolved_target: `${agent}-request`,
      agent_id: target.did,
      disposition: target.disposition,
      correlation_id: body.correlation_id,
      detail: target.disposition === "unreachable" ? "owner lease unavailable" : null,
      receipt_id: null,
    };
    if (target.disposition === "unreachable") {
      throw {
        status: 503,
        code: "stop_not_confirmed",
        message: "Cooperative Stop could not be confirmed.",
        body: { error: { details: [outcome] } },
      };
    }
    return { turn_id: body.turn_id, stop_outcomes: [outcome] };
  },
};
const controller = createStopController({
  api,
  correlationIdFactory: () => `corr-${++correlation}`,
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
});
for (const detail of [
  { agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#2" },
  { agent: "Claw", agentDid: "did:kestrel:claw", turnId: "claw#9" },
]) controller.select(stopTargetFromDetail(detail));
const returned = await controller.stopSelected();
process.stdout.write(JSON.stringify({
  returned: returned.map((item) => item.state),
  retained: controller.results().map((item) => ({
    key: item.key,
    state: item.state,
    message: item.message,
  })),
}));
""",
    )

    assert result["returned"] == ["stopped", "unreachable"]
    assert [item["state"] for item in result["retained"]] == [
        "stopped",
        "unreachable",
    ]
    assert "owner lease unavailable" in result["retained"][1]["message"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_completed_turn_declines_locally_and_selection_survives_redraw(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "completed.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";

let calls = 0;
const controller = createStopController({
  api: { async requestForAgent() { calls += 1; throw new Error("must not call"); } },
  correlationIdFactory: () => "unused",
});
const first = stopTargetFromDetail({
  agent: "Emma",
  agentDid: "did:kestrel:emma",
  turnId: "same#1",
  orchestrator: "Direct",
});
controller.select(first);
const reconciled = stopTargetFromDetail({
  agent: "Renamed display value",
  agentDid: "did:kestrel:emma",
  turnId: "same#1",
  orchestrator: "talon",
}, { completed: true });
controller.select(reconciled);
const selected = controller.selected();
const outcome = await controller.stopSelected();
process.stdout.write(JSON.stringify({ calls, selected, outcome }));
""",
    )

    assert result["calls"] == 0
    assert len(result["selected"]) == 1
    # Identity reconciliation may advance lifecycle state but never replaces
    # the originally pinned route with redraw/display metadata.
    assert result["selected"][0]["agentName"] == "Emma"
    assert result["selected"][0]["completed"] is True
    assert result["outcome"][0]["state"] == "already_complete"
    assert result["outcome"][0]["local"] is True


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_unknown_completion_disables_stop_until_turn_events_are_loaded(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "completion-unknown.mjs",
        r"""
import { stopActionModel, stopTargetFromDetail } from "./stop-actions.js";
const controller = { getResult() { return null; }, isSelected() { return false; } };
const unknown = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#4",
}, { completionKnown: false });
const checked = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#4",
}, { completionKnown: true, completed: false });
process.stdout.write(JSON.stringify({
  unknown: stopActionModel(unknown, controller),
  checked: stopActionModel(checked, controller),
}));
""",
    )

    assert result["unknown"]["disabled"] is True
    assert result["unknown"]["stopLabel"] == "Checking turn state…"
    assert result["checked"]["disabled"] is False


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_pending_target_is_submitted_once_across_single_and_batch_actions(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "pending-dedupe.mjs",
        r"""
import {
  createStopController,
  mountStopActionBar,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
let release;
const controller = createStopController({
  correlationIdFactory: () => `corr-${calls + 1}`,
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      calls += 1;
      const body = JSON.parse(options.body);
      await new Promise((resolve) => { release = resolve; });
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: "request-1", agent_id: "did:kestrel:emma",
          disposition: "stopped", correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#pending",
});
controller.select(target);
const listeners = new Map();
const element = {
  innerHTML: "",
  addEventListener(name, fn) { listeners.set(name, fn); },
  removeEventListener(name) { listeners.delete(name); },
};
const mounted = mountStopActionBar(element, controller);
const first = controller.stopOne(target);
const second = controller.stopOne(target);
const batch = controller.stopSelected();
const pendingHtml = element.innerHTML;
while (!release) await Promise.resolve();
release();
const outcomes = await Promise.all([first, second]);
const batchOutcomes = await batch;
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls,
  pendingHtml,
  states: outcomes.map((item) => item.state),
  batchCount: batchOutcomes.length,
}));
""",
    )

    assert result["calls"] == 1
    assert "data-stop-selected disabled" in result["pendingHtml"]
    assert result["states"] == ["stopped", "stopped"]
    assert result["batchCount"] == 0


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_unknown_and_confirmed_targets_are_never_redispatched(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "stop-guards.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
const api = {
  async requestForAgent(_path, options) {
    calls += 1;
    const body = JSON.parse(options.body);
    return {
      turn_id: body.turn_id,
      stop_outcomes: [{
        scope: "turn", requested_target: body.turn_id,
        resolved_target: "request-1", agent_id: "did:kestrel:emma",
        disposition: "stopped", correlation_id: body.correlation_id,
      }],
    };
  },
};
const controller = createStopController({
  api,
  correlationIdFactory: () => "corr-one",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
});
const unknown = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#unknown",
}, { completionKnown: false });
controller.select(unknown);
const unknownOne = await controller.stopOne(unknown);
const unknownBatch = await controller.stopSelected();
const known = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#unknown",
}, { completionKnown: true, completed: false });
controller.observe(known);
const first = await controller.stopSelected();
const replay = await controller.stopOne(known);
const terminalBatch = await controller.stopSelected();
process.stdout.write(JSON.stringify({
  calls, unknownOne, unknownBatch, first, replay, terminalBatch,
  selected: controller.selected(),
}));
""",
    )

    assert result["calls"] == 1
    assert result["unknownOne"] is None
    assert result["unknownBatch"] == []
    assert result["first"][0]["state"] == "stopped"
    assert result["replay"]["state"] == "stopped"
    assert result["terminalBatch"] == []
    assert result["selected"][0]["completionKnown"] is True


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_dismissing_outcome_preserves_confirmed_terminal_guard(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "dismiss-guard.mjs",
        r"""
import {
  createStopController,
  mountStopActionBar,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopActionModel,
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
const controller = createStopController({
  correlationIdFactory: () => "corr-dismiss",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      calls += 1;
      const body = JSON.parse(options.body);
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: "request-1", agent_id: "did:kestrel:emma",
          disposition: "stopped", correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#dismiss",
});
controller.select(target);
const element = {
  innerHTML: "",
  addEventListener() {},
  removeEventListener() {},
};
const mounted = mountStopActionBar(element, controller);
const first = await controller.stopOne(target);
controller.clearResults();
const visibleAfterDismiss = controller.results();
const remembered = controller.getResult(target);
const model = stopActionModel(target, controller);
const barAfterDismiss = element.innerHTML;
const second = await controller.stopOne(target);
const batch = await controller.stopSelected();
mounted.destroy();
process.stdout.write(JSON.stringify({
  calls, first, visibleAfterDismiss, remembered, model, barAfterDismiss, second, batch,
}));
""",
    )

    assert result["calls"] == 1
    assert result["visibleAfterDismiss"] == []
    assert result["remembered"]["state"] == "stopped"
    assert result["model"]["disabled"] is True
    assert "data-stop-selected disabled" in result["barAfterDismiss"]
    assert result["second"]["state"] == "stopped"
    assert result["batch"] == []


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_dismissing_indeterminate_outcome_preserves_replay_identity(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "dismiss-indeterminate.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
const correlations = ["corr-first", "corr-second"];
const calls = [];
const controller = createStopController({
  correlationIdFactory: () => correlations.shift(),
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      const body = JSON.parse(options.body);
      calls.push(body.correlation_id);
      if (calls.length === 1) throw new Error("timeout after possible commit");
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: "request-1", agent_id: "did:kestrel:emma",
          disposition: "stopped", correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#ambiguous",
}, { completionKnown: true, completed: false });
const first = await controller.stopOne(target);
controller.clearResults();
const visible = controller.results();
const retained = controller.getResult(target);
const second = await controller.stopOne(target);
process.stdout.write(JSON.stringify({ calls, first, visible, retained, second }));
""",
    )

    assert result["first"]["state"] == "indeterminate"
    assert result["visible"] == []
    assert result["retained"]["state"] == "indeterminate"
    assert result["calls"] == ["corr-first", "corr-first"]
    assert result["second"]["state"] == "stopped"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_dismissing_refusal_retains_later_completion_guard(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "dismiss-completion-guard.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
const controller = createStopController({
  correlationIdFactory: () => `corr-${calls + 1}`,
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      calls += 1;
      const body = JSON.parse(options.body);
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: "request-1", agent_id: "did:kestrel:emma",
          disposition: "refused", correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const stale = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#completed",
}, { completionKnown: true, completed: false });
const first = await controller.stopOne(stale);
controller.observe(stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#completed",
}, { completionKnown: true, completed: true }));
controller.clearResults();
const second = await controller.stopOne(stale);
process.stdout.write(JSON.stringify({ calls, first, second }));
""",
    )

    assert result["first"]["state"] == "refused"
    assert result["calls"] == 1
    assert result["second"]["state"] == "already_complete"
    assert result["second"]["local"] is True


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_inspector_retry_reuses_retained_route_and_completion_evidence(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "retained-inspector-retry.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopActionModel,
  stopTargetFromDetail,
} from "./stop-actions.js";
const calls = [];
const controller = createStopController({
  correlationIdFactory: () => "corr-retained",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, _options, agent) {
      calls.push(agent);
      throw new Error("timeout after possible commit");
    },
  },
});
const canonical = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#retained",
}, { completionKnown: true, completed: false });
const first = await controller.stopOne(canonical);
controller.observe(stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#retained",
}, { completionKnown: true, completed: true }));
const staleInspector = stopTargetFromDetail({
  agent: "Wrong redraw route", agentDid: "did:kestrel:emma", turnId: "emma#retained",
}, { completionKnown: true, completed: false });
const model = stopActionModel(staleInspector, controller);
const second = await controller.stopOne(staleInspector);
process.stdout.write(JSON.stringify({ calls, first, model, second }));
""",
    )

    assert result["first"]["state"] == "indeterminate"
    assert result["calls"] == ["Emma"]
    assert result["model"]["disabled"] is True
    assert result["model"]["stopLabel"] == "Already complete"
    assert result["second"]["state"] == "already_complete"
    assert result["second"]["target"]["agentName"] == "Emma"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_reselection_keeps_indeterminate_route_and_late_completion(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "retained-reselection.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
const routes = [];
const controller = createStopController({
  correlationIdFactory: () => "corr-retained-reselection",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, _options, agent) {
      routes.push(agent);
      throw new Error("response lost after possible commit");
    },
  },
});
const original = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#retained",
}, { completionKnown: true, completed: false });
controller.select(original);
await controller.stopSelected();
controller.observe(stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#retained",
}, { completionKnown: true, completed: true }));
controller.clearSelection();
const staleRenamed = stopTargetFromDetail({
  agent: "Wrong redraw route",
  agentDid: "did:kestrel:emma",
  turnId: "emma#retained",
}, { completionKnown: true, completed: false });
controller.select(staleRenamed);
const selected = controller.selected()[0];
const outcomes = await controller.stopSelected();
process.stdout.write(JSON.stringify({ routes, selected, outcomes }));
""",
    )

    assert result["routes"] == ["Emma"]
    assert result["selected"]["agentName"] == "Emma"
    assert result["selected"]["completed"] is True
    assert result["outcomes"][0]["state"] == "already_complete"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_synthetic_unknown_lane_is_not_a_stop_route(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "synthetic-route.mjs",
        r"""
import {
  ATTR_AGENT_DID,
  ATTR_AGENT_NAME,
  ATTR_TURN_ID,
  UNKNOWN_AGENT,
  normalizeSpanDetail,
  stopTargetFromDetail,
} from "./phoenix.js";
const unstamped = normalizeSpanDetail({
  name: "turn",
  attributes: {
    [ATTR_AGENT_DID]: "did:kestrel:missing-route",
    [ATTR_TURN_ID]: "missing-route#1",
  },
}, { agent: UNKNOWN_AGENT });
const stampedUnknown = normalizeSpanDetail({
  name: "turn",
  attributes: {
    [ATTR_AGENT_NAME]: "unknown",
    [ATTR_AGENT_DID]: "did:kestrel:real-unknown",
    [ATTR_TURN_ID]: "unknown#1",
  },
}, { agent: UNKNOWN_AGENT });
process.stdout.write(JSON.stringify({
  unstampedDetail: unstamped,
  unstampedTarget: stopTargetFromDetail(unstamped),
  stampedTarget: stopTargetFromDetail(stampedUnknown),
}));
""",
    )

    assert result["unstampedDetail"]["agent"] == "unknown"
    assert result["unstampedDetail"]["agentRoute"] is None
    assert result["unstampedTarget"]["addressable"] is False
    assert "agent route" in result["unstampedTarget"]["missing"]
    assert result["stampedTarget"]["addressable"] is True
    assert result["stampedTarget"]["agentName"] == "unknown"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_navigator_completion_requires_untruncated_refreshable_inventory(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "navigator.js").write_text(
        (STATIC / "navigator.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _run(
        pkg,
        "navigator-completion.mjs",
        r"""
import {
  navigatorTraceInventoryComplete,
  navigatorTurnCompletionEvidence,
  navigatorTurnNeedsCompletionRefresh,
} from "./navigator.js";
const turn = (data, loaded = true) => ({ data, loaded });
const refreshable = {
  kind: "turn",
  loaded: true,
  data: {
    inventoryComplete: true,
    completionRefreshesRemaining: 2,
    summary: null,
  },
};
process.stdout.write(JSON.stringify({
  initial: navigatorTurnCompletionEvidence(turn({}, false)),
  truncated: navigatorTurnCompletionEvidence(turn({ inventoryComplete: false })),
  active: navigatorTurnCompletionEvidence(turn({ inventoryComplete: true })),
  summarized: navigatorTurnCompletionEvidence(turn({
    inventoryComplete: false,
    summary: { status: "ok" },
  })),
  inventories: {
    absentTrace: navigatorTraceInventoryComplete(null, []),
    absentConnection: navigatorTraceInventoryComplete({}, []),
    emptyTrace: navigatorTraceInventoryComplete({ spans: { edges: [] } }, []),
    fullPage: navigatorTraceInventoryComplete(
      { spans: { edges: [] } },
      Array.from({ length: 1000 }, () => ({})),
    ),
  },
  refresh: {
    live: navigatorTurnNeedsCompletionRefresh(refreshable),
    exhausted: navigatorTurnNeedsCompletionRefresh({
      ...refreshable,
      data: { ...refreshable.data, completionRefreshesRemaining: 0 },
    }),
    summarized: navigatorTurnNeedsCompletionRefresh({
      ...refreshable,
      data: { ...refreshable.data, summary: { status: "ok" } },
    }),
    manualExhausted: navigatorTurnNeedsCompletionRefresh({
      ...refreshable,
      data: { ...refreshable.data, completionRefreshesRemaining: 0 },
    }, { manual: true }),
    manualIncomplete: navigatorTurnNeedsCompletionRefresh({
      ...refreshable,
      data: {
        ...refreshable.data,
        inventoryComplete: false,
        completionRefreshesRemaining: 0,
      },
    }, { manual: true }),
  },
}));
""",
    )

    assert result["initial"] == {"completed": False, "completionKnown": False}
    assert result["truncated"] == {"completed": False, "completionKnown": False}
    assert result["active"] == {"completed": False, "completionKnown": True}
    assert result["summarized"] == {"completed": True, "completionKnown": True}
    assert result["inventories"] == {
        "absentTrace": False,
        "absentConnection": False,
        "emptyTrace": True,
        "fullPage": False,
    }
    assert result["refresh"] == {
        "live": True,
        "exhausted": False,
        "summarized": False,
        "manualExhausted": True,
        "manualIncomplete": True,
    }


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_navigator_only_canonical_direct_turn_summary_closes_stop(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "navigator.js").write_text(
        (STATIC / "navigator.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _run(
        pkg,
        "navigator-canonical-summary.mjs",
        r"""
import { isCanonicalTurnSummarySpan } from "./navigator.js";
const span = (name, parentId = "turn-root", spanKind = "CHAIN") => ({
  name, parentId, spanKind,
});
process.stdout.write(JSON.stringify({
  canonical: isCanonicalTurnSummarySpan(span("turn 12 summary"), "turn-root"),
  planning: isCanonicalTurnSummarySpan(span("turn planning summary"), "turn-root"),
  decorated: isCanonicalTurnSummarySpan(span("agent turn 12 summary done"), "turn-root"),
  wrongParent: isCanonicalTurnSummarySpan(span("turn 12 summary", "tool-root"), "turn-root"),
  toolNamedSummary: isCanonicalTurnSummarySpan(
    span("turn 12 summary", "turn-root", "TOOL"),
    "turn-root",
  ),
}));
""",
    )

    assert result == {
        "canonical": True,
        "planning": False,
        "decorated": False,
        "wrongParent": False,
        "toolNamedSummary": False,
    }
    navigator = (STATIC / "navigator.js").read_text(encoding="utf-8")
    load_events = navigator[
        navigator.index("async function loadEvents") : navigator.index(
            "// ── Virtualized rows"
        )
    ]
    assert "isCanonicalTurnSummarySpan(span, rootSpanId)" in load_events
    assert r"\bturn\b.*\bsummary\b" not in load_events


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_focused_completion_updates_retained_selection_without_open_popover(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "timeline.js").write_text(
        (STATIC / "timeline.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _run(
        pkg,
        "focused-completion-selection.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
import { observeFocusedTurnCompletion } from "./timeline.js";
const controller = createStopController({
  api: { async requestForAgent() { throw new Error("must not call"); } },
});
const detail = {
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#focused",
};
controller.select(stopTargetFromDetail(detail, {
  completionKnown: false,
  completed: false,
}));
observeFocusedTurnCompletion(controller, detail, {
  completionKnown: true,
  completed: true,
});
process.stdout.write(JSON.stringify(controller.selected()[0]));
""",
    )

    assert result["completionKnown"] is True
    assert result["completed"] is True


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_revalidation_invalidates_stale_negative_completion_evidence(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "completion-revalidation.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
let calls = 0;
const controller = createStopController({
  api: { async requestForAgent() { calls += 1; return {}; } },
});
const detail = {
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#revalidate",
};
controller.select(stopTargetFromDetail(detail, {
  completionKnown: true,
  completed: false,
}));
// A prior focused "active" snapshot expires before the replacement trace read.
controller.observe(stopTargetFromDetail(detail, {
  completionKnown: false,
  completed: false,
}));
const selected = controller.selected()[0];
const batch = await controller.stopSelected();
process.stdout.write(JSON.stringify({ selected, batch, calls }));
""",
    )

    assert result["selected"]["completionKnown"] is False
    assert result["selected"]["completed"] is False
    assert result["batch"] == []
    assert result["calls"] == 0


def test_active_turn_popover_uses_layout_completion_index_not_full_store_scans():
    timeline = (STATIC / "timeline.js").read_text(encoding="utf-8")
    hot_path = timeline[
        timeline.index("function knownTurnCompletion") : timeline.index(
            "function reconcileSelectedTurnCompletions"
        )
    ]

    assert "spans.values()" not in hot_path
    assert "completedTurnKeys" in hot_path
    assert "completedTurnKeys = turnCompletionIndex(spans.values())" in timeline


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_shared_trace_completion_repaints_the_current_same_turn_popover(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "timeline.js").write_text(
        (STATIC / "timeline.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _run(
        pkg,
        "timeline-completion-key.mjs",
        r"""
import { sameTimelineTurnCompletion } from "./timeline.js";
const first = { id: "span-a", projectId: "project-1", traceId: "trace-1" };
process.stdout.write(JSON.stringify({
  sibling: sameTimelineTurnCompletion(
    first,
    { id: "span-b", projectId: "project-1", traceId: "trace-1" },
  ),
  otherTrace: sameTimelineTurnCompletion(
    first,
    { id: "span-c", projectId: "project-1", traceId: "trace-2" },
  ),
  otherProject: sameTimelineTurnCompletion(
    first,
    { id: "span-d", projectId: "project-2", traceId: "trace-1" },
  ),
}));
""",
    )

    assert result == {"sibling": True, "otherTrace": False, "otherProject": False}
    timeline = (STATIC / "timeline.js").read_text(encoding="utf-8")
    assert "sameTimelineTurnCompletion(activePopoverSpan, s)" in timeline


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_timeline_partial_inventory_keeps_completion_unknown(tmp_path):
    pkg = _module_dir(tmp_path)
    (pkg / "timeline.js").write_text(
        (STATIC / "timeline.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _run(
        pkg,
        "turn-completion.mjs",
        r"""
import {
  needsFocusedTurnCompletion,
  turnCompletionIndex,
  turnCompletionEvidence,
} from "./timeline.js";
const detail = {
  agent: "Emma",
  turnId: "turn-4",
  agentDid: "did:kestrel:emma",
};
const root = {
  name: "Emma turn 4",
  spanKind: "AGENT",
  context: { spanId: "turn-root-span" },
  attributes: JSON.stringify({
    "kestrel.turn_id": "turn-4",
    "kestrel.agent_did": "did:kestrel:emma",
    "kestrel.marker": "start",
  }),
};
const summary = {
  name: "turn 4 summary",
  spanKind: "CHAIN",
  parentId: "turn-root-span",
  attributes: root.attributes,
};
const toolNamedSummary = {
  name: "turn 4 summary",
  spanKind: "TOOL",
  parentId: "turn-root-span",
  attributes: root.attributes,
};
const nestedChain = {
  name: "nested chain",
  spanKind: "CHAIN",
  context: { spanId: "nested-chain-span" },
  parentId: "turn-root-span",
  attributes: JSON.stringify({
    "kestrel.turn_id": "turn-4",
    "kestrel.agent_did": "did:kestrel:emma",
  }),
};
const nestedNamedSummary = {
  name: "turn 4 summary",
  spanKind: "CHAIN",
  parentId: "nested-chain-span",
  attributes: nestedChain.attributes,
};
const completionIndex = turnCompletionIndex([root, summary]);
const impostorIndex = turnCompletionIndex([root, toolNamedSummary]);
const nestedImpostorIndex = turnCompletionIndex([
  root, nestedChain, nestedNamedSummary,
]);
process.stdout.write(JSON.stringify({
  partial: turnCompletionEvidence([root], detail, { truncated: true }),
  full: turnCompletionEvidence([root], detail, { truncated: false }),
  completedPartial: turnCompletionEvidence([root, summary], detail, { truncated: true }),
  toolNamedSummary: turnCompletionEvidence(
    [root, toolNamedSummary], detail, { truncated: true },
  ),
  nestedNamedSummary: turnCompletionEvidence(
    [root, nestedChain, nestedNamedSummary], detail, { truncated: true },
  ),
  indexed: {
    completed: completionIndex.has("did:kestrel:emma\u0000turn-4"),
    unrelated: completionIndex.has("did:kestrel:claw\u0000turn-4"),
    toolNamedSummary: impostorIndex.has("did:kestrel:emma\u0000turn-4"),
    nestedNamedSummary: nestedImpostorIndex.has("did:kestrel:emma\u0000turn-4"),
  },
  focused: {
    active: needsFocusedTurnCompletion([root], detail),
    completed: needsFocusedTurnCompletion([root, summary], detail),
    toolNamedSummary: needsFocusedTurnCompletion([root, toolNamedSummary], detail),
    nestedNamedSummary: needsFocusedTurnCompletion(
      [root, nestedChain, nestedNamedSummary], detail,
    ),
    noController: needsFocusedTurnCompletion([root], detail, { stopAvailable: false }),
    unaddressable: needsFocusedTurnCompletion([root], { turnId: "turn-4" }),
  },
}));
""",
    )

    assert result["partial"] == {"completed": False, "completionKnown": False}
    assert result["full"] == {"completed": False, "completionKnown": True}
    assert result["completedPartial"] == {
        "completed": True,
        "completionKnown": True,
    }
    assert result["toolNamedSummary"] == {
        "completed": False,
        "completionKnown": False,
    }
    assert result["nestedNamedSummary"] == {
        "completed": False,
        "completionKnown": False,
    }
    assert result["indexed"] == {
        "completed": True,
        "unrelated": False,
        "toolNamedSummary": False,
        "nestedNamedSummary": False,
    }
    assert result["focused"] == {
        "active": True,
        "completed": False,
        "toolNamedSummary": True,
        "nestedNamedSummary": True,
        "noController": False,
        "unaddressable": False,
    }


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_mismatched_server_identity_is_indeterminate(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "mismatch.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
const controller = createStopController({
  correlationIdFactory: () => "corr-mismatch",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      const body = JSON.parse(options.body);
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn",
          requested_target: body.turn_id,
          resolved_target: "request-1",
          agent_id: "did:kestrel:someone-else",
          disposition: "stopped",
          correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#3",
});
const outcome = await controller.stopOne(target);
process.stdout.write(JSON.stringify(outcome));
""",
    )

    assert result["state"] == "indeterminate"
    assert "did and turn id" in result["message"].lower()


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_indeterminate_retry_replays_operation_and_completion_disables_it(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "indeterminate-replay.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
let factoryCalls = 0;
const bodies = [];
const controller = createStopController({
  correlationIdFactory: () => `corr-${++factoryCalls}`,
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      calls += 1;
      const body = JSON.parse(options.body);
      bodies.push(body);
      if (calls === 1 || calls === 3) throw new Error("response lost");
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: "request-1", agent_id: "did:kestrel:emma",
          disposition: "already_complete", correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#replay",
});
const first = await controller.stopOne(target);
const second = await controller.stopOne(controller.targetForKey(target.key));

const separate = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#complete",
});
const ambiguous = await controller.stopOne(separate);
const completed = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#complete",
}, { completed: true, completionKnown: true });
controller.observe(completed);
const retainedAfterSummary = controller.getResult(separate);
const declined = await controller.stopOne(controller.targetForKey(separate.key));
process.stdout.write(JSON.stringify({
  first: first.state,
  second: second.state,
  ambiguous: ambiguous.state,
  retainedCompleted: retainedAfterSummary.target.completed,
  declined: declined.state,
  calls,
  factoryCalls,
  correlationIds: bodies.map((body) => body.correlation_id),
}));
""",
    )

    assert result == {
        "first": "indeterminate",
        "second": "already_complete",
        "ambiguous": "indeterminate",
        "retainedCompleted": True,
        "declined": "already_complete",
        "calls": 3,
        "factoryCalls": 2,
        "correlationIds": ["corr-1", "corr-1", "corr-2"],
    }


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_pending_stop_cannot_overwrite_late_completion_evidence(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "pending-completion-race.mjs",
        r"""
import {
  createStopController,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";

let releaseResponse;
let calls = 0;
const responseGate = new Promise((resolve) => { releaseResponse = resolve; });
const controller = createStopController({
  correlationIdFactory: () => "corr-race",
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options) {
      calls += 1;
      const body = JSON.parse(options.body);
      await responseGate;
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn",
          requested_target: body.turn_id,
          resolved_target: body.turn_id,
          agent_id: "did:kestrel:emma",
          disposition: "refused",
          correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#race",
});
const pending = controller.stopOne(target);
await Promise.resolve();
controller.observe(stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "emma#race",
}, { completed: true, completionKnown: true }));
releaseResponse();
const refused = await pending;
const retained = controller.getResult(target);
const retry = await controller.stopOne(controller.targetForKey(target.key));
process.stdout.write(JSON.stringify({
  refused: refused.state,
  retainedCompleted: retained.target.completed,
  knownCompleted: controller.knownTargets()[0].completed,
  retry: retry.state,
  calls,
}));
""",
    )

    assert result == {
        "refused": "refused",
        "retainedCompleted": True,
        "knownCompleted": True,
        "retry": "already_complete",
        "calls": 1,
    }


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_action_bar_dispatches_snapshot_and_keeps_partial_outcomes_visible(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "action-bar.mjs",
        r"""
import {
  createStopController,
  mountStopActionBar,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";

const calls = [];
const controller = createStopController({
  correlationIdFactory: () => `corr-${calls.length + 1}`,
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: {
    async requestForAgent(_path, options, agent) {
      const body = JSON.parse(options.body);
      calls.push([agent, body.turn_id]);
      return {
        turn_id: body.turn_id,
        stop_outcomes: [{
          scope: "turn", requested_target: body.turn_id,
          resolved_target: `${agent}-request`,
          agent_id: `did:kestrel:${agent.toLowerCase()}`,
          disposition: agent === "Emma" ? "stopped" : "already_complete",
          correlation_id: body.correlation_id,
        }],
      };
    },
  },
});
for (const agent of ["Emma", "Claw"]) {
  controller.select(stopTargetFromDetail({
    agent, agentDid: `did:kestrel:${agent.toLowerCase()}`, turnId: `${agent}#1`,
  }));
}
const listeners = new Map();
const element = {
  innerHTML: "",
  addEventListener(name, fn) { listeners.set(name, fn); },
  removeEventListener(name) { listeners.delete(name); },
};
const mounted = mountStopActionBar(element, controller);
const before = element.innerHTML;
listeners.get("click")({
  target: { closest(selector) { return selector === "[data-stop-selected]" ? {} : null; } },
});
for (let i = 0; i < 20 && controller.results().some((r) => r.state === "submitting"); i++) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
const after = element.innerHTML;
mounted.destroy();
process.stdout.write(JSON.stringify({ before, after, calls }));
""",
    )

    assert "2 turns selected" in result["before"]
    assert result["calls"] == [["Emma", "Emma#1"], ["Claw", "Claw#1"]]
    assert "Stopped" in result["after"]
    assert "Already complete" in result["after"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_action_bar_hides_retry_while_completion_is_unknown(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "action-bar-unknown-retry.mjs",
        r"""
import {
  createStopController,
  mountStopActionBar,
  REQUIRED_TURN_STOP_CAPABILITY_V1,
  stopTargetFromDetail,
} from "./stop-actions.js";
const controller = createStopController({
  capabilityLoader: async () => REQUIRED_TURN_STOP_CAPABILITY_V1,
  api: { async requestForAgent() { throw new Error("response lost"); } },
});
const target = stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "Emma#unknown",
}, { completionKnown: true, completed: false });
await controller.stopOne(target);
const listeners = new Map();
const element = {
  innerHTML: "",
  addEventListener(name, fn) { listeners.set(name, fn); },
  removeEventListener(name) { listeners.delete(name); },
};
const mounted = mountStopActionBar(element, controller);
const before = element.innerHTML;
controller.observe(stopTargetFromDetail({
  agent: "Emma", agentDid: "did:kestrel:emma", turnId: "Emma#unknown",
}, { completionKnown: false, completed: false }));
const after = element.innerHTML;
mounted.destroy();
process.stdout.write(JSON.stringify({ before, after }));
""",
    )

    assert "data-retry-stop" in result["before"]
    assert "data-retry-stop" not in result["after"]


def test_both_inspectors_and_panel_ship_the_shared_stop_wiring():
    timeline = (STATIC / "timeline.js").read_text(encoding="utf-8")
    navigator = (STATIC / "navigator.js").read_text(encoding="utf-8")
    panel = (STATIC / "observability.js").read_text(encoding="utf-8")

    assert "data-pstop" in timeline
    assert "data-pselect" in timeline
    assert "stopController.stopOne(stopTargetForSpan(s))" in timeline
    assert "loadTurnCompletion(s, detail)" in timeline
    assert "data-inspector-stop" in navigator
    assert "data-inspector-select" in navigator
    assert "stopController.stopOne(stopTargetForNode" in navigator
    assert "navigatorTurnCompletionEvidence(turn)" in navigator
    assert "navigatorTraceInventoryComplete(trace, spans)" in navigator
    assert "navigatorTurnNeedsCompletionRefresh(node, { manual })" in navigator
    assert "stopTargetForNode(node);" in navigator
    assert "reconcileSelectedTurnCompletions();" in timeline
    assert "completedTurnKeys = turnCompletionIndex(spans.values());" in timeline
    assert "stopController.knownTargets()" in timeline
    assert "turnCompletionEvidence(spans.values(), target" not in timeline
    assert "turnCompletionCache.get(key)?.completed === true" in timeline
    assert "turnCompletionCache.delete(key);" in timeline
    assert "observeFocusedTurnCompletion(stopController, detail, evidence);" in timeline
    refresh_wiring = timeline[
        timeline.index("// Toolbar.") : timeline.index("// ── Live render loop")
    ]
    assert "retryActivePopoverTurnCompletion();" in refresh_wiring
    assert "needsFocusedTurnCompletion(spans.values(), detail" not in timeline
    load_children = navigator[
        navigator.index("async function loadChildren") : navigator.index(
            "async function loadProjects"
        )
    ]
    assert load_children.index("node.loaded = true") < load_children.index(
        "stopTargetForNode(node)"
    )
    assert "if (stopController) stopTargetForNode(node);" not in navigator[
        navigator.index("async function loadEvents") : navigator.index(
            "// ── Virtualized rows"
        )
    ]
    assert "createStopController({ api: API })" in panel
    assert "mountStopActionBar(stopActionsEl, stopController)" in panel
    assert "const opts = { project, stopController };" in panel
