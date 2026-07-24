"""Executable shipped-JavaScript contracts for span inspection/navigation."""

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
    pkg = tmp_path / "span-contract"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    phoenix = (STATIC / "phoenix.js").read_text(encoding="utf-8").replace(
        'import API from "/js/api.js";',
        "const API = { requestHost: async () => ({}) };",
    )
    assert "const API" in phoenix
    (pkg / "phoenix.js").write_text(phoenix, encoding="utf-8")
    (pkg / "navigator.js").write_text(
        (STATIC / "navigator.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (pkg / "timeline.js").write_text(
        (STATIC / "timeline.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return pkg


def _write_fake_dom(pkg: pathlib.Path) -> None:
    """Small browser surface sufficient to mount the shipped views under Node."""
    (pkg / "fake-dom.mjs").write_text(
        r"""
class FakeClassList {
  constructor() {
    this.values = new Set();
  }
  toggle(name, force) {
    const on = force === undefined ? !this.values.has(name) : Boolean(force);
    if (on) this.values.add(name);
    else this.values.delete(name);
    return on;
  }
}

class FakeCanvasContext {
  constructor() {
    this.frames = [];
    this.currentFrame = null;
  }
  record(type, args) {
    if (!this.currentFrame) return;
    this.currentFrame.operations.push({
      type,
      args,
      fillStyle: this.fillStyle ?? null,
      strokeStyle: this.strokeStyle ?? null,
      lineWidth: this.lineWidth ?? null,
      textAlign: this.textAlign ?? null,
    });
  }
  setTransform() {}
  clearRect(...args) {
    this.currentFrame = { operations: [] };
    this.frames.push(this.currentFrame);
    this.record("clearRect", args);
  }
  fillRect(...args) {
    this.record("fillRect", args);
  }
  strokeRect(...args) {
    this.record("strokeRect", args);
  }
  beginPath() {}
  moveTo() {}
  lineTo() {}
  stroke() {}
  fillText(...args) {
    this.record("fillText", args);
  }
  save() {}
  restore() {}
  rect() {}
  clip() {}
  measureText(value) {
    return { width: String(value).length * 7 };
  }
}

export class FakeElement {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.style = {};
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.selectorMap = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.scrollTop = 0;
    this.clientHeight = 600;
    this.offsetWidth = 320;
    this.offsetHeight = 180;
    this._innerHTML = "";
    this._textContent = "";
    this.context = this.tagName === "CANVAS" ? new FakeCanvasContext() : null;
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.selectorMap = new Map();
    const matches = this._innerHTML.matchAll(/\bdata-([a-z0-9-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g);
    for (const match of matches) {
      const name = match[1];
      const child = new FakeElement(name === "canvas" ? "canvas" : "div");
      child.setAttribute(`data-${name}`, match[2] ?? match[3] ?? match[4] ?? "");
      if (name === "i") child.dataset.i = match[2] ?? match[3] ?? match[4] ?? "";
      this.selectorMap.set(`[data-${name}]`, child);
    }
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set textContent(value) {
    this._textContent = String(value);
  }
  get textContent() {
    return this._textContent;
  }
  querySelector(selector) {
    return this.selectorMap.get(selector) || null;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  hasAttribute(name) {
    return this.attributes.has(name);
  }
  addEventListener(type, listener) {
    const list = this.listeners.get(type) || [];
    list.push(listener);
    this.listeners.set(type, list);
  }
  removeEventListener(type, listener) {
    const list = this.listeners.get(type) || [];
    this.listeners.set(type, list.filter((item) => item !== listener));
  }
  dispatch(type, init = {}) {
    const event = {
      target: this,
      preventDefault() {},
      stopPropagation() {},
      ...init,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  closest(selector) {
    const match = /^\[([^\]]+)\]$/.exec(selector);
    return match && this.hasAttribute(match[1]) ? this : null;
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: 1000, height: 600 };
  }
  getContext() {
    return this.context;
  }
  setPointerCapture() {}
  releasePointerCapture() {}
}

export function installFakeDom() {
  const windowListeners = new Map();
  globalThis.document = {
    hidden: false,
    head: { appendChild() {} },
    createElement: (tagName) => new FakeElement(tagName),
  };
  globalThis.window = {
    devicePixelRatio: 1,
    addEventListener(type, listener) {
      const list = windowListeners.get(type) || [];
      list.push(listener);
      windowListeners.set(type, list);
    },
    removeEventListener(type, listener) {
      const list = windowListeners.get(type) || [];
      windowListeners.set(type, list.filter((item) => item !== listener));
    },
  };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
  globalThis.requestAnimationFrame = (callback) =>
    setTimeout(() => callback(Date.now()), 0);
}

export async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(message);
}
""",
        encoding="utf-8",
    )


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_shared_detail_and_lossless_cross_view_contracts(tmp_path):
    """Timeline and Navigator consume one detail model and preserve span id."""
    pkg = _module_dir(tmp_path)
    (pkg / "harness.mjs").write_text(
        r"""
import {
  normalizeSpanDetail,
  renderSpanDetail,
  spanDetailFields,
  buildNavigatorRevealTarget,
  buildTimelineRevealTarget,
} from "./phoenix.js";

const raw = {
  id: "phoenix-node-7",
  name: "web_search",
  spanKind: "TOOL",
  startTime: "2026-07-24T12:00:00.000Z",
  endTime: "2026-07-24T12:00:00.125Z",
  latencyMs: 125,
  statusCode: "ERROR",
  parentId: "parent-span",
  context: { spanId: "exact-span", traceId: "trace-1" },
  attributes: JSON.stringify({
    kestrel: {
      agent_name: "talon/implement",
      stage: "implement",
      session_id: "session-1",
      tool_count: 3,
      error_count: 1,
      success_ratio: 0.666,
    },
    llm: { model_name: "gpt-5" },
    input: { value: "already returned by Phoenix" },
    output: { value: "tool output" },
  }),
};
const detail = normalizeSpanDetail(raw, {
  projectId: "project-node",
  projectName: "owner/repo",
});
const sparse = normalizeSpanDetail({
  id: "sparse-node",
  name: "marker",
  startTime: "2026-07-24T12:00:01.000Z",
  endTime: "2026-07-24T12:00:01.000Z",
  context: { spanId: "sparse-span", traceId: "trace-2" },
  attributes: "{}",
});
process.stdout.write(JSON.stringify({
  detail,
  fields: spanDetailFields(detail),
  html: renderSpanDetail(detail),
  compactHtml: renderSpanDetail(detail, { rawAttributes: false }),
  sparse,
  sparseFields: spanDetailFields(sparse),
  sparseHtml: renderSpanDetail(sparse),
  navigatorTarget: buildNavigatorRevealTarget(detail),
  workerlessNavigatorTarget: buildNavigatorRevealTarget({
    ...detail,
    worker: null,
  }),
  timelineTarget: buildTimelineRevealTarget(detail),
}));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "harness.mjs")],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    detail = result["detail"]
    assert detail["spanId"] == "exact-span"
    assert detail["parentSpanId"] == "parent-span"
    assert detail["nodeId"] == "phoenix-node-7"
    assert detail["agent"] == "talon"
    assert detail["worker"] == "implement"
    assert detail["sessionId"] == "session-1"
    assert detail["model"] == "gpt-5"
    assert detail["input"] == "already returned by Phoenix"
    assert detail["output"] == "tool output"
    assert detail["stats"] == {
        "turnCount": None,
        "toolCount": 3,
        "errorCount": 1,
        "successRatio": 0.666,
    }
    labels = [field["label"] for field in result["fields"]]
    assert labels == [
        "name",
        "kind",
        "status",
        "state",
        "started",
        "ended",
        "duration",
        "agent",
        "worker",
        "model",
        "project",
        "session",
        "trace ID",
        "span ID",
        "parent span ID",
        "tools",
        "errors",
        "success",
    ]
    assert "already returned by Phoenix" in result["html"]
    assert "Raw attributes" in result["html"]
    assert "Raw attributes" not in result["compactHtml"]

    # Missing optional fields/I/O are omitted, never rendered as empty labels.
    assert result["sparse"]["input"] is None
    assert result["sparse"]["output"] is None
    sparse_labels = [field["label"] for field in result["sparseFields"]]
    assert "agent" not in sparse_labels
    assert "model" not in sparse_labels
    assert "session" not in sparse_labels
    assert "input.value" not in result["sparseHtml"]
    assert "output.value" not in result["sparseHtml"]

    # Timeline → Navigator → Timeline keeps the same authoritative OTel span id.
    nav = result["navigatorTarget"]
    timeline = result["timelineTarget"]
    assert nav["spanId"] == timeline["spanId"] == "exact-span"
    assert nav["nodeId"] == timeline["nodeId"] == "phoenix-node-7"
    assert nav["projectId"] == timeline["projectId"] == "project-node"
    assert nav["startTime"] == timeline["startTime"] == 1784894400000
    assert nav["agentName"] == "talon"
    assert nav["sessionId"] == "session-1"
    assert result["workerlessNavigatorTarget"]["worker"] is None


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_exact_event_path_and_honest_turn_fallback(tmp_path):
    """The shipped resolver expands an exact Event path or returns only Turn."""
    pkg = _module_dir(tmp_path)
    (pkg / "reveal.mjs").write_text(
        r"""
import { resolveExactSpanReveal } from "./navigator.js";

const span = (nodeId, spanId) => ({ id: nodeId, context: { spanId } });
const turn = {
  kind: "turn",
  data: { span: span("turn-node", "turn-span") },
  children: [],
};
const parent = {
  kind: "event",
  data: { span: span("parent-node", "parent-span") },
  children: [],
};
const leaf = {
  kind: "event",
  data: { span: span("leaf-node", "leaf-span") },
  children: [],
};
turn.children.push(parent);
parent.children.push(leaf);

const exact = resolveExactSpanReveal(turn, {
  spanId: "leaf-span",
  nodeId: "leaf-node",
});
const missing = resolveExactSpanReveal(turn, {
  spanId: "missing-span",
  nodeId: "leaf-node",
});
const byNode = resolveExactSpanReveal(turn, { nodeId: "parent-node" });
const turnHit = resolveExactSpanReveal(turn, { spanId: "turn-span" });
const summarize = (r) => ({
  exact: r.exact,
  selectedKind: r.node && r.node.kind,
  selectedSpan: r.node && r.node.data.span.context.spanId,
  path: r.path.map((n) => n.data.span.context.spanId),
});
process.stdout.write(JSON.stringify({
  exact: summarize(exact),
  missing: summarize(missing),
  byNode: summarize(byNode),
  turnHit: summarize(turnHit),
}));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "reveal.mjs")],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    assert result["exact"] == {
        "exact": True,
        "selectedKind": "event",
        "selectedSpan": "leaf-span",
        "path": ["turn-span", "parent-span", "leaf-span"],
    }
    # spanId is authoritative: a conflicting Phoenix node id must not select a
    # different event. The only honest fallback is the containing Turn.
    assert result["missing"] == {
        "exact": False,
        "selectedKind": "turn",
        "selectedSpan": "turn-span",
        "path": ["turn-span"],
    }
    assert result["byNode"]["selectedSpan"] == "parent-span"
    assert result["byNode"]["exact"] is True
    assert result["turnHit"]["selectedSpan"] == "turn-span"
    assert result["turnHit"]["exact"] is True


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_mounted_workerless_talon_root_reveals_stageless_session(tmp_path):
    """A workerless Timeline target must not resolve through a worker bucket."""
    pkg = _module_dir(tmp_path)
    _write_fake_dom(pkg)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "talon_trace.json"
    (pkg / "mounted-navigator.mjs").write_text(
        r"""
import { readFileSync } from "node:fs";
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";

installFakeDom();
const fixture = JSON.parse(readFileSync(process.argv[2], "utf8"));
const spans = fixture.talon_trace;
const root = spans[0];
const spanPageCalls = [];

globalThis.fetch = async (_url, options) => {
  const { query, variables = {} } = JSON.parse(options.body);
  let data;
  if (query.includes("NavigatorProjects")) {
    data = {
      projects: {
        edges: [{
          node: {
            id: "project-1",
            name: "UncleSaurus/widget",
            traceCount: 1,
            endTime: root.endTime,
          },
        }],
      },
    };
  } else if (query.includes("NavigatorTraceSpans")) {
    data = { node: { trace: { spans: { edges: spans.map((node) => ({ node })) } } } };
  } else if (query.includes("NavigatorSpanPage")) {
    spanPageCalls.push(variables);
    const page = variables.filter && !variables.rootOnly ? spans : [root];
    data = {
      node: {
        spans: {
          edges: page.map((node) => ({ node })),
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
    };
  } else {
    throw new Error("unexpected GraphQL operation");
  }
  return { status: 200, ok: true, json: async () => ({ data }) };
};

const {
  buildNavigatorRevealTarget,
  normalizeSpanDetail,
} = await import("./phoenix.js");
const { mount } = await import("./navigator.js");
const target = buildNavigatorRevealTarget(
  normalizeSpanDetail(root, {
    projectId: "project-1",
    projectName: "UncleSaurus/widget",
  }),
);
const container = new FakeElement("div");
const mounted = mount(container, { revealTarget: target });
const inspector = container.querySelector("[data-inspector]");
const spacer = container.querySelector("[data-spacer]");
await waitFor(
  () => inspector.innerHTML.includes("aaaa000000000001"),
  "workerless Talon root was not selected",
);
mounted.destroy();

process.stdout.write(JSON.stringify({
  target,
  spanPageCalls,
  inspector: inspector.innerHTML,
  tree: spacer.innerHTML,
}));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "mounted-navigator.mjs"), str(fixture)],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    assert result["target"]["worker"] is None
    calls = result["spanPageCalls"]
    assert calls[0]["rootOnly"] is True  # Fleet → Agent
    assert calls[1]["rootOnly"] is False  # Agent → worker/stageless split
    # The next mounted drill must be the exact-agent, root-only stageless
    # bucket. Before the fix this was a worker filter (gate for this fixture).
    assert calls[2]["rootOnly"] is True
    assert calls[2]["filter"] == (
        'attributes["kestrel"]["agent_name"] == \'talon\''
    )
    assert ">worker<" not in result["inspector"]
    assert "obs-nav__row--selected" in result["tree"]
    assert "UncleSaurus/widget#7" in result["tree"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_mounted_timeline_hides_navigator_action_without_session(tmp_path):
    """An orphan span may open a popover but cannot promise a Navigator reveal."""
    pkg = _module_dir(tmp_path)
    _write_fake_dom(pkg)
    (pkg / "mounted-timeline.mjs").write_text(
        r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";

installFakeDom();
const start = Date.now() - 20_000;
const orphan = {
  id: "orphan-node",
  name: "orphan tool",
  spanKind: "tool",
  startTime: new Date(start).toISOString(),
  endTime: new Date(start + 15_000).toISOString(),
  latencyMs: 15_000,
  statusCode: "OK",
  parentId: "not-loaded-parent",
  attributes: JSON.stringify({
    openinference: { span: { kind: "TOOL" } },
    kestrel: { agent_name: "talon/implement", stage: "implement" },
  }),
  context: { spanId: "orphan-span", traceId: "orphan-trace" },
};

globalThis.fetch = async (_url, options) => {
  const { query } = JSON.parse(options.body);
  let data;
  if (query.includes("NavigatorProjects")) {
    data = {
      projects: {
        edges: [{
          node: {
            id: "project-1",
            name: "owner/repo",
            traceCount: 1,
            endTime: orphan.endTime,
          },
        }],
      },
    };
  } else if (query.includes("NavigatorSpanPage")) {
    data = {
      node: {
        spans: {
          edges: [{ node: orphan }],
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
    };
  } else {
    throw new Error("unexpected GraphQL operation");
  }
  return { status: 200, ok: true, json: async () => ({ data }) };
};

const { mount } = await import("./timeline.js");
const container = new FakeElement("div");
const mounted = mount(container, {
  openNavigator() {
    throw new Error("unfulfillable Navigator action was invoked");
  },
  openTrace() {},
  revealTarget: {
    projectId: "project-1",
    projectName: "owner/repo",
    spanId: "orphan-span",
    startTime: start,
  },
});
const canvas = container.querySelector("[data-canvas]");
const popover = container.querySelector("[data-pop]");
const notice = container.querySelector("[data-reveal-notice]");
await waitFor(
  () => notice.textContent.includes("highlighted"),
  "Timeline orphan did not finish mounting",
);

// The reveal window centers the orphan at x=584; the worker lane is y=77..102.
const pointer = {
  button: 0,
  pointerId: 1,
  clientX: 650,
  clientY: 85,
  offsetX: 650,
  offsetY: 85,
};
canvas.dispatch("pointerdown", pointer);
canvas.dispatch("pointerup", pointer);
await waitFor(() => popover.innerHTML.includes("orphan tool"), "popover did not open");
mounted.destroy();

process.stdout.write(JSON.stringify({
  popover: popover.innerHTML,
  hasNavigatorButton: Boolean(popover.querySelector("[data-pnav]")),
  hasPhoenixButton: Boolean(popover.querySelector("[data-pphx]")),
}));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "mounted-timeline.mjs")],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    assert "orphan tool" in result["popover"]
    assert result["hasNavigatorButton"] is False
    assert result["hasPhoenixButton"] is True


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_mounted_timeline_redraws_async_exact_reveal_without_input(tmp_path):
    """Async reveal data paints centered/highlighted; fallbacks stay honest."""
    pkg = _module_dir(tmp_path)
    _write_fake_dom(pkg)
    (pkg / "async-timeline-reveal.mjs").write_text(
        r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";

installFakeDom();

const actualStart = Date.now() - 120_000;
const iso = (value) => new Date(value).toISOString();
const makeSpan = ({
  id,
  spanId,
  agent,
  name = "loaded tool",
  start = actualStart,
  duration = 10_000,
  parentId = null,
  attributes = null,
}) => ({
  id,
  name,
  spanKind: "TOOL",
  startTime: iso(start),
  endTime: iso(start + duration),
  latencyMs: duration,
  statusCode: "OK",
  parentId,
  attributes: JSON.stringify(
    attributes || {
      openinference: { span: { kind: "TOOL" } },
      kestrel: { agent_name: agent },
    },
  ),
  context: { spanId, traceId: `trace-${spanId}` },
});

const exactSpans = Array.from({ length: 30 }, (_unused, index) =>
  makeSpan({
    id: `node-${index}`,
    spanId: index === 15 ? "exact-span" : `span-${index}`,
    agent: `agent-${String(index).padStart(2, "0")}`,
    name: index === 15 ? "exact loaded target" : `background ${index}`,
    start: actualStart + index,
  }),
);

let scenario = "exact";
let activeSpans = exactSpans;
let releaseProjects;
let projectsReleased = false;
const projectGate = new Promise((resolve) => {
  releaseProjects = () => {
    projectsReleased = true;
    resolve();
  };
});

globalThis.fetch = async (_url, options) => {
  const { query } = JSON.parse(options.body);
  if (query.includes("NavigatorProjects")) {
    if (scenario === "exact" && !projectsReleased) await projectGate;
    const data = {
      projects: {
        edges: [{
          node: {
            id: "project-1",
            name: "owner/repo",
            traceCount: activeSpans.length,
            endTime: activeSpans[0] && activeSpans[0].endTime,
          },
        }],
      },
    };
    return { status: 200, ok: true, json: async () => ({ data }) };
  }
  if (query.includes("NavigatorSpanPage")) {
    const data = {
      node: {
        spans: {
          edges: activeSpans.map((node) => ({ node })),
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
    };
    return { status: 200, ok: true, json: async () => ({ data }) };
  }
  throw new Error("unexpected GraphQL operation");
};

const { mount } = await import("./timeline.js");
const exactContainer = new FakeElement("div");
const exactMounted = mount(exactContainer, {
  revealTarget: {
    projectId: "project-1",
    projectName: "owner/repo",
    spanId: "exact-span",
    // Deliberately stale navigation metadata: finishReveal() must recenter on
    // the loaded span's authoritative start, ten seconds later.
    startTime: actualStart - 10_000,
  },
});
const exactCanvas = exactContainer.querySelector("[data-canvas]");
const exactNotice = exactContainer.querySelector("[data-reveal-notice]");
const exactLive = exactContainer.querySelector("[data-live]");
await waitFor(
  () => exactCanvas.context.frames.length > 0,
  "Timeline did not paint its pre-load frame",
);
const preLoadFrameCount = exactCanvas.context.frames.length;
const preLoadOperations = exactCanvas.context.frames.flatMap((frame) => frame.operations);

releaseProjects();
await waitFor(
  () => exactNotice.textContent.includes("exact-span highlighted"),
  "Timeline did not finish the exact reveal",
);
await waitFor(
  () => exactCanvas.context.frames.some((frame, index) =>
    index >= preLoadFrameCount &&
    frame.operations.some(
      (operation) =>
        operation.type === "strokeRect" &&
        operation.strokeStyle === "#facc15",
    ),
  ),
  "Timeline did not redraw the loaded exact-span highlight",
);

const highlightedFrameIndex = exactCanvas.context.frames.findIndex(
  (frame, index) =>
    index >= preLoadFrameCount &&
    frame.operations.some(
      (operation) =>
        operation.type === "strokeRect" &&
        operation.strokeStyle === "#facc15",
    ),
);
const highlightedFrame = exactCanvas.context.frames[highlightedFrameIndex];
const highlightStroke = highlightedFrame.operations.find(
  (operation) =>
    operation.type === "strokeRect" &&
    operation.strokeStyle === "#facc15",
);
const targetFill = highlightedFrame.operations.find(
  (operation) =>
    operation.type === "fillRect" &&
    operation.fillStyle === "#f59e0b" &&
    Math.abs(operation.args[0] - (highlightStroke.args[0] + 2)) < 0.01 &&
    Math.abs(operation.args[1] - (highlightStroke.args[1] + 2)) < 0.01,
);
const exactResult = {
  preLoadFrameCount,
  preLoadHadTarget: preLoadOperations.some(
    (operation) =>
      operation.type === "fillRect" && operation.fillStyle === "#f59e0b",
  ),
  preLoadHadHighlight: preLoadOperations.some(
    (operation) =>
      operation.type === "strokeRect" && operation.strokeStyle === "#facc15",
  ),
  preLoadText: preLoadOperations
    .filter((operation) => operation.type === "fillText")
    .map((operation) => operation.args[0]),
  highlightedFrameIndex,
  highlightStroke,
  targetFill,
  livePressed: exactLive.attributes.get("aria-pressed"),
  liveClass: exactLive.classList.values.has("obs-tl__btn--on"),
  notice: exactNotice.textContent,
};
exactMounted.destroy();

async function mountFallback(kind, target, spans, noticeText) {
  scenario = kind;
  activeSpans = spans;
  const container = new FakeElement("div");
  const mounted = mount(container, { revealTarget: target });
  const canvas = container.querySelector("[data-canvas]");
  const notice = container.querySelector("[data-reveal-notice]");
  await waitFor(
    () => notice.textContent.includes(noticeText),
    `${kind} reveal did not show its fallback notice`,
  );
  await waitFor(
    () => canvas.context.frames.some((frame) =>
      frame.operations.some(
        (operation) =>
          operation.type === "fillRect" &&
          operation.fillStyle === "#f59e0b",
      ),
    ),
    `${kind} reveal did not paint its post-load frame`,
  );
  const result = {
    notice: notice.textContent,
    highlighted: canvas.context.frames.some((frame) =>
      frame.operations.some(
        (operation) =>
          operation.type === "strokeRect" &&
          operation.strokeStyle === "#facc15",
      ),
    ),
    paintedOther: canvas.context.frames.some((frame) =>
      frame.operations.some(
        (operation) =>
          operation.type === "fillRect" &&
          operation.fillStyle === "#f59e0b",
      ),
    ),
  };
  mounted.destroy();
  return result;
}

const otherSpan = makeSpan({
  id: "other-node",
  spanId: "other-span",
  agent: "agent-other",
});
const missingResult = await mountFallback(
  "missing",
  {
    projectId: "project-1",
    projectName: "owner/repo",
    spanId: "missing-span",
    startTime: actualStart,
  },
  [otherSpan],
  "could not be loaded",
);
const foldedSummary = makeSpan({
  id: "summary-node",
  spanId: "summary-span",
  agent: "agent-summary",
  name: "session summary",
  duration: 12_000,
});
const foldedResult = await mountFallback(
  "folded",
  {
    projectId: "project-1",
    projectName: "owner/repo",
    spanId: "summary-span",
    startTime: actualStart,
  },
  [otherSpan, foldedSummary],
  "folded into its owning Timeline band",
);

process.stdout.write(JSON.stringify({
  exact: exactResult,
  missing: missingResult,
  folded: foldedResult,
}));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "async-timeline-reveal.mjs")],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    exact = result["exact"]
    assert exact["preLoadFrameCount"] >= 1
    assert exact["preLoadHadTarget"] is False
    assert exact["preLoadHadHighlight"] is False
    assert "No spans in this window" in exact["preLoadText"]
    assert exact["highlightedFrameIndex"] >= exact["preLoadFrameCount"]
    assert exact["targetFill"] is not None
    assert exact["highlightStroke"]["strokeStyle"] == "#facc15"
    # 1000px canvas - 168px gutter => the loaded start is centered at x=584.
    assert exact["targetFill"]["args"][0] == pytest.approx(584)
    # Thirty lanes force a vertical scroll; the selected middle lane is centered.
    assert 275 < exact["targetFill"]["args"][1] < 325
    assert exact["livePressed"] == "false"
    assert exact["liveClass"] is False
    assert "exact-span highlighted" in exact["notice"]

    for fallback in (result["missing"], result["folded"]):
        assert fallback["paintedOther"] is True
        assert fallback["highlighted"] is False
        assert "no other span was highlighted" in fallback["notice"]
