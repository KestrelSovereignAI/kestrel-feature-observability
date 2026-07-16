// Fleet observability — orchestrator swimlane panel.
//
// Host-scoped console module. Renders a left orchestrator tree (GET /tree) and a
// right lanes/sublanes timeline (session → task → tool-call → event, one lane
// per agent, sublanes nested under their orchestrator), live over the streamable
// stream (GET /stream — a fetch-based text/event-stream with Last-Event-ID
// resumability, reconciled from GET /events on (re)connect).
//
// A sub-view of the unified "Observability" console panel (observability.js);
// the container imports and mounts/unmounts this view — it does not
// self-register a top-level panel. `mount(container)` fills the supplied content
// element and returns a handle whose `destroy()` unmounts it.
//
// All pure grouping logic lives in swimlane.lanes.js (DOM-free, unit-tested).

import API from "/js/api.js";
import { buildLanes, nestLanes, ts } from "./swimlane.lanes.js";

const API_PREFIX = "/api/host/observability";

// ── Layout constants ──────────────────────────────────────────

const ROW_H = 56; // height of a single session row within a lane
const LANE_PAD = 6; // vertical padding between session rows

const RANGE_MS = { "1m": 60_000, "5m": 300_000, all: Infinity };
const RANGE_SCALE = { "1m": 18, "5m": 5, all: 0 }; // px-per-second (all computed)

const PALETTE = [
  "#818cf8", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4",
  "#ec4899", "#a3e635", "#f97316", "#14b8a6", "#c084fc",
];

// ── Utilities ─────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function hashColor(key) {
  let h = 0;
  const str = String(key);
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}

function hookType(e) {
  const v = e && e.metadata && e.metadata["hook_event_type"];
  return typeof v === "string" ? v : "—";
}

function shortId(id) {
  const s = String(id ?? "");
  return s.length > 12 ? s.slice(0, 12) + "…" : s;
}

// ── Panel ─────────────────────────────────────────────────────

export const panel = {
  id: "observability-fleet-swimlane",
  title: "Fleet Swimlane",
  capability: "observability-fleet",

  async fetchTree() {
    // Authenticated host-root GET (attaches X-API-Key); returns parsed JSON.
    return API.requestHost(`${API_PREFIX}/tree`);
  },

  async fetchEvents({ orchestrator, subtree } = {}) {
    const params = new URLSearchParams({ limit: "1000" });
    if (orchestrator != null) params.set("orchestrator", orchestrator);
    if (subtree) params.set("subtree", "true");
    return API.requestHost(`${API_PREFIX}/events?${params}`);
  },

  // Fetch-based live stream. `onEvent(payload, id)` is called per event; resumes
  // from the last stream id after a reconnect via the Last-Event-ID header.
  async stream(onEvent, { signal, lastEventId } = {}) {
    // Raw fetch (needs the streaming Response body), but authenticated with the
    // console's cached X-API-Key — the cookie alone is not accepted by the host.
    const headers = {};
    const apiKey = API.getApiKey();
    if (apiKey) headers["X-API-Key"] = apiKey;
    if (lastEventId) headers["Last-Event-ID"] = String(lastEventId);
    const res = await fetch(`${API_PREFIX}/stream`, {
      headers,
      signal,
    });
    if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const lines = frame.split("\n");
        const idLine = lines.find((l) => l.startsWith("id: "));
        const dataLine = lines.find((l) => l.startsWith("data: "));
        if (dataLine) {
          const id = idLine ? Number(idLine.slice(4)) : undefined;
          try {
            onEvent(JSON.parse(dataLine.slice(6)), id);
          } catch {
            /* ignore malformed frame */
          }
        }
      }
    }
  },

  mount,
};

export default panel;

// ── View / mount ──────────────────────────────────────────────

export function mount(container) {
  const state = {
    events: [],
    tree: [],
    selectedOrchestrator: undefined, // undefined = all; null = Direct; string = named
    playing: true,
    range: "5m",
    colorMode: "status",
    live: false,
    lastEventId: undefined,
  };

  let abort = null;
  let pollTimer = null;
  let rafId = null;
  let destroyed = false;
  let popoverEl = null;

  // ── Time / geometry ─────────────────────────────────────────

  function nowMs() {
    return new Date().getTime();
  }

  function timeBounds(events) {
    const now = nowMs();
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

  // Events after the selected-orchestrator scope + time-range filters.
  function scopedEvents() {
    let events = state.events;
    if (state.selectedOrchestrator !== undefined) {
      const sel = state.selectedOrchestrator;
      events = events.filter((e) => {
        const orch = e.orchestrator != null ? e.orchestrator : null;
        // Scope to the subtree: events driven by the orchestrator, plus events
        // the orchestrator itself emitted (agent_name === orchestrator).
        return orch === sel || (sel !== null && e.agent_name === sel);
      });
    }
    if (state.range === "all") return events;
    const cutoff = nowMs() - RANGE_MS[state.range];
    return events.filter((e) => ts(e) >= cutoff);
  }

  // ── Colors ──────────────────────────────────────────────────

  function sessionColor(s) {
    if (state.colorMode === "status") {
      return s.status === "failed"
        ? "var(--color-danger, #ef4444)"
        : s.status === "running"
        ? "var(--color-warning, #f59e0b)"
        : "var(--color-success, #22c55e)";
    }
    return "var(--color-border, #334155)";
  }

  function toolColor(tc) {
    switch (state.colorMode) {
      case "tool":
        return hashColor(tc.toolName);
      case "hook": {
        const ht = tc.events.map(hookType).find((h) => h !== "—") ?? "—";
        return ht === "—" ? "var(--color-accent, #818cf8)" : hashColor(ht);
      }
      default:
        return tc.success === false
          ? "var(--color-danger, #ef4444)"
          : tc.success === true
          ? "var(--color-success, #22c55e)"
          : "var(--color-accent, #818cf8)";
    }
  }

  // ── Render: left tree ───────────────────────────────────────

  function renderTree() {
    const allActive = state.selectedOrchestrator === undefined ? "sw-tree__node--active" : "";
    const nodes = state.tree
      .map((node) => {
        const key = node.is_direct ? "__direct__" : node.orchestrator;
        const selected =
          state.selectedOrchestrator !== undefined &&
          (node.is_direct
            ? state.selectedOrchestrator === null
            : state.selectedOrchestrator === node.orchestrator);
        const agents = (node.agents || [])
          .map(
            (a) => `
            <div class="sw-tree__agent" title="${escapeHtml(a.agent_name)}">
              <span class="sw-tree__agent-name">${escapeHtml(a.label || a.agent_name)}</span>
              <span class="sw-tree__count">${a.event_count}</span>
            </div>`
          )
          .join("");
        // is_direct nodes carry a null orchestrator; encode it so the click
        // handler can distinguish "Direct" (null) from a named orchestrator.
        return `
          <div class="sw-tree__group">
            <button class="sw-tree__node ${selected ? "sw-tree__node--active" : ""}"
                    data-orch-key="${escapeHtml(key)}" data-direct="${node.is_direct ? "1" : "0"}">
              <span class="sw-tree__label">${escapeHtml(node.label)}</span>
              <span class="sw-tree__count">${node.event_count}</span>
            </button>
            <div class="sw-tree__agents">${agents}</div>
          </div>`;
      })
      .join("");

    return `
      <div class="sw-tree">
        <button class="sw-tree__node sw-tree__node--all ${allActive}" data-orch-key="__all__">
          <span class="sw-tree__label">All orchestrators</span>
        </button>
        ${nodes || `<div class="sw-tree__empty">No orchestrators yet.</div>`}
      </div>`;
  }

  // ── Render: right timeline ──────────────────────────────────

  function renderToolCall(tc, originStart, scale, level) {
    const left = ((tc.start - originStart) / 1000) * scale;
    const width = Math.max(6, ((tc.end - tc.start) / 1000) * scale);
    const dots = tc.events
      .map((e) => {
        const dl = ((ts(e) - tc.start) / 1000) * scale;
        return `<span class="sw-dot sw-dot--${escapeHtml(e.event_type)}" data-event-id="${escapeHtml(e.id)}"
                 style="left:${dl}px"
                 title="${escapeHtml(e.event_type)}${e.tool_name ? " · " + escapeHtml(e.tool_name) : ""}"></span>`;
      })
      .join("");
    const evId = tc.events[0] && tc.events[0].id;
    return `
      <div class="sw-block sw-toolcall sw-toolcall--l${level}"
           style="left:${left}px;width:${width}px;border-color:${toolColor(tc)}"
           data-event-id="${escapeHtml(evId)}"
           title="${escapeHtml(tc.toolName)}${tc.success === false ? " (failed)" : ""}">
        <span class="sw-block__label">${escapeHtml(tc.toolName)}</span>
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
        const children = t.children.map((tc) => renderToolCall(tc, t.start, scale, 2)).join("");
        const evId = t.call.events[0] && t.call.events[0].id;
        return `
          <div class="sw-block sw-task" style="left:${tLeft}px;width:${tWidth}px"
               data-event-id="${escapeHtml(evId)}" title="Task (${t.children.length} tool calls)">
            <span class="sw-block__label">Task</span>
            ${children}
          </div>`;
      })
      .join("");

    const directHtml = s.toolCalls.map((tc) => renderToolCall(tc, s.start, scale, 1)).join("");

    const looseHtml = s.looseEvents
      .map((e) => {
        const dl = ((ts(e) - s.start) / 1000) * scale;
        return `<span class="sw-dot sw-dot--${escapeHtml(e.event_type)}" data-event-id="${escapeHtml(e.id)}"
                 style="left:${dl}px;bottom:4px" title="${escapeHtml(e.event_type)}"></span>`;
      })
      .join("");

    return `
      <div class="sw-block sw-session sw-session--${s.status}"
           style="left:${left}px;width:${width}px;top:${top}px;height:${ROW_H}px;border-color:${sessionColor(s)}"
           data-session="${escapeHtml(s.sessionId)}"
           title="Session ${escapeHtml(s.sessionId)} · ${s.status}">
        <span class="sw-block__label sw-session__label">${escapeHtml(shortId(s.sessionId))} · ${s.status}</span>
        ${tasksHtml}
        ${directHtml}
        ${looseHtml}
      </div>`;
  }

  function renderTimeline(events) {
    const groups = nestLanes(buildLanes(events));
    const { min, max } = timeBounds(events);
    const scale = scaleFor(min, max);
    const contentWidth = Math.max(600, ((max - min) / 1000) * scale + 40);

    if (!events.length) {
      return `<div class="sw-empty">No observability events in this range. New events will appear here live.</div>`;
    }

    let laneLabels = "";
    let laneTracks = "";
    for (const group of groups) {
      // Sublane header for the orchestrator.
      const groupRows = group.lanes.reduce((sum, l) => sum + Math.max(1, l.sessions.length), 0);
      laneLabels += `<div class="sw-orch-label" title="${escapeHtml(group.label)}">
          <span class="sw-orch-label__name">${escapeHtml(group.label)}</span>
          <span class="sw-tree__count">${group.eventCount}</span>
        </div>`;
      laneTracks += `<div class="sw-orch-track"></div>`;

      for (const lane of group.lanes) {
        const rows = Math.max(1, lane.sessions.length);
        const h = rows * (ROW_H + LANE_PAD) + LANE_PAD;
        laneLabels += `
          <div class="sw-lane-label" style="height:${h}px" title="${escapeHtml(lane.agentName)}">
            <span>${escapeHtml(lane.agentName)}</span>
          </div>`;
        const blocks = lane.sessions.map((s, i) => renderSession(s, i, min, scale)).join("");
        laneTracks += `<div class="sw-lane" style="height:${h}px;width:${contentWidth}px">${blocks}</div>`;
      }
    }

    return `
      <div class="sw-grid">
        <div class="sw-lane-labels">${laneLabels}</div>
        <div class="sw-scroll" data-scroll>
          <div class="sw-content" style="width:${contentWidth}px">
            ${laneTracks}
          </div>
        </div>
      </div>`;
  }

  // ── Render: controls / legend ───────────────────────────────

  function renderControls() {
    const rangeBtn = (r, label) =>
      `<button class="sw-btn ${state.range === r ? "sw-btn--active" : ""}" data-range="${r}">${label}</button>`;
    const colorBtn = (c, label) =>
      `<button class="sw-btn ${state.colorMode === c ? "sw-btn--active" : ""}" data-color="${c}">${label}</button>`;
    return `
      <div class="sw-controls">
        <button class="sw-btn sw-btn--play" data-action="toggle-play">
          ${state.playing ? "&#10074;&#10074; Pause" : "&#9654; Play"}
        </button>
        <span class="sw-controls__group">
          <span class="sw-controls__label">Range</span>
          ${rangeBtn("1m", "1m")}${rangeBtn("5m", "5m")}${rangeBtn("all", "All")}
        </span>
        <span class="sw-controls__group">
          <span class="sw-controls__label">Color</span>
          ${colorBtn("status", "Status")}${colorBtn("tool", "Tool")}${colorBtn("hook", "Hook")}
        </span>
      </div>`;
  }

  function renderLegend(events) {
    let items = [];
    if (state.colorMode === "status") {
      items = [
        { label: "running", color: "var(--color-warning, #f59e0b)" },
        { label: "completed", color: "var(--color-success, #22c55e)" },
        { label: "failed", color: "var(--color-danger, #ef4444)" },
      ];
    } else if (state.colorMode === "tool") {
      const tools = new Set();
      for (const e of events) if (e.tool_name) tools.add(e.tool_name);
      items = [...tools].slice(0, 12).map((t) => ({ label: t, color: hashColor(t) }));
      if (!items.length) items = [{ label: "no tool data", color: "var(--color-accent, #818cf8)" }];
    } else {
      const hooks = new Set();
      for (const e of events) {
        const h = hookType(e);
        if (h !== "—") hooks.add(h);
      }
      items = [...hooks].slice(0, 12).map((h) => ({ label: h, color: hashColor(h) }));
      if (!items.length) items = [{ label: "no hook data", color: "var(--color-accent, #818cf8)" }];
    }
    return `
      <div class="sw-legend">
        ${items
          .map(
            (i) => `<span class="sw-legend__item">
              <span class="sw-legend__swatch" style="background:${i.color}"></span>${escapeHtml(i.label)}
            </span>`
          )
          .join("")}
      </div>`;
  }

  // ── Render root ─────────────────────────────────────────────

  function render() {
    if (destroyed) return;
    const events = scopedEvents();
    const groups = nestLanes(buildLanes(events));
    const laneCount = groups.reduce((n, g) => n + g.lanes.length, 0);
    const scopeLabel =
      state.selectedOrchestrator === undefined
        ? "all orchestrators"
        : state.selectedOrchestrator === null
        ? "Direct"
        : state.selectedOrchestrator;

    container.innerHTML = `
      <div class="sw-panel">
        <div class="sw-header">
          <h2>&#128337; Fleet Swimlane</h2>
          <span class="sw-subtitle">${escapeHtml(scopeLabel)} · ${laneCount} lane${laneCount === 1 ? "" : "s"} · ${events.length} events</span>
          <span class="sw-live ${state.live ? "sw-live--on" : "sw-live--off"}">
            ${state.live ? "&#9679; LIVE" : "&#9679; POLL"}
          </span>
        </div>
        ${renderControls()}
        ${renderLegend(events)}
        <div class="sw-body">
          <aside class="sw-sidebar">${renderTree()}</aside>
          <main class="sw-timeline">${renderTimeline(events)}</main>
        </div>
      </div>`;

    ensureStyles();
    wireEvents();
    applyAutoScroll();
  }

  // ── Interaction ─────────────────────────────────────────────

  function wireEvents() {
    container.querySelector('[data-action="toggle-play"]')?.addEventListener("click", () => {
      state.playing = !state.playing;
      render();
    });
    container.querySelectorAll("[data-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.range = btn.dataset.range;
        render();
      });
    });
    container.querySelectorAll("[data-color]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.colorMode = btn.dataset.color;
        render();
      });
    });
    // Left tree selection scopes the swimlane.
    container.querySelectorAll("[data-orch-key]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.orchKey;
        if (key === "__all__") state.selectedOrchestrator = undefined;
        else if (btn.dataset.direct === "1") state.selectedOrchestrator = null;
        else state.selectedOrchestrator = key;
        render();
      });
    });
    // Block / dot clicks open the inline detail popover.
    container.querySelectorAll("[data-event-id]").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        showPopover(el.dataset.eventId, ev);
      });
    });
    container.querySelectorAll("[data-session]").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (ev.target !== el && ev.target.classList.contains("sw-session__label") === false) return;
        ev.stopPropagation();
        showSessionPopover(el.dataset.session, ev);
      });
    });
  }

  const scrollEl = () => container.querySelector("[data-scroll]");

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

  // ── Detail popover (self-contained; no external drill-down) ──

  function closePopover() {
    if (popoverEl && popoverEl.parentNode) popoverEl.parentNode.removeChild(popoverEl);
    popoverEl = null;
  }

  function positionPopover(ev) {
    const rect = container.getBoundingClientRect();
    const x = Math.min(ev.clientX - rect.left + 8, container.clientWidth - 260);
    const y = Math.min(ev.clientY - rect.top + 8, container.clientHeight - 40);
    popoverEl.style.left = `${Math.max(4, x)}px`;
    popoverEl.style.top = `${Math.max(4, y)}px`;
  }

  function rowsHtml(rows) {
    return rows
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(
        ([k, v]) =>
          `<div class="sw-pop__row"><span class="sw-pop__key">${escapeHtml(k)}</span><span class="sw-pop__val">${escapeHtml(v)}</span></div>`
      )
      .join("");
  }

  function openPopoverEl(ev, title, bodyHtml) {
    closePopover();
    popoverEl = document.createElement("div");
    popoverEl.className = "sw-pop";
    popoverEl.innerHTML = `
      <div class="sw-pop__head">
        <span class="sw-pop__title">${escapeHtml(title)}</span>
        <button class="sw-pop__close" data-pop-close>&times;</button>
      </div>
      <div class="sw-pop__body">${bodyHtml}</div>`;
    container.querySelector(".sw-panel").appendChild(popoverEl);
    positionPopover(ev);
    popoverEl.querySelector("[data-pop-close]").addEventListener("click", closePopover);
  }

  function showPopover(eventId, ev) {
    const e = state.events.find((x) => String(x.id) === String(eventId));
    if (!e) return;
    const md = e.metadata || {};
    const keyMeta = ["hook_event_type", "gate", "attempt", "workflow_run_id", "stage"]
      .map((k) => [k, md[k] ?? (k === "workflow_run_id" ? e.workflow_run_id : k === "stage" ? e.stage : undefined)]);
    const body = rowsHtml([
      ["event", e.event_type],
      ["tool", e.tool_name],
      ["agent", e.agent_name],
      ["orchestrator", e.orchestrator ?? "Direct"],
      ["session", e.session_id],
      ["status", e.success === true ? "ok" : e.success === false ? "failed" : ""],
      ["duration", e.duration_ms != null ? `${e.duration_ms} ms` : ""],
      ["ts", e.ts],
      ["error", e.error_message],
      ...keyMeta,
    ]);
    openPopoverEl(ev, e.tool_name || e.event_type, body);
  }

  function showSessionPopover(sessionId, ev) {
    const sessEvents = state.events.filter((x) => x.session_id === sessionId);
    if (!sessEvents.length) return;
    const agent = sessEvents[0].agent_name;
    const orch = sessEvents[0].orchestrator ?? "Direct";
    const times = sessEvents.map(ts);
    const durSec = ((Math.max(...times) - Math.min(...times)) / 1000).toFixed(1);
    const body = rowsHtml([
      ["session", sessionId],
      ["agent", agent],
      ["orchestrator", orch],
      ["events", sessEvents.length],
      ["span", `${durSec}s`],
    ]);
    openPopoverEl(ev, `Session ${shortId(sessionId)}`, body);
  }

  // ── Data ────────────────────────────────────────────────────

  function mergeEvent(e) {
    if (!e || !e.agent_name) return;
    const id = e.id;
    if (id && state.events.some((x) => x.id === id)) return;
    state.events.push(e);
    if (state.events.length > 3000) state.events.splice(0, state.events.length - 3000);
  }

  async function reconcile() {
    try {
      const res = await panel.fetchEvents();
      const incoming = res.events || [];
      // Replace wholesale on initial/reconnect; the stream tops it up live.
      const seen = new Set();
      const merged = [];
      for (const e of incoming) {
        if (e.id && seen.has(e.id)) continue;
        if (e.id) seen.add(e.id);
        merged.push(e);
      }
      state.events = merged;
    } catch {
      // Keep whatever we have; render shows the empty state if nothing.
    }
    render();
  }

  async function connectLive() {
    while (!destroyed) {
      abort = new AbortController();
      try {
        // Reconcile the backlog from /events on every (re)connect.
        await reconcile();
        await panel.stream(
          (payload, id) => {
            if (!state.live) state.live = true;
            if (id != null) state.lastEventId = id;
            mergeEvent(payload);
            if (!destroyed) render();
          },
          { signal: abort.signal, lastEventId: state.lastEventId }
        );
        state.live = false;
      } catch (err) {
        state.live = false;
        if (destroyed || (err && err.name === "AbortError")) return;
      }
      if (destroyed) return;
      render();
      // Backoff before reconnecting.
      await new Promise((r) => setTimeout(r, 3000));
    }
  }

  async function loadTree() {
    try {
      const res = await panel.fetchTree();
      state.tree = res.tree || [];
    } catch {
      state.tree = [];
    }
  }

  // ── Boot ────────────────────────────────────────────────────

  container.innerHTML = `<div class="sw-panel"><div class="sw-empty">Loading fleet swimlane…</div></div>`;
  ensureStyles();

  loadTree().then(render);
  connectLive();
  // Periodic tree refresh + reconciliation safety net.
  pollTimer = setInterval(() => {
    loadTree();
    if (!state.live) reconcile();
    else render();
  }, 15_000);
  rafId = requestAnimationFrame(tick);

  return {
    refresh: reconcile,
    destroy() {
      destroyed = true;
      if (pollTimer) clearInterval(pollTimer);
      if (rafId != null) cancelAnimationFrame(rafId);
      try {
        abort?.abort();
      } catch {
        /* noop */
      }
      closePopover();
    },
  };
}

// ── Styles (scoped, theme-aware) ──────────────────────────────

let stylesInjected = false;
function ensureStyles() {
  if (stylesInjected || typeof document === "undefined") return;
  const style = document.createElement("style");
  style.setAttribute("data-observability-fleet-swimlane", "");
  style.textContent = `
    .sw-panel { display:flex; flex-direction:column; height:100%; color:var(--color-text,#e2e8f0); font-size:13px; }
    .sw-header { display:flex; align-items:center; gap:12px; padding:8px 12px; }
    .sw-header h2 { margin:0; font-size:16px; }
    .sw-subtitle { color:var(--color-text-muted,#94a3b8); }
    .sw-live { margin-left:auto; font-size:11px; font-weight:600; }
    .sw-live--on { color:var(--color-success,#22c55e); }
    .sw-live--off { color:var(--color-text-muted,#94a3b8); }
    .sw-controls, .sw-legend { display:flex; align-items:center; gap:8px; padding:4px 12px; flex-wrap:wrap; }
    .sw-controls__group { display:inline-flex; align-items:center; gap:4px; }
    .sw-controls__label, .sw-tree__empty { color:var(--color-text-muted,#94a3b8); font-size:11px; text-transform:uppercase; }
    .sw-btn { background:var(--color-surface,#1e293b); color:inherit; border:1px solid var(--color-border,#334155);
              border-radius:4px; padding:3px 8px; cursor:pointer; font-size:12px; }
    .sw-btn--active { background:var(--color-accent,#818cf8); color:#0b1120; border-color:var(--color-accent,#818cf8); }
    .sw-legend__item { display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--color-text-muted,#94a3b8); }
    .sw-legend__swatch { width:10px; height:10px; border-radius:2px; display:inline-block; }
    .sw-body { display:flex; flex:1; min-height:0; border-top:1px solid var(--color-border,#334155); }
    .sw-sidebar { width:220px; flex:0 0 220px; overflow-y:auto; border-right:1px solid var(--color-border,#334155); padding:6px; }
    .sw-tree__group { margin-bottom:4px; }
    .sw-tree__node { display:flex; align-items:center; gap:6px; width:100%; text-align:left; background:transparent;
                     color:inherit; border:1px solid transparent; border-radius:4px; padding:4px 6px; cursor:pointer; }
    .sw-tree__node:hover { background:var(--color-surface,#1e293b); }
    .sw-tree__node--active { background:var(--color-accent,#818cf8); color:#0b1120; }
    .sw-tree__node--all { font-weight:600; margin-bottom:6px; }
    .sw-tree__label { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sw-tree__count { font-size:10px; opacity:.7; }
    .sw-tree__agents { padding-left:10px; }
    .sw-tree__agent { display:flex; gap:6px; padding:2px 6px; color:var(--color-text-muted,#94a3b8); font-size:11px; }
    .sw-tree__agent-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sw-timeline { flex:1; min-width:0; position:relative; overflow:hidden; }
    .sw-grid { display:flex; height:100%; }
    .sw-lane-labels { flex:0 0 160px; overflow:hidden; border-right:1px solid var(--color-border,#334155); }
    .sw-orch-label { display:flex; gap:6px; align-items:center; padding:4px 8px; font-weight:600;
                     background:var(--color-surface,#1e293b); border-bottom:1px solid var(--color-border,#334155); }
    .sw-orch-track { height:29px; border-bottom:1px solid var(--color-border,#334155); }
    .sw-lane-label { display:flex; align-items:center; padding:0 8px; border-bottom:1px solid var(--color-border,#334155);
                     color:var(--color-text-muted,#94a3b8); }
    .sw-lane-label span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sw-scroll { flex:1; overflow-x:auto; overflow-y:hidden; }
    .sw-content { position:relative; }
    .sw-lane { position:relative; border-bottom:1px solid var(--color-border,#334155); }
    .sw-block { position:absolute; box-sizing:border-box; border:1px solid var(--color-border,#334155);
                border-radius:4px; background:rgba(129,140,248,.08); cursor:pointer; overflow:visible; }
    .sw-session { background:rgba(148,163,184,.08); border-width:2px; }
    .sw-session__label { position:absolute; top:2px; left:4px; font-size:10px; color:var(--color-text-muted,#94a3b8); }
    .sw-task { top:18px; height:20px; background:rgba(129,140,248,.12); }
    .sw-toolcall { top:20px; height:14px; background:rgba(6,182,212,.15); }
    .sw-toolcall--l2 { top:1px; height:12px; }
    .sw-block__label { position:absolute; left:3px; top:0; font-size:9px; white-space:nowrap; pointer-events:none; }
    .sw-dot { position:absolute; top:2px; width:6px; height:6px; border-radius:50%; background:var(--color-accent,#818cf8); }
    .sw-dot--error { background:var(--color-danger,#ef4444); }
    .sw-dot--gate_failed { background:var(--color-danger,#ef4444); }
    .sw-dot--gate_passed { background:var(--color-success,#22c55e); }
    .sw-empty, .sw-tree__empty { padding:24px; color:var(--color-text-muted,#94a3b8); text-align:center; }
    .sw-pop { position:absolute; z-index:50; width:250px; background:var(--color-surface,#1e293b);
              border:1px solid var(--color-border,#334155); border-radius:6px; box-shadow:0 6px 24px rgba(0,0,0,.4); font-size:12px; }
    .sw-pop__head { display:flex; align-items:center; gap:8px; padding:6px 8px; border-bottom:1px solid var(--color-border,#334155); }
    .sw-pop__title { flex:1; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sw-pop__close { background:transparent; border:0; color:inherit; cursor:pointer; font-size:16px; line-height:1; }
    .sw-pop__body { padding:6px 8px; max-height:220px; overflow:auto; }
    .sw-pop__row { display:flex; gap:8px; padding:1px 0; }
    .sw-pop__key { flex:0 0 88px; color:var(--color-text-muted,#94a3b8); }
    .sw-pop__val { flex:1; word-break:break-word; }
  `;
  document.head.appendChild(style);
  stylesInjected = true;
}
