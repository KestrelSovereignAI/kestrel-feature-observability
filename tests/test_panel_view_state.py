"""Panel view-state persistence contracts (#86), executed against the shipped JS.

The observability panel used to lose everything on a remount: bouncing to the
Phoenix sub-tab and back rebuilt a *fresh* Timeline (default zoom, live-follow
on, no drill), and a reload forgot the panel entirely. Only the sub-tab was
persisted, through a bespoke raw-``localStorage`` key.

Both halves of the fix are pinned here by running the real modules under node:

- ``timeline.js`` exposes its serializable view state on its mount handle, and
  ``setState`` on a *fresh* mount restores the same window/anchor/live flag — a
  live snapshot resumes live (re-anchored on the wall clock), a paused one
  restores its historical window paused, and junk falls back to the defaults.
- ``observability.js`` round-trips the active sub-tab (and the Timeline slice)
  through the console's ``registerPanel({viewState})`` provider — with raw
  ``localStorage`` proven untouched and the URL hash cleared, so only the
  provider can be carrying the state.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from test_span_navigation_contract import _module_dir, _write_fake_dom

STATIC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "kestrel_feature_observability"
    / "fleet"
    / "static"
)
NODE = shutil.which("node")

DEFAULT_WINDOW_MS = 30 * 60 * 1000
MIN_WINDOW_MS = 60 * 1000


def _observability_module_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """observability.js with its three console imports and both views stubbed.

    The panel's own logic (sub-tab selection, the view-state port, handing the
    Timeline slice to each Timeline mount) is what's under test here; the view
    modules are doubles so a sub-tab switch is observable without dragging the
    whole canvas/GraphQL stack in. ``timeline.js``'s real ``getState``/
    ``setState`` are covered by the mounted test above.
    """
    pkg = tmp_path / "obs-view-state"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"type":"module"}', encoding="utf-8")

    source = (STATIC / "observability.js").read_text(encoding="utf-8")
    for original, replacement in (
        (
            'import { registerPanel } from "/js/ui-ext/panels.js";',
            'import { registerPanel } from "./stub-panels.js";',
        ),
        (
            'import { storeGet } from "/js/ui_state.mjs";',
            'import { storeGet } from "./stub-ui-state.js";',
        ),
        ('import API from "/js/api.js";', 'import API from "./stub-api.js";'),
        (
            'import { createStopController, mountStopActionBar } from "./stop-actions.js";',
            'import { createStopController, mountStopActionBar } from "./stub-stop-actions.js";',
        ),
    ):
        assert original in source, f"observability.js import changed: {original}"
        source = source.replace(original, replacement)
    (pkg / "observability.js").write_text(source, encoding="utf-8")

    (pkg / "stub-panels.js").write_text(
        'export function registerPanel(def) { globalThis.__panels.push(def); }\n',
        encoding="utf-8",
    )
    (pkg / "stub-ui-state.js").write_text(
        "export function storeGet(key) { globalThis.__uiStateReads.push(key); return null; }\n"
        "export function storeSet(key, value) { globalThis.__uiStateWrites.push([key, value]); }\n",
        encoding="utf-8",
    )
    (pkg / "stub-api.js").write_text(
        'export default { requestHost: async () => { throw new Error("no phoenix"); } };\n',
        encoding="utf-8",
    )
    (pkg / "stub-stop-actions.js").write_text(
        "export function createStopController() { return {}; }\n"
        "export function mountStopActionBar() { return { destroy() {} }; }\n",
        encoding="utf-8",
    )
    # Timeline double: records every mount, its opts, and the state handed to it,
    # and lets the test rewrite `state` to stand in for a user zoom/pan.
    (pkg / "timeline.js").write_text(
        r"""
export function mount(container, opts) {
  const record = { opts, state: null, setStateCalls: 0, destroyed: false };
  globalThis.__timelineMounts.push(record);
  return {
    destroy() { record.destroyed = true; },
    getState() { return record.state; },
    setState(state) { record.state = state; record.setStateCalls += 1; },
  };
}
""",
        encoding="utf-8",
    )
    (pkg / "navigator.js").write_text(
        r"""
export function mount(container, opts) {
  const record = { opts, destroyed: false };
  globalThis.__navigatorMounts.push(record);
  return { destroy() { record.destroyed = true; } };
}
""",
        encoding="utf-8",
    )
    return pkg


_OBS_FAKE_DOM = r"""
class ClassList {
  constructor() { this.names = new Set(); }
  toggle(name, force) {
    const on = force === undefined ? !this.names.has(name) : Boolean(force);
    if (on) this.names.add(name); else this.names.delete(name);
    return on;
  }
  contains(name) { return this.names.has(name); }
}

export class Element {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.dataset = {};
    this.style = { setProperty() {} };
    this.classList = new ClassList();
    this.listeners = new Map();
    this.children = [];
    this.textContent = "";
    this._innerHTML = "";
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
    for (const match of this._innerHTML.matchAll(/data-view="([\w-]+)"/g)) {
      const button = new Element("button");
      button.dataset.view = match[1];
      button.selector = "[data-view]";
      this.children.push(button);
    }
    if (this._innerHTML.includes("data-obs-content")) {
      const content = new Element("div");
      content.selector = "[data-obs-content]";
      this.children.push(content);
    }
  }
  get innerHTML() { return this._innerHTML; }
  querySelector(selector) {
    return this.children.find((child) => child.selector === selector) || null;
  }
  querySelectorAll(selector) {
    return this.children.filter((child) => child.selector === selector);
  }
  setAttribute() {}
  addEventListener(type, listener) {
    const list = this.listeners.get(type) || [];
    list.push(listener);
    this.listeners.set(type, list);
  }
  dispatch(type) {
    for (const listener of this.listeners.get(type) || []) listener({ target: this });
  }
}

export function installObsDom() {
  globalThis.__panels = [];
  globalThis.__uiStateReads = [];
  globalThis.__uiStateWrites = [];
  globalThis.__timelineMounts = [];
  globalThis.__navigatorMounts = [];
  globalThis.__rawStorageCalls = [];
  globalThis.document = {
    head: { appendChild() {} },
    createElement: (tagName) => new Element(tagName),
  };
  globalThis.location = { hash: "" };
  // A raw-localStorage tripwire: nothing in observability.js may reach it.
  globalThis.localStorage = {
    getItem(key) { globalThis.__rawStorageCalls.push(["get", key]); return null; },
    setItem(key, value) { globalThis.__rawStorageCalls.push(["set", key, value]); },
    removeItem(key) { globalThis.__rawStorageCalls.push(["remove", key]); },
  };
}

export const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

export function activeTab(container) {
  const active = container
    .querySelectorAll("[data-view]")
    .filter((button) => button.classList.contains("obs-subnav__tab--active"));
  return active.length === 1 ? active[0].dataset.view : null;
}

export function clickTab(container, view) {
  const button = container
    .querySelectorAll("[data-view]")
    .find((candidate) => candidate.dataset.view === view);
  if (!button) throw new Error(`no sub-tab button for ${view}`);
  button.dispatch("click");
}
"""


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_timeline_view_state_round_trips_zoom_pan_and_live(tmp_path):
    """A zoom+pan snapshot restores to the same window/anchor/live on a remount."""
    pkg = _module_dir(tmp_path)
    _write_fake_dom(pkg)
    (pkg / "timeline-view-state.mjs").write_text(
        r"""
import { FakeElement, installFakeDom, waitFor } from "./fake-dom.mjs";

installFakeDom();

const iso = (value) => new Date(value).toISOString();
const spanStart = Date.now() - 10 * 60 * 1000;
const spans = Array.from({ length: 3 }, (_unused, index) => ({
  id: `node-${index}`,
  name: `tool ${index}`,
  spanKind: "TOOL",
  startTime: iso(spanStart + index * 1000),
  endTime: iso(spanStart + index * 1000 + 500),
  latencyMs: 500,
  statusCode: "OK",
  parentId: null,
  attributes: JSON.stringify({
    openinference: { span: { kind: "TOOL" } },
    kestrel: { agent_name: "Claw", session_id: "sess-1" },
  }),
  context: { spanId: `span-${index}`, traceId: "trace-1" },
}));

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
            traceCount: spans.length,
            endTime: spans[spans.length - 1].endTime,
          },
        }],
      },
    };
  } else if (query.includes("NavigatorSpanPage")) {
    data = {
      node: { spans: { edges: spans.map((node) => ({ node })), pageInfo: { hasNextPage: false, endCursor: null } } },
    };
  } else {
    throw new Error("unexpected GraphQL operation");
  }
  return { status: 200, ok: true, json: async () => ({ data }) };
};

const { mount } = await import("./timeline.js");

// boot() ends with setLive(), which is the first thing to stamp aria-pressed.
const booted = (container) =>
  waitFor(
    () => container.querySelector("[data-live]").hasAttribute("aria-pressed"),
    "timeline did not finish booting",
  );

const mountedAt = Date.now();
const out = {};
const handles = [];

// ── 1. Zoom + pan a live Timeline, then snapshot it. ──
const first = new FakeElement("div");
const live = mount(first, {});
handles.push(live);
await booted(first);
out.bootState = live.getState();
const canvas = first.querySelector("[data-canvas]");
// ctrl+wheel → zoom in around the cursor (one 1/1.15 step per event). Plain
// scroll never zooms since #94, so the modifier is what reaches zoomAt().
for (let i = 0; i < 5; i += 1) {
  canvas.dispatch("wheel", { deltaY: -120, deltaX: 0, offsetX: 600, ctrlKey: true });
}
// Horizontal wheel → pan back into history, which pauses live-follow.
canvas.dispatch("wheel", { deltaY: 0, deltaX: -300, offsetX: 600 });
const pausedState = live.getState();
out.pausedState = pausedState;

// #94: a PLAIN vertical wheel scrolls lanes — it must not touch the window or
// the pan anchor (it used to zoom, which is what a Magic Mouse tripped).
canvas.dispatch("wheel", { deltaY: 240, deltaX: 0, offsetX: 600 });
const afterPlainScroll = live.getState();
out.plainScrollKeptWindow = afterPlainScroll.windowMs === pausedState.windowMs;
out.plainScrollKeptViewEnd = afterPlainScroll.viewEnd === pausedState.viewEnd;
out.plainScrollStayedPaused = afterPlainScroll.live === false;

// #94: panning back to the present edge is magnetic — it snaps to `now` and
// re-engages Live rather than clamping there paused.
canvas.dispatch("wheel", { deltaY: 0, deltaX: 900_000, offsetX: 600 });
out.magneticLive = live.getState().live;

// ── 2. A fresh mount restores that exact paused window. ──
const second = new FakeElement("div");
const restored = mount(second, {});
handles.push(restored);
restored.setState({
  ...pausedState,
  collapsed: ["owner/repo"],
  laneScrollY: 0,
  highlightedSpanId: "span-1",
});
await booted(second);
out.restoredState = restored.getState();
out.restoredLiveButtonOn = second
  .querySelector("[data-live]")
  .classList.values.has("obs-tl__btn--on");

// ── 3. A state captured LIVE resumes live, re-anchored on the wall clock. ──
const third = new FakeElement("div");
const resumed = mount(third, {});
handles.push(resumed);
resumed.setState({ windowMs: 120000, viewEnd: mountedAt - 3600_000, live: true });
await booted(third);
out.resumedState = resumed.getState();
out.resumedIsRecent = resumed.getState().viewEnd >= mountedAt;

// ── 4. Missing / stale state degrades to today's defaults. ──
const fourth = new FakeElement("div");
const defaulted = mount(fourth, {});
handles.push(defaulted);
out.junkRejected = defaulted.setState(null);
defaulted.setState({ windowMs: "nonsense", viewEnd: "nonsense" });
await booted(fourth);
out.defaultState = defaulted.getState();

// ── 5. An explicit cross-view reveal outranks a stored window. ──
const fifth = new FakeElement("div");
const revealed = mount(fifth, {
  revealTarget: { projectId: "project-1", spanId: "span-1", startTime: spanStart + 1000 },
});
handles.push(revealed);
revealed.setState({ ...pausedState, collapsed: ["owner/repo"], highlightedSpanId: "elsewhere" });
out.revealStateAfterSet = { windowMs: revealed.getState().windowMs, live: revealed.getState().live };
out.revealCollapsed = revealed.getState().collapsed;

for (const handle of handles) handle.destroy();
process.stdout.write(JSON.stringify(out));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "timeline-view-state.mjs")],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    # A normal entry boots live at the default window.
    assert result["bootState"]["live"] is True
    assert result["bootState"]["windowMs"] == DEFAULT_WINDOW_MS

    # Zoom shrank the window; the pan moved the right edge back and paused live.
    paused = result["pausedState"]
    assert paused["live"] is False
    assert MIN_WINDOW_MS <= paused["windowMs"] < DEFAULT_WINDOW_MS
    assert paused["viewEnd"] < result["bootState"]["viewEnd"]

    # #94: a plain vertical wheel is a LANE scroll — the persisted window/anchor
    # (and the paused flag) are untouched, so scrolling can't rescale the view.
    assert result["plainScrollKeptWindow"] is True
    assert result["plainScrollKeptViewEnd"] is True
    assert result["plainScrollStayedPaused"] is True
    # ...while a time-pan that reaches the present edge re-engages Live.
    assert result["magneticLive"] is True

    # ...and a fresh mount comes back to exactly that window/anchor/live flag.
    restored = result["restoredState"]
    assert restored["windowMs"] == paused["windowMs"]
    assert restored["viewEnd"] == paused["viewEnd"]
    assert restored["live"] is False
    assert result["restoredLiveButtonOn"] is False
    # The drill/expanded selection rides along.
    assert restored["collapsed"] == ["owner/repo"]
    assert restored["highlightedSpanId"] == "span-1"

    # A live snapshot resumes live rather than restoring its stale right edge.
    assert result["resumedState"]["live"] is True
    assert result["resumedState"]["windowMs"] == 120000
    assert result["resumedIsRecent"] is True

    # Junk is rejected outright and unusable numbers fall back to the defaults.
    assert result["junkRejected"] is False
    assert result["defaultState"]["windowMs"] == DEFAULT_WINDOW_MS
    assert result["defaultState"]["live"] is True

    # An exact cross-view reveal keeps its own paused, zoomed-in window; only the
    # collapse set restores alongside it.
    assert result["revealStateAfterSet"] == {"windowMs": MIN_WINDOW_MS, "live": False}
    assert result["revealCollapsed"] == ["owner/repo"]


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_observability_round_trips_subtab_through_the_provider(tmp_path):
    """Sub-tab + Timeline slice survive a remount and a reload via the provider."""
    pkg = _observability_module_dir(tmp_path)
    (pkg / "obs-dom.mjs").write_text(_OBS_FAKE_DOM, encoding="utf-8")
    (pkg / "obs-view-state.mjs").write_text(
        r"""
import { Element, installObsDom, tick, activeTab, clickTab } from "./obs-dom.mjs";

installObsDom();
const out = {};

// ── Session 1 ──
const first = await import("./observability.js?session=1");
const contribution = globalThis.__panels[0];
const provider = contribution.viewState;
out.panelId = contribution.panelId;
out.providerShape = {
  key: provider.key,
  getState: typeof provider.getState,
  setState: typeof provider.setState,
};

const container = new Element("div");
const panel = first.mount(container);
await tick();
out.defaultTab = activeTab(container);

// Stand in for a zoom+pan inside the live Timeline.
const zoomed = {
  windowMs: 300000,
  viewEnd: 1700000000000,
  live: false,
  laneScrollY: 42,
  collapsed: ["owner/repo"],
  highlightedSpanId: "span-7",
};
globalThis.__timelineMounts[0].state = zoomed;

// ── Sub-tab remount: Timeline → Phoenix → Timeline ──
clickTab(container, "phoenix");
out.timelineDestroyedOnSwitch = globalThis.__timelineMounts[0].destroyed;
clickTab(container, "timeline");
await tick();
out.timelineMountCount = globalThis.__timelineMounts.length;
out.remountRestored = globalThis.__timelineMounts[1].state;

// ── Snapshot through the provider, then tear the mount down (a "reload"). ──
clickTab(container, "navigator");
const saved = provider.getState();
out.saved = saved;
panel.destroy();
out.savedAfterDestroy = provider.getState();

// ── Session 2: a fresh module instance, and NO hash to fall back on. ──
globalThis.location.hash = "";
const second = await import("./observability.js?session=2");
const provider2 = globalThis.__panels[1].viewState;
out.noStateYet = provider2.getState() === undefined;

const container2 = new Element("div");
second.mount(container2);
provider2.setState(saved); // exactly what panels.js does right after render
await tick();
out.restoredTab = activeTab(container2);
out.restoredHash = globalThis.location.hash;
// The restored sub-tab mounts DIRECTLY — no Timeline flash on the way.
out.timelineMountCountAfterRestore = globalThis.__timelineMounts.length;

// ...and dropping into the Timeline hands it the restored slice.
clickTab(container2, "timeline");
await tick();
out.timelineStateAfterRestore =
  globalThis.__timelineMounts[globalThis.__timelineMounts.length - 1].state;

out.rawStorageCalls = globalThis.__rawStorageCalls;
out.uiStateReads = globalThis.__uiStateReads;

process.stdout.write(JSON.stringify(out));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(pkg / "obs-view-state.mjs")],
        cwd=pkg,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(proc.stdout)

    # The panel registers a well-formed view-state provider.
    assert result["panelId"] == "observability"
    assert result["providerShape"] == {"key": "view", "getState": "function", "setState": "function"}

    # A fresh console opens on Timeline.
    assert result["defaultTab"] == "timeline"

    # Phoenix → Timeline is a real remount, and the new Timeline is handed the
    # exact state the old one held (the #86 "fresh Timeline" regression).
    assert result["timelineDestroyedOnSwitch"] is True
    assert result["timelineMountCount"] == 2
    assert result["remountRestored"] == {
        "windowMs": 300000,
        "viewEnd": 1700000000000,
        "live": False,
        "laneScrollY": 42,
        "collapsed": ["owner/repo"],
        "highlightedSpanId": "span-7",
    }

    # The provider snapshot carries the sub-tab AND the Timeline slice, and keeps
    # answering after the mount is torn down.
    assert result["saved"]["tab"] == "navigator"
    assert result["saved"]["timeline"]["windowMs"] == 300000
    assert result["savedAfterDestroy"] == result["saved"]

    # Reload: a brand-new module instance has nothing until the framework
    # restores, and then lands on the persisted sub-tab with no hash to help.
    assert result["noStateYet"] is True
    assert result["restoredTab"] == "navigator"
    assert result["restoredHash"] == ""
    assert result["timelineMountCountAfterRestore"] == 2  # no throw-away mount
    assert result["timelineStateAfterRestore"] == result["saved"]["timeline"]

    # Nothing reached raw localStorage; the only storage read is the curation
    # debug flag, and it goes through ui_state.mjs.
    assert result["rawStorageCalls"] == []
    assert set(result["uiStateReads"]) <= {"kestrel.observability.curated"}


def test_observability_has_no_bespoke_localstorage_path():
    """The retired `kestrel.observability.subtab` key and raw access are gone."""
    source = (STATIC / "observability.js").read_text(encoding="utf-8")
    assert "localStorage.getItem" not in source
    assert "localStorage.setItem" not in source
    assert "kestrel.observability.subtab" not in source
    assert 'import { storeGet } from "/js/ui_state.mjs";' in source
    assert "viewState:" in source
