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
def test_single_stop_pins_canonical_agent_route_and_verifies_receipt(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "single.mjs",
        r"""
import {
  createStopController,
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
const controller = createStopController({ api, correlationIdFactory: () => "corr-one" });
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
def test_multi_stop_preserves_success_and_typed_failure_per_exact_target(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "multi.mjs",
        r"""
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";

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
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
let release;
const controller = createStopController({
  correlationIdFactory: () => `corr-${calls + 1}`,
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
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
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
const controller = createStopController({ api, correlationIdFactory: () => "corr-one" });
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
  stopActionModel,
  stopTargetFromDetail,
} from "./stop-actions.js";
let calls = 0;
const controller = createStopController({
  correlationIdFactory: () => "corr-dismiss",
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
    }


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
  attributes: JSON.stringify({
    "kestrel.turn_id": "turn-4",
    "kestrel.agent_did": "did:kestrel:emma",
  }),
};
const summary = {
  name: "turn 4 summary",
  attributes: root.attributes,
};
const completionIndex = turnCompletionIndex([root, summary]);
process.stdout.write(JSON.stringify({
  partial: turnCompletionEvidence([root], detail, { truncated: true }),
  full: turnCompletionEvidence([root], detail, { truncated: false }),
  completedPartial: turnCompletionEvidence([root, summary], detail, { truncated: true }),
  indexed: {
    completed: completionIndex.has("did:kestrel:emma\u0000turn-4"),
    unrelated: completionIndex.has("did:kestrel:claw\u0000turn-4"),
  },
  focused: {
    active: needsFocusedTurnCompletion([root], detail),
    completed: needsFocusedTurnCompletion([root, summary], detail),
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
    assert result["indexed"] == {"completed": True, "unrelated": False}
    assert result["focused"] == {
        "active": True,
        "completed": False,
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
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
const controller = createStopController({
  correlationIdFactory: () => "corr-mismatch",
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
import { createStopController, stopTargetFromDetail } from "./stop-actions.js";
let calls = 0;
let factoryCalls = 0;
const bodies = [];
const controller = createStopController({
  correlationIdFactory: () => `corr-${++factoryCalls}`,
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
def test_action_bar_dispatches_snapshot_and_keeps_partial_outcomes_visible(tmp_path):
    pkg = _module_dir(tmp_path)
    result = _run(
        pkg,
        "action-bar.mjs",
        r"""
import {
  createStopController,
  mountStopActionBar,
  stopTargetFromDetail,
} from "./stop-actions.js";

const calls = [];
const controller = createStopController({
  correlationIdFactory: () => `corr-${calls.length + 1}`,
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
    assert "navigatorTurnNeedsCompletionRefresh(node)" in navigator
    assert "stopTargetForNode(node);" in navigator
    assert "reconcileSelectedTurnCompletions();" in timeline
    assert "const completedTurns = turnCompletionIndex(spans.values());" in timeline
    assert "turnCompletionEvidence(spans.values(), target" not in timeline
    assert "turnCompletionCache.get(key)?.completed === true" in timeline
    assert "turnCompletionCache.delete(key);" in timeline
    assert "needsFocusedTurnCompletion(spans.values(), detail" in timeline
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
