/**
 * Kestrel Sovereign Console — Fleet Swimlane (Timeline) Panel (feature-owned)
 *
 * Ported from kestrel-claws `dashboard/src/views/timeline.ts` and extended for
 * the observability feature. Renders observability events as nested
 * "russian-doll" blocks (session → task → tool call → event), one lane per
 * agent on the Y axis and time on the X axis, and adds:
 *
 *   - a left agent selector (tree) populated from `GET /agent-tree`;
 *   - nested **sublanes**: a child agent's lane indents under its driver using
 *     the lineage fields (`parent_agent`/`driven_by`/`parent_session_id`/
 *     `subagent_id`) folded into event metadata by the data plane;
 *   - live updates via SSE when the host exposes an event stream, else polling
 *     of the subtree events endpoint.
 *
 * Consumes the feature's own query router:
 *   GET /api/observability/agent-tree           (spawn hierarchy)
 *   GET /api/observability/events?subtree=true   (per-subtree event stream)
 *
 * IMPORTANT — DOM-free import: this module performs NO DOM access and imports NO
 * host modules at import time. `buildLanes` / `nestLanes` and their helpers are
 * pure and exported for `node --test`. The panel self-registration only runs in
 * a browser (guarded on `window`/`document`) and dynamically imports the host
 * `ui-ext` modules there, so importing this file under node stays side-effect
 * free.
 */

// ── Layout constants ──────────────────────────────────────────

const ROW_H = 56; // height of a single session row within a lane
const LANE_PAD = 6; // vertical padding between session rows
const SUBLANE_INDENT = 16; // px of indent per nesting level in the lane labels

const RANGE_MS = {
  "1m": 60_000,
  "5m": 300_000,
  all: Infinity,
};

// px-per-second scale per range (governs how wide the scroll content is)
const RANGE_SCALE = {
  "1m": 18,
  "5m": 5,
  all: 0, // computed dynamically
};

const PALETTE = [
  "#818cf8", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4",
  "#ec4899", "#a3e635", "#f97316", "#14b8a6", "#c084fc",
];

const TOOL_PAIR_WINDOW = 10_000; // pair pre/post within 10s

// ── Grouping model (documented; JS carries no types) ──────────
//
// ToolCallBlock: { toolName, start, end, success, events }
// TaskBlock:     { start, end, call, children }
// SessionBlock:  { sessionId, agentName, start, end, status, tasks,
//                  toolCalls, looseEvents }
// Lane:          { agentName, sessions, depth?, parentAgent? }

function ts(e) {
  const t = new Date(e.timestamp).getTime();
  return Number.isFinite(t) ? t : 0;
}

/**
 * Flat events → lanes (per agent) → sessions → tool calls → tasks.
 * Framework-agnostic and DOM-free; exported for tests.
 */
export function buildLanes(events) {
  const byAgent = new Map();
  for (const e of events) {
    const agent = e.agent_name || "unknown";
    if (!byAgent.has(agent)) byAgent.set(agent, []);
    byAgent.get(agent).push(e);
  }

  const lanes = [];
  for (const agentName of [...byAgent.keys()].sort()) {
    const agentEvents = byAgent.get(agentName);
    const bySession = new Map();
    for (const e of agentEvents) {
      const sid = e.session_id || "unknown";
      if (!bySession.has(sid)) bySession.set(sid, []);
      bySession.get(sid).push(e);
    }

    const sessions = [];
    for (const [sessionId, sessEvents] of bySession) {
      const sorted = [...sessEvents].sort((a, b) => ts(a) - ts(b));
      const { toolCalls, looseEvents } = pairToolCalls(sorted);

      // Split Task tool-calls from the rest, then nest others under them.
      const taskCalls = toolCalls.filter((tc) => tc.toolName === "Task");
      const others = toolCalls.filter((tc) => tc.toolName !== "Task");

      const claimed = new Set();
      const tasks = taskCalls.map((call) => {
        const children = others.filter(
          (tc) => tc.start >= call.start && tc.end <= call.end
        );
        children.forEach((c) => claimed.add(c));
        return { start: call.start, end: call.end, call, children };
      });

      const directToolCalls = others.filter((tc) => !claimed.has(tc));

      const allTimes = sorted.map(ts);
      const start = Math.min(...allTimes);
      const end = Math.max(...allTimes);

      sessions.push({
        sessionId,
        agentName,
        start,
        end,
        status: sessionStatus(sorted, toolCalls),
        tasks,
        toolCalls: directToolCalls,
        looseEvents,
      });
    }

    sessions.sort((a, b) => a.start - b.start);
    lanes.push({ agentName, sessions, depth: 0, parentAgent: null });
  }

  return lanes;
}

/** Pair tool_call/tool_response (and errors) with the same tool_name in a window. */
function pairToolCalls(sorted) {
  const toolCalls = [];
  const looseEvents = [];
  const open = [];

  for (const e of sorted) {
    if (e.event_type === "tool_call") {
      open.push(e);
    } else if (e.event_type === "tool_response" || e.event_type === "error") {
      const idx = open.findIndex(
        (c) =>
          c.tool_name === e.tool_name &&
          ts(e) - ts(c) >= 0 &&
          ts(e) - ts(c) <= TOOL_PAIR_WINDOW
      );
      if (idx >= 0) {
        const call = open.splice(idx, 1)[0];
        toolCalls.push({
          toolName: call.tool_name ?? e.tool_name ?? "tool",
          start: ts(call),
          end: ts(e),
          success: e.event_type === "error" ? false : e.success ?? null,
          events: [call, e],
        });
      } else {
        // Orphan response/error — a zero-width block of its own.
        toolCalls.push({
          toolName: e.tool_name ?? (e.event_type === "error" ? "error" : "tool"),
          start: ts(e),
          end: ts(e),
          success: e.event_type === "error" ? false : e.success ?? null,
          events: [e],
        });
      }
    } else {
      looseEvents.push(e);
    }
  }

  // Unfinished calls — still running.
  for (const c of open) {
    toolCalls.push({
      toolName: c.tool_name ?? "tool",
      start: ts(c),
      end: ts(c),
      success: null,
      events: [c],
    });
  }

  toolCalls.sort((a, b) => a.start - b.start);
  return { toolCalls, looseEvents };
}

function sessionStatus(events, toolCalls) {
  if (events.some((e) => e.event_type === "error" || e.success === false)) {
    return "failed";
  }
  // A tool call with only one event (no matching response) is still open.
  if (toolCalls.some((tc) => tc.events.length === 1 && tc.events[0].event_type === "tool_call")) {
    return "running";
  }
  return "completed";
}

// ── Sublane nesting ───────────────────────────────────────────

/**
 * Walk a `GET /agent-tree` node (or list of nodes) into a child→parent map of
 * agent names. Both `agent_name` and `name` node keys are accepted.
 * DOM-free; exported for tests.
 */
export function buildParentMap(tree) {
  const parent = new Map();
  function walk(node, parentName) {
    if (!node || typeof node !== "object") return;
    const name = node.agent_name || node.name;
    if (name && parentName) parent.set(name, parentName);
    for (const child of node.children || []) walk(child, name || parentName);
  }
  if (Array.isArray(tree)) tree.forEach((t) => walk(t, null));
  else walk(tree, null);
  return parent;
}

/**
 * Derive child→parent from event lineage metadata folded in by the data plane
 * (`parent_agent`/`driven_by`/`driver`). First writer wins per child.
 * DOM-free; exported for tests.
 */
export function lineageParents(events) {
  const parent = new Map();
  for (const e of events || []) {
    const child = e.agent_name;
    const md = e.metadata || {};
    const p = md.parent_agent || md.driven_by || md.driver;
    if (child && p && child !== p && !parent.has(child)) parent.set(child, p);
  }
  return parent;
}

/**
 * Order + indent lanes so each child agent nests under its driver.
 *
 * Parentage is taken from the agent tree first, then from event lineage for any
 * child the tree didn't cover. A child whose parent has no lane of its own
 * re-attaches to the nearest ancestor that does (or becomes a root). Each
 * returned lane carries `depth` (0 = root) and `parentAgent`. Deterministic:
 * roots and siblings are emitted in name order. Cycle-safe.
 *
 * DOM-free; exported for tests.
 */
export function nestLanes(lanes, tree, events = []) {
  const laneByName = new Map(lanes.map((l) => [l.agentName, l]));

  const parent = buildParentMap(tree);
  for (const [child, p] of lineageParents(events)) {
    if (!parent.has(child)) parent.set(child, p);
  }

  // Nearest ancestor that actually owns a lane (skips lane-less intermediates).
  function effectiveParent(name) {
    const seen = new Set([name]);
    let p = parent.get(name);
    while (p && !laneByName.has(p) && !seen.has(p)) {
      seen.add(p);
      p = parent.get(p);
    }
    return p && laneByName.has(p) && p !== name ? p : null;
  }

  const childrenOf = new Map();
  const roots = [];
  for (const lane of lanes) {
    const p = effectiveParent(lane.agentName);
    if (p) {
      if (!childrenOf.has(p)) childrenOf.set(p, []);
      childrenOf.get(p).push(lane.agentName);
    } else {
      roots.push(lane.agentName);
    }
  }
  roots.sort();

  const out = [];
  const visited = new Set();
  function emit(name, depth) {
    if (visited.has(name)) return;
    visited.add(name);
    const lane = laneByName.get(name);
    out.push({ ...lane, depth, parentAgent: parent.get(name) || null });
    for (const kid of (childrenOf.get(name) || []).slice().sort()) {
      emit(kid, depth + 1);
    }
  }
  for (const r of roots) emit(r, 0);
  // Anything left unvisited (a cycle) is appended flat so nothing is dropped.
  for (const lane of lanes) {
    if (!visited.has(lane.agentName)) out.push({ ...lane, depth: 0, parentAgent: null });
  }
  return out;
}

/**
 * Collect an agent + all its descendants (by name) from a `GET /agent-tree`.
 * Used to scope the swimlane to a selected agent's subtree. DOM-free; exported.
 */
export function subtreeAgentNames(tree, agentName) {
  const names = new Set();
  function findAndCollect(node) {
    if (!node || typeof node !== "object") return false;
    const name = node.agent_name || node.name;
    if (name === agentName) {
      collect(node);
      return true;
    }
    for (const child of node.children || []) {
      if (findAndCollect(child)) return true;
    }
    return false;
  }
  function collect(node) {
    const name = node.agent_name || node.name;
    if (name) names.add(name);
    for (const child of node.children || []) collect(child);
  }
  if (Array.isArray(tree)) tree.forEach(findAndCollect);
  else findAndCollect(tree);
  return names;
}

// ── Pure presentation helpers (DOM-free) ──────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function hashColor(key) {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}

function hookType(e) {
  const v = e.metadata && e.metadata["hook_event_type"];
  return typeof v === "string" ? v : "—";
}

function shortId(id) {
  const s = String(id ?? "");
  return s.length > 12 ? s.slice(0, 12) + "…" : s;
}

// ============================================================================
// Browser-only panel registration + rendering.
//
// Everything below runs only in a browser. It is wrapped in `mountTimeline`
// and only invoked from the `window`/`document` guard at the very bottom, so
// importing this module under node stays DOM-free and host-import-free.
// ============================================================================

export function mountTimeline({ API, registerPanel, bus }) {
  const PANEL_ID = "timeline";
  const CAPABILITY = "observability";

  const state = {
    events: [],
    tree: null,
    selectedAgent: null, // null → whole fleet
    playing: true,
    range: "5m",
    colorMode: "status",
    live: false,
  };

  let bodyEl = null;
  let source = null;
  let pollTimer = null;
  let rafId = null;
  let destroyed = false;

  // ── Time / geometry ─────────────────────────────────────────

  function timeBounds(events) {
    const now = Date.now();
    if (state.range === "all") {
      if (!events.length) return { min: now - 60_000, max: now };
      const times = events.map(ts);
      return { min: Math.min(...times), max: Math.max(Math.max(...times), now) };
    }
    return { min: now - RANGE_MS[state.range], max: now };
  }

  function scaleFor(min, max) {
    if (state.range !== "all") return RANGE_SCALE[state.range];
    const durSec = Math.max(1, (max - min) / 1000);
    return Math.min(30, Math.max(2, 1400 / durSec));
  }

  function scopedEvents() {
    let events = state.events;
    if (state.selectedAgent && state.tree) {
      const names = subtreeAgentNames(state.tree, state.selectedAgent);
      if (names.size) events = events.filter((e) => names.has(e.agent_name));
    }
    if (state.range === "all") return events;
    const cutoff = Date.now() - RANGE_MS[state.range];
    return events.filter((e) => ts(e) >= cutoff);
  }

  // ── Colors ──────────────────────────────────────────────────

  function sessionColor(s) {
    switch (state.colorMode) {
      case "status":
        return s.status === "failed"
          ? "var(--error)"
          : s.status === "running"
          ? "var(--warning)"
          : "var(--success)";
      default:
        return "var(--border-color)";
    }
  }

  function toolColor(tc) {
    switch (state.colorMode) {
      case "tool":
        return hashColor(tc.toolName);
      case "hook": {
        const ht = tc.events.map(hookType).find((h) => h !== "—") ?? "—";
        return ht === "—" ? "var(--accent)" : hashColor(ht);
      }
      default:
        return tc.success === false
          ? "var(--error)"
          : tc.success === true
          ? "var(--success)"
          : "var(--accent)";
    }
  }

  // ── Agent selector (left tree) ──────────────────────────────

  function renderTreeNode(node, depth) {
    const name = node.agent_name || node.name || "unknown";
    const kids = node.children || [];
    const active = state.selectedAgent === name ? " tl-tree__item--active" : "";
    let html = `
      <div class="tl-tree__item${active}" data-agent="${escapeHtml(name)}"
           style="padding-left:${8 + depth * SUBLANE_INDENT}px" title="${escapeHtml(name)}">
        <span class="tl-tree__dot"></span>${escapeHtml(name)}
      </div>`;
    for (const child of kids) html += renderTreeNode(child, depth + 1);
    return html;
  }

  function renderSidebar() {
    const allActive = state.selectedAgent == null ? " tl-tree__item--active" : "";
    const nodes = state.tree
      ? (Array.isArray(state.tree) ? state.tree : [state.tree])
      : [];
    const treeHtml = nodes.length
      ? nodes.map((n) => renderTreeNode(n, 0)).join("")
      : `<div class="tl-tree__empty">No agents yet.</div>`;
    return `
      <div class="tl-sidebar">
        <div class="tl-sidebar__title">Agents</div>
        <div class="tl-tree__item${allActive}" data-agent="__all__"
             style="padding-left:8px"><span class="tl-tree__dot"></span>All agents</div>
        ${treeHtml}
      </div>`;
  }

  // ── Render (right swimlane) ─────────────────────────────────

  function renderLegend(lanes) {
    let items = [];
    if (state.colorMode === "status") {
      items = [
        { label: "running", color: "var(--warning)" },
        { label: "completed", color: "var(--success)" },
        { label: "failed", color: "var(--error)" },
      ];
    } else if (state.colorMode === "tool") {
      const tools = new Set();
      for (const l of lanes)
        for (const s of l.sessions) {
          s.toolCalls.forEach((tc) => tools.add(tc.toolName));
          s.tasks.forEach((t) => t.children.forEach((tc) => tools.add(tc.toolName)));
        }
      items = [...tools].slice(0, 12).map((t) => ({ label: t, color: hashColor(t) }));
    } else {
      const hooks = new Set();
      for (const e of state.events) {
        const h = hookType(e);
        if (h !== "—") hooks.add(h);
      }
      items = [...hooks].slice(0, 12).map((h) => ({ label: h, color: hashColor(h) }));
      if (!items.length) items = [{ label: "no hook data", color: "var(--accent)" }];
    }

    return `
      <div class="tl-legend">
        ${items
          .map(
            (i) => `
          <span class="tl-legend__item">
            <span class="tl-legend__swatch" style="background:${i.color}"></span>
            ${escapeHtml(i.label)}
          </span>`
          )
          .join("")}
      </div>`;
  }

  function renderControls() {
    const rangeBtn = (r, label) =>
      `<button class="tl-btn ${state.range === r ? "tl-btn--active" : ""}" data-range="${r}">${label}</button>`;
    const colorBtn = (c, label) =>
      `<button class="tl-btn ${state.colorMode === c ? "tl-btn--active" : ""}" data-color="${c}">${label}</button>`;

    return `
      <div class="tl-controls">
        <button class="tl-btn tl-btn--play" data-action="toggle-play">
          ${state.playing ? "&#10074;&#10074; Pause" : "&#9654; Play"}
        </button>
        <span class="tl-controls__group">
          <span class="tl-controls__label">Range</span>
          ${rangeBtn("1m", "1m")}${rangeBtn("5m", "5m")}${rangeBtn("all", "All")}
        </span>
        <span class="tl-controls__group">
          <span class="tl-controls__label">Color</span>
          ${colorBtn("status", "Status")}${colorBtn("tool", "Tool")}${colorBtn("hook", "Hook")}
        </span>
      </div>`;
  }

  function renderToolCall(tc, originStart, scale, level) {
    const left = ((tc.start - originStart) / 1000) * scale;
    const width = Math.max(6, ((tc.end - tc.start) / 1000) * scale);
    const dots = tc.events
      .map((e) => {
        const dl = ((ts(e) - tc.start) / 1000) * scale;
        return `<span class="tl-dot tl-dot--${escapeHtml(e.event_type)}" data-event="1"
                 data-session="${escapeHtml(e.session_id)}" data-agent="${escapeHtml(e.agent_name)}"
                 style="left:${dl}px" title="${escapeHtml(e.event_type)}${e.tool_name ? " · " + escapeHtml(e.tool_name) : ""}"></span>`;
      })
      .join("");
    return `
      <div class="tl-block tl-toolcall tl-toolcall--l${level}"
           style="left:${left}px;width:${width}px;border-color:${toolColor(tc)}"
           data-session="${escapeHtml(tc.events[0].session_id)}"
           data-agent="${escapeHtml(tc.events[0].agent_name)}"
           title="${escapeHtml(tc.toolName)}${tc.success === false ? " (failed)" : ""}">
        <span class="tl-block__label">${escapeHtml(tc.toolName)}</span>
        ${dots}
      </div>`;
  }

  function renderSession(s, rowIndex, min, scale) {
    const left = ((s.start - min) / 1000) * scale;
    const width = Math.max(24, ((s.end - s.start) / 1000) * scale);
    const top = rowIndex * (ROW_H + LANE_PAD);

    const tasksHtml = s.tasks
      .map((t) => {
        const tLeft = ((t.start - s.start) / 1000) * scale;
        const tWidth = Math.max(16, ((t.end - t.start) / 1000) * scale);
        const children = t.children
          .map((tc) => renderToolCall(tc, t.start, scale, 2))
          .join("");
        return `
          <div class="tl-block tl-task"
               style="left:${tLeft}px;width:${tWidth}px"
               data-session="${escapeHtml(s.sessionId)}" data-agent="${escapeHtml(s.agentName)}"
               title="Task (${t.children.length} tool calls)">
            <span class="tl-block__label">Task</span>
            ${children}
          </div>`;
      })
      .join("");

    const directHtml = s.toolCalls.map((tc) => renderToolCall(tc, s.start, scale, 1)).join("");

    const looseHtml = s.looseEvents
      .map((e) => {
        const dl = ((ts(e) - s.start) / 1000) * scale;
        return `<span class="tl-dot tl-dot--${escapeHtml(e.event_type)}" data-event="1"
                 data-session="${escapeHtml(e.session_id)}" data-agent="${escapeHtml(e.agent_name)}"
                 style="left:${dl}px;bottom:4px" title="${escapeHtml(e.event_type)}"></span>`;
      })
      .join("");

    return `
      <div class="tl-block tl-session tl-session--${s.status}"
           style="left:${left}px;width:${width}px;top:${top}px;height:${ROW_H}px;border-color:${sessionColor(s)}"
           data-session="${escapeHtml(s.sessionId)}" data-agent="${escapeHtml(s.agentName)}"
           title="Session ${escapeHtml(s.sessionId)} · ${s.status}">
        <span class="tl-block__label tl-session__label">${escapeHtml(shortId(s.sessionId))} · ${s.status}</span>
        ${tasksHtml}
        ${directHtml}
        ${looseHtml}
      </div>`;
  }

  function render() {
    if (destroyed || !bodyEl) return;
    const events = scopedEvents();
    const lanes = nestLanes(buildLanes(events), state.tree, events);
    const { min, max } = timeBounds(events);
    const scale = scaleFor(min, max);
    const contentWidth = Math.max(600, ((max - min) / 1000) * scale + 40);

    const laneRows = lanes.map((l) => ({ lane: l, rows: Math.max(1, l.sessions.length) }));
    const totalHeight = laneRows.reduce(
      (sum, lr) => sum + lr.rows * (ROW_H + LANE_PAD) + LANE_PAD,
      0
    );

    let laneLabels = "";
    let laneTracks = "";
    for (const { lane, rows } of laneRows) {
      const h = rows * (ROW_H + LANE_PAD) + LANE_PAD;
      const indent = (lane.depth || 0) * SUBLANE_INDENT;
      laneLabels += `
        <div class="tl-lane-label" style="height:${h}px;padding-left:${8 + indent}px"
             title="${escapeHtml(lane.agentName)}${lane.parentAgent ? " · under " + escapeHtml(lane.parentAgent) : ""}">
          <span>${lane.depth ? "&#8627; " : ""}${escapeHtml(lane.agentName)}</span>
        </div>`;
      const blocks = lane.sessions.map((s, i) => renderSession(s, i, min, scale)).join("");
      laneTracks += `<div class="tl-lane" style="height:${h}px;width:${contentWidth}px">${blocks}</div>`;
    }

    const body = !events.length
      ? `<div class="tl-empty">No observability events for this ${
          state.selectedAgent ? "agent subtree" : "range"
        }. New events will appear here live.</div>`
      : `
        <div class="tl-grid">
          <div class="tl-lane-labels">${laneLabels}</div>
          <div class="tl-scroll" id="tl-scroll">
            <div class="tl-content" style="width:${contentWidth}px;height:${totalHeight}px">
              ${laneTracks}
            </div>
          </div>
        </div>`;

    bodyEl.innerHTML = `
      ${STYLE}
      <div class="tl-root">
        ${renderSidebar()}
        <div class="tl-main">
          <div class="tl-header">
            <h2 class="m-0">&#128337; Fleet Timeline</h2>
            <span class="tl-subtitle">${
              state.selectedAgent ? escapeHtml(state.selectedAgent) + " subtree" : "All agents"
            } · ${lanes.length} lane${lanes.length === 1 ? "" : "s"} · ${events.length} events</span>
            <span class="tl-live ${state.live ? "tl-live--on" : "tl-live--off"}"
                  title="${state.live ? "Live via SSE" : "Polling"}">
              ${state.live ? "&#9679; LIVE" : "&#9679; POLL"}
            </span>
          </div>
          ${renderControls()}
          ${renderLegend(lanes)}
          ${body}
        </div>
      </div>`;

    wireEvents();
    applyAutoScroll();
  }

  // ── Interaction ─────────────────────────────────────────────

  function wireEvents() {
    bodyEl.querySelector('[data-action="toggle-play"]')?.addEventListener("click", () => {
      state.playing = !state.playing;
      render();
    });
    bodyEl.querySelectorAll("[data-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.range = btn.dataset.range;
        render();
      });
    });
    bodyEl.querySelectorAll("[data-color]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.colorMode = btn.dataset.color;
        render();
      });
    });
    bodyEl.querySelectorAll(".tl-tree__item").forEach((el) => {
      el.addEventListener("click", () => {
        const agent = el.dataset.agent;
        state.selectedAgent = agent === "__all__" ? null : agent;
        render();
      });
    });
  }

  const scrollEl = () => bodyEl && bodyEl.querySelector("#tl-scroll");

  function applyAutoScroll() {
    const el = scrollEl();
    if (el && state.playing) el.scrollLeft = el.scrollWidth - el.clientWidth;
  }

  function tick() {
    if (destroyed) return;
    if (state.playing) {
      const el = scrollEl();
      if (el) {
        const target = el.scrollWidth - el.clientWidth;
        el.scrollLeft += (target - el.scrollLeft) * 0.15;
      }
    }
    rafId = requestAnimationFrame(tick);
  }

  // ── Data ────────────────────────────────────────────────────

  function mergeEvent(e) {
    if (!e || !e.agent_name) return;
    const id = e.event_id;
    if (id && state.events.some((x) => x.event_id === id)) return;
    state.events.push(e);
    if (state.events.length > 2000) state.events.splice(0, state.events.length - 2000);
  }

  async function refreshTree() {
    try {
      state.tree = await API.request("/api/observability/agent-tree");
    } catch (e) {
      // A missing tree just means a flat (single-lane) swimlane.
      state.tree = state.tree || null;
    }
  }

  async function refreshEvents() {
    try {
      const res = await API.request(
        "/api/observability/events?subtree=true&limit=500"
      );
      state.events = (res && res.events) || [];
    } catch (e) {
      // Keep whatever we have; render shows the empty state if nothing.
    }
  }

  async function refresh() {
    if (!API.hasCapability(CAPABILITY)) return;
    await Promise.all([refreshTree(), refreshEvents()]);
    render();
  }

  function connectLive() {
    if (typeof EventSource === "undefined") return;
    try {
      source = new EventSource("/api/observability/stream");
      source.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          if (data && data.agent_name && data.event_type) {
            mergeEvent(data);
            render();
          } else {
            refreshEvents().then(render);
          }
        } catch {
          refreshEvents().then(render);
        }
      };
      source.onopen = () => {
        state.live = true;
        render();
      };
      source.onerror = () => {
        try {
          source && source.close();
        } catch {
          /* noop */
        }
        source = null;
        state.live = false;
      };
    } catch {
      source = null;
      state.live = false;
    }
  }

  // ── Panel registration ──────────────────────────────────────

  function renderPanel(el) {
    bodyEl = el;
    if (!bodyEl) return;
    bodyEl.innerHTML = `${STYLE}<div class="tl-root"><div class="tl-empty">Loading live timeline…</div></div>`;
    refresh();
    connectLive();
    if (!pollTimer) pollTimer = setInterval(() => refreshEvents().then(render), 15_000);
    if (rafId == null) rafId = requestAnimationFrame(tick);
  }

  registerPanel({
    panelId: PANEL_ID,
    label: "Fleet Timeline",
    gate: () => API.hasCapability(CAPABILITY),
    render: renderPanel,
  });

  bus.on("panel:shown", (payload) => {
    if (payload && payload.panelId === PANEL_ID) refresh();
  });

  return {
    refresh,
    destroy() {
      destroyed = true;
      if (pollTimer) clearInterval(pollTimer);
      if (rafId != null) cancelAnimationFrame(rafId);
      try {
        source && source.close();
      } catch {
        /* noop */
      }
      source = null;
    },
  };
}

// Scoped styles injected with the panel body — theme variables only so the
// embedding host can remap them, and the swimlane scrolls internally.
const STYLE = `
<style>
  .tl-root { display:flex; gap:12px; height:100%; min-height:420px; }
  .tl-sidebar { width:200px; flex:0 0 200px; border:1px solid var(--border-color);
    border-radius:8px; background:var(--bg-secondary); overflow-y:auto; padding:8px 0; }
  .tl-sidebar__title { font-size:0.7rem; text-transform:uppercase; letter-spacing:0.05em;
    color:var(--text-tertiary); padding:0 8px 6px; }
  .tl-tree__item { display:flex; align-items:center; gap:6px; padding:5px 8px; cursor:pointer;
    font-size:0.85rem; color:var(--text-primary); white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; }
  .tl-tree__item:hover { background:var(--bg-tertiary); }
  .tl-tree__item--active { background:var(--bg-tertiary); font-weight:600; }
  .tl-tree__dot { width:6px; height:6px; border-radius:50%; background:var(--accent); flex:0 0 auto; }
  .tl-tree__empty { padding:8px; font-size:0.8rem; color:var(--text-tertiary); }
  .tl-main { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .tl-header { display:flex; align-items:center; gap:12px; margin-bottom:8px; }
  .tl-subtitle { font-size:0.8rem; color:var(--text-secondary); }
  .tl-live { font-size:0.7rem; font-weight:600; margin-left:auto; }
  .tl-live--on { color:var(--success); }
  .tl-live--off { color:var(--text-tertiary); }
  .tl-controls, .tl-legend { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
    margin-bottom:8px; }
  .tl-controls__group { display:flex; align-items:center; gap:4px; }
  .tl-controls__label { font-size:0.75rem; color:var(--text-tertiary); }
  .tl-btn { padding:3px 8px; font-size:0.78rem; background:var(--bg-tertiary);
    color:var(--text-primary); border:1px solid var(--border-color); border-radius:4px;
    cursor:pointer; }
  .tl-btn--active { border-color:var(--accent); color:var(--accent); }
  .tl-legend__item { display:flex; align-items:center; gap:4px; font-size:0.75rem;
    color:var(--text-secondary); }
  .tl-legend__swatch { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .tl-empty { padding:2rem; text-align:center; color:var(--text-secondary); font-size:0.9rem; }
  .tl-grid { display:flex; flex:1 1 auto; min-height:0; border:1px solid var(--border-color);
    border-radius:8px; overflow:hidden; background:var(--bg-secondary); }
  .tl-lane-labels { flex:0 0 auto; width:150px; border-right:1px solid var(--border-color);
    background:var(--bg-tertiary); }
  .tl-lane-label { display:flex; align-items:center; font-size:0.8rem; color:var(--text-primary);
    border-bottom:1px solid var(--border-color); overflow:hidden; white-space:nowrap;
    text-overflow:ellipsis; }
  .tl-scroll { flex:1 1 auto; overflow-x:auto; overflow-y:auto; position:relative; }
  .tl-content { position:relative; }
  .tl-lane { position:relative; border-bottom:1px solid var(--border-color); }
  .tl-block { position:absolute; box-sizing:border-box; border:1px solid var(--border-color);
    border-radius:4px; overflow:hidden; cursor:default; }
  .tl-session { background:var(--bg-tertiary); }
  .tl-block__label { font-size:0.68rem; color:var(--text-secondary); padding:1px 3px;
    display:inline-block; pointer-events:none; }
  .tl-task { top:20px; height:16px; background:rgba(129,140,248,0.12); }
  .tl-toolcall { top:20px; height:14px; background:var(--bg-secondary); }
  .tl-toolcall--l2 { top:2px; height:12px; }
  .tl-dot { position:absolute; top:2px; width:5px; height:5px; border-radius:50%;
    background:var(--accent); }
  .tl-dot--error { background:var(--error); }
</style>`;

// ── Browser-only self-registration ────────────────────────────
//
// Runs only in a real Console (window + document present). Dynamically imports
// the host `ui-ext` modules so that node's test runner can import the pure
// exports above without those paths resolving.
if (typeof window !== "undefined" && typeof document !== "undefined") {
  Promise.all([
    import("/js/api.js"),
    import("/js/ui-ext/panels.js"),
    import("/js/ui-ext/bus.js"),
  ])
    .then(([apiMod, panelsMod, busMod]) => {
      mountTimeline({
        API: apiMod.default,
        registerPanel: panelsMod.registerPanel,
        bus: busMod.default,
      });
    })
    .catch((e) => {
      // Non-fatal: a host without the ui-ext registry simply won't show the tab.
      console.error("Timeline panel failed to register:", e);
    });
}
