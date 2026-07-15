// Fleet observability — Fleet Runs drill-down panel.
//
// Host-scoped console module, the second UI contribution alongside swimlane.js.
// Renders the run-level drill-down over the fleet event store:
//
//   runs list (GET /runs)  →  run detail (GET /runs/{run_id})  →  stage events
//
// A run is an orchestrator's workflow: events aggregated by `workflow_run_id`
// into {orchestrator, status, duration, stages}. Drill a run to its ordered
// stage/subagent sequence, then drill a stage to its tool-call events. Live
// updates arrive over the `workflow_run_id`-filtered GET /stream.
//
// READ-ONLY: observability owns the *view*. Run controls (acting on live
// orchestration, not the store) are an orchestration concern for a separate
// surface and are deliberately absent here — no dead buttons.
//
// Registered via HostFeature.get_ui_contributions(); sovereign mounts this
// module and serves the sibling static assets. Self-registers through the host
// `ui-ext` registerPanel API. `capability: null` → host-always-on (sovereign
// #2460), so the nav tab always renders.

import { registerPanel } from "/js/ui-ext/panels.js";

const API_PREFIX = "/api/host/observability";

// ── Utilities ─────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shortId(id) {
  const s = String(id ?? "");
  return s.length > 16 ? s.slice(0, 8) + "…" + s.slice(-6) : s;
}

function fmtDuration(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return String(iso);
  }
}

// Number of attempts recorded on a stage's events (from metadata.attempt). The
// highest attempt seen is the attempt count; anything above 1 is a re-attempt.
function attemptStats(events) {
  let max = 0;
  for (const e of events) {
    const a = e.metadata && e.metadata.attempt;
    if (typeof a === "number" && a > max) max = a;
  }
  return { attempts: max, reattempts: max > 1 ? max - 1 : 0 };
}

// Event types that mark a run/stage terminal or failed — kept in sync with the
// server's `_derive_status` so live client-side rollups match the fetched ones.
const TERMINAL_EVENT_TYPES = new Set(["agent_response", "subagent_response"]);
const FAILURE_EVENT_TYPES = new Set(["error", "gate_failed"]);

// Derive running/completed/failed from a group's events (mirrors the store's
// `_derive_status`: failure wins over completion, else running).
function deriveStatus(events) {
  for (const e of events) {
    if (FAILURE_EVENT_TYPES.has(e.event_type) || e.success === false) return "failed";
  }
  for (const e of events) {
    if (TERMINAL_EVENT_TYPES.has(e.event_type)) return "completed";
  }
  return "running";
}

// Re-derive the per-stage rollup from a run's events, mirroring the server's
// `_summarise_stages` (stages ordered by first appearance; null stage grouped
// under a `null` key) so live-appended events keep the stage list accurate.
function rebuildStages(events) {
  const order = [];
  const byStage = new Map();
  for (const e of events) {
    const stage = e.stage ?? null;
    if (!byStage.has(stage)) {
      byStage.set(stage, []);
      order.push(stage);
    }
    byStage.get(stage).push(e);
  }
  return order.map((stage) => {
    const stageEvents = byStage.get(stage);
    const named = stageEvents.find((e) => e.agent_name != null);
    return {
      stage,
      agent_name: named ? named.agent_name : null,
      status: deriveStatus(stageEvents),
      event_count: stageEvents.length,
    };
  });
}

// Artifacts surfaced from an event's metadata (best-effort; producer-defined).
function artifactsOf(event) {
  const md = event.metadata || {};
  const raw = md.artifacts ?? md.artifact ?? md.outputs;
  if (raw == null) return [];
  if (Array.isArray(raw)) return raw.map((x) => String(x));
  if (typeof raw === "object") return Object.entries(raw).map(([k, v]) => `${k}: ${v}`);
  return [String(raw)];
}

// ── Panel ─────────────────────────────────────────────────────

export const panel = {
  id: "observability-fleet-runs",
  title: "Fleet Runs",
  // Host panels are always-on; keep the capability gate off so the tab renders
  // (host gate handling — sovereign #2460).
  capability: null,

  async fetchRuns({ orchestrator, since } = {}) {
    const params = new URLSearchParams();
    if (orchestrator != null) params.set("orchestrator", orchestrator);
    if (since != null) params.set("since", since);
    const qs = params.toString();
    const res = await fetch(`${API_PREFIX}/runs${qs ? "?" + qs : ""}`, {
      credentials: "include",
    });
    if (!res.ok) throw new Error(`runs ${res.status}`);
    return res.json();
  },

  async fetchRunDetail(runId) {
    const res = await fetch(`${API_PREFIX}/runs/${encodeURIComponent(runId)}`, {
      credentials: "include",
    });
    if (!res.ok) throw new Error(`run ${res.status}`);
    return res.json();
  },

  // Fetch-based live stream, narrowed to one run via `workflow_run_id`.
  // `onEvent(payload, id)` fires per event; on reconnect it continues from the
  // last stream id via the Last-Event-ID header.
  async stream(runId, onEvent, { signal, lastEventId } = {}) {
    const params = new URLSearchParams();
    if (runId != null) params.set("workflow_run_id", runId);
    const headers = {};
    if (lastEventId) headers["Last-Event-ID"] = String(lastEventId);
    const res = await fetch(`${API_PREFIX}/stream?${params}`, {
      credentials: "include",
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
    runs: [],
    selectedRunId: null, // null = list view; string = detail view
    detail: null, // full run detail (with events) for the selected run
    expandedStage: null, // stage name whose tool-call events are expanded
    live: false,
    lastEventId: undefined,
    loading: true,
    error: null,
  };

  let abort = null;
  let pollTimer = null;
  let destroyed = false;

  // ── Status pill ─────────────────────────────────────────────

  function statusPill(status) {
    const s = status || "running";
    return `<span class="fr-pill fr-pill--${escapeHtml(s)}">${escapeHtml(s)}</span>`;
  }

  // ── Render: runs list ───────────────────────────────────────

  function renderRunsList() {
    if (!state.runs.length) {
      return `<div class="fr-empty">No runs yet. Orchestrated workflow runs will appear here.</div>`;
    }
    const rows = state.runs
      .map((run) => {
        const stages = (run.stages || [])
          .filter((s) => s.stage != null)
          .map((s) => `<span class="fr-chip">${escapeHtml(s.stage)}</span>`)
          .join("");
        return `
          <button class="fr-run" data-run-id="${escapeHtml(run.run_id)}">
            <span class="fr-run__orch" title="${escapeHtml(run.orchestrator ?? "Direct")}">
              ${escapeHtml(run.orchestrator ?? "Direct")}
            </span>
            ${statusPill(run.status)}
            <span class="fr-run__meta">${fmtDuration(run.duration_ms)}</span>
            <span class="fr-run__meta">${escapeHtml(fmtTime(run.started_at))}</span>
            <span class="fr-run__meta fr-run__id">${escapeHtml(shortId(run.run_id))}</span>
            <span class="fr-run__meta">${run.event_count} ev</span>
            <span class="fr-run__stages">${stages}</span>
          </button>`;
      })
      .join("");
    return `<div class="fr-runs">${rows}</div>`;
  }

  // ── Render: run detail (stage sequence) ─────────────────────

  function renderStageEvents(stageName) {
    const events = (state.detail.events || []).filter((e) => e.stage === stageName);
    if (!events.length) return `<div class="fr-empty fr-empty--sm">No events for this stage.</div>`;
    const rows = events
      .map((e) => {
        const arts = artifactsOf(e);
        const artHtml = arts.length
          ? `<span class="fr-ev__artifacts">${arts
              .map((a) => `<span class="fr-chip fr-chip--art">${escapeHtml(a)}</span>`)
              .join("")}</span>`
          : "";
        const status =
          e.success === true ? "ok" : e.success === false ? "failed" : "";
        return `
          <div class="fr-ev">
            <span class="fr-ev__type fr-ev__type--${escapeHtml(e.event_type)}">${escapeHtml(e.event_type)}</span>
            <span class="fr-ev__tool">${escapeHtml(e.tool_name ?? "—")}</span>
            <span class="fr-ev__agent" title="${escapeHtml(e.agent_name)}">${escapeHtml(e.agent_name)}</span>
            <span class="fr-ev__meta">${status}</span>
            <span class="fr-ev__meta">${e.duration_ms != null ? fmtDuration(e.duration_ms) : ""}</span>
            <span class="fr-ev__meta">${escapeHtml(fmtTime(e.ts))}</span>
            ${artHtml}
          </div>`;
      })
      .join("");
    return `<div class="fr-events">${rows}</div>`;
  }

  function renderDetail() {
    const d = state.detail;
    if (!d) {
      return `<div class="fr-empty">${state.loading ? "Loading run…" : "Run not found."}</div>`;
    }
    const stages = (d.stages || [])
      .map((s) => {
        const stageEvents = (d.events || []).filter((e) => e.stage === s.stage);
        const { attempts, reattempts } = attemptStats(stageEvents);
        const label = s.stage == null ? "(no stage)" : s.stage;
        const expanded = state.expandedStage === s.stage;
        const attemptHtml =
          attempts > 1
            ? `<span class="fr-stage__meta">${attempts} attempts · ${reattempts} re-attempt${reattempts === 1 ? "" : "s"}</span>`
            : "";
        return `
          <div class="fr-stage ${expanded ? "fr-stage--open" : ""}">
            <button class="fr-stage__head" data-stage="${escapeHtml(String(s.stage))}">
              <span class="fr-stage__caret">${expanded ? "▾" : "▸"}</span>
              <span class="fr-stage__name">${escapeHtml(label)}</span>
              ${statusPill(s.status)}
              <span class="fr-stage__meta" title="${escapeHtml(s.agent_name ?? "")}">${escapeHtml(s.agent_name ?? "—")}</span>
              ${attemptHtml}
              <span class="fr-stage__meta">${s.event_count} ev</span>
            </button>
            ${expanded ? renderStageEvents(s.stage) : ""}
          </div>`;
      })
      .join("");
    return `
      <div class="fr-detail">
        <div class="fr-detail__bar">
          <button class="fr-back" data-action="back">← Runs</button>
          <span class="fr-detail__orch">${escapeHtml(d.orchestrator ?? "Direct")}</span>
          ${statusPill(d.status)}
          <span class="fr-detail__meta">${fmtDuration(d.duration_ms)}</span>
          <span class="fr-detail__meta">${escapeHtml(fmtTime(d.started_at))}</span>
          <span class="fr-detail__meta fr-run__id">${escapeHtml(shortId(d.run_id))}</span>
          <span class="fr-detail__meta">${d.event_count} events</span>
        </div>
        <div class="fr-stages">${stages || `<div class="fr-empty fr-empty--sm">No stages.</div>`}</div>
      </div>`;
  }

  // ── Render root ─────────────────────────────────────────────

  function render() {
    if (destroyed) return;
    const inDetail = state.selectedRunId != null;
    const subtitle = inDetail
      ? "run detail"
      : `${state.runs.length} run${state.runs.length === 1 ? "" : "s"}`;
    const body = state.error
      ? `<div class="fr-empty">${escapeHtml(state.error)}</div>`
      : inDetail
      ? renderDetail()
      : state.loading
      ? `<div class="fr-empty">Loading runs…</div>`
      : renderRunsList();

    container.innerHTML = `
      <div class="fr-panel">
        <div class="fr-header">
          <h2>&#128260; Fleet Runs</h2>
          <span class="fr-subtitle">${escapeHtml(subtitle)}</span>
          <span class="fr-live ${state.live ? "fr-live--on" : "fr-live--off"}">
            ${state.live ? "&#9679; LIVE" : "&#9679; POLL"}
          </span>
        </div>
        <div class="fr-body">${body}</div>
      </div>`;

    ensureStyles();
    wireEvents();
  }

  // ── Interaction ─────────────────────────────────────────────

  function wireEvents() {
    container.querySelectorAll("[data-run-id]").forEach((btn) => {
      btn.addEventListener("click", () => openRun(btn.dataset.runId));
    });
    container.querySelector('[data-action="back"]')?.addEventListener("click", () => {
      closeStream();
      state.selectedRunId = null;
      state.detail = null;
      state.expandedStage = null;
      state.live = false;
      render();
    });
    container.querySelectorAll("[data-stage]").forEach((btn) => {
      btn.addEventListener("click", () => {
        // data-stage carries the String()-ified stage; map "null" back to null.
        const raw = btn.dataset.stage;
        const stage = raw === "null" ? null : raw;
        state.expandedStage = state.expandedStage === stage ? null : stage;
        render();
      });
    });
  }

  // ── Data ────────────────────────────────────────────────────

  async function loadRuns() {
    try {
      const res = await panel.fetchRuns();
      state.runs = res.runs || [];
      state.error = null;
    } catch (err) {
      state.error = `Failed to load runs (${err.message || err}).`;
    } finally {
      state.loading = false;
    }
    if (state.selectedRunId == null) render();
  }

  async function openRun(runId) {
    state.selectedRunId = runId;
    state.detail = null;
    state.expandedStage = null;
    state.loading = true;
    state.error = null;
    render();
    try {
      state.detail = await panel.fetchRunDetail(runId);
      state.error = null;
    } catch (err) {
      state.error = `Failed to load run (${err.message || err}).`;
    } finally {
      state.loading = false;
    }
    render();
    connectLive(runId);
  }

  function mergeEvent(payload) {
    if (!state.detail || !payload || !payload.id) return;
    if (payload.workflow_run_id !== state.selectedRunId) return;
    const events = state.detail.events || (state.detail.events = []);
    if (events.some((e) => e.id === payload.id)) return;
    events.push(payload);
    events.sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
    state.detail.event_count = events.length;
    // Recompute the derived rollups so stage pills / counts / new stages track
    // the live event stream (the server only computed these once, at fetch).
    state.detail.stages = rebuildStages(events);
    state.detail.status = deriveStatus(events);
    const timestamps = events.map((e) => e.ts).filter(Boolean);
    if (timestamps.length) {
      state.detail.started_at = timestamps[0];
      state.detail.ended_at = timestamps[timestamps.length - 1];
      const a = Date.parse(state.detail.started_at);
      const b = Date.parse(state.detail.ended_at);
      state.detail.duration_ms =
        Number.isNaN(a) || Number.isNaN(b) ? null : b - a;
    }
  }

  function closeStream() {
    try {
      abort?.abort();
    } catch {
      /* noop */
    }
    abort = null;
  }

  async function connectLive(runId) {
    closeStream();
    while (!destroyed && state.selectedRunId === runId) {
      abort = new AbortController();
      try {
        await panel.stream(
          runId,
          (payload, id) => {
            if (state.selectedRunId !== runId) return;
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
        if (destroyed || state.selectedRunId !== runId) return;
        if (err && err.name === "AbortError") return;
      }
      if (destroyed || state.selectedRunId !== runId) return;
      render();
      await new Promise((r) => setTimeout(r, 3000));
    }
  }

  // ── Boot ────────────────────────────────────────────────────

  container.innerHTML = `<div class="fr-panel"><div class="fr-empty">Loading fleet runs…</div></div>`;
  ensureStyles();

  loadRuns();
  // Periodic refresh of the runs list while in list view.
  pollTimer = setInterval(() => {
    if (state.selectedRunId == null) loadRuns();
  }, 15_000);

  return {
    refresh: loadRuns,
    destroy() {
      destroyed = true;
      if (pollTimer) clearInterval(pollTimer);
      closeStream();
    },
  };
}

// ── Styles (scoped, theme-aware) ──────────────────────────────

let stylesInjected = false;
function ensureStyles() {
  if (stylesInjected || typeof document === "undefined") return;
  const style = document.createElement("style");
  style.setAttribute("data-observability-fleet-runs", "");
  style.textContent = `
    .fr-panel { display:flex; flex-direction:column; height:100%; color:var(--color-text,#e2e8f0); font-size:13px; }
    .fr-header { display:flex; align-items:center; gap:12px; padding:8px 12px; border-bottom:1px solid var(--color-border,#334155); }
    .fr-header h2 { margin:0; font-size:16px; }
    .fr-subtitle { color:var(--color-text-muted,#94a3b8); }
    .fr-live { margin-left:auto; font-size:11px; font-weight:600; }
    .fr-live--on { color:var(--color-success,#22c55e); }
    .fr-live--off { color:var(--color-text-muted,#94a3b8); }
    .fr-body { flex:1; min-height:0; overflow:auto; padding:8px 12px; }
    .fr-empty { padding:24px; color:var(--color-text-muted,#94a3b8); text-align:center; }
    .fr-empty--sm { padding:10px; text-align:left; }
    .fr-runs { display:flex; flex-direction:column; gap:4px; }
    .fr-run { display:flex; align-items:center; gap:10px; width:100%; text-align:left; cursor:pointer;
              background:var(--color-surface,#1e293b); border:1px solid var(--color-border,#334155);
              border-radius:6px; padding:8px 10px; color:inherit; }
    .fr-run:hover { border-color:var(--color-accent,#818cf8); }
    .fr-run__orch { flex:1; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .fr-run__meta { color:var(--color-text-muted,#94a3b8); font-size:11px; white-space:nowrap; }
    .fr-run__id { font-family:ui-monospace,monospace; }
    .fr-run__stages { display:flex; gap:4px; flex-wrap:wrap; max-width:40%; justify-content:flex-end; }
    .fr-chip { font-size:10px; padding:1px 6px; border-radius:10px; background:rgba(129,140,248,.15);
               border:1px solid var(--color-border,#334155); color:var(--color-text-muted,#94a3b8); }
    .fr-chip--art { background:rgba(34,197,94,.14); }
    .fr-pill { font-size:10px; font-weight:600; text-transform:uppercase; padding:1px 8px; border-radius:10px; }
    .fr-pill--running { background:rgba(245,158,11,.18); color:var(--color-warning,#f59e0b); }
    .fr-pill--completed { background:rgba(34,197,94,.18); color:var(--color-success,#22c55e); }
    .fr-pill--failed { background:rgba(239,68,68,.18); color:var(--color-danger,#ef4444); }
    .fr-detail { display:flex; flex-direction:column; gap:8px; }
    .fr-detail__bar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .fr-detail__orch { font-weight:600; }
    .fr-detail__meta { color:var(--color-text-muted,#94a3b8); font-size:11px; }
    .fr-back { background:var(--color-surface,#1e293b); color:inherit; border:1px solid var(--color-border,#334155);
               border-radius:4px; padding:3px 10px; cursor:pointer; font-size:12px; }
    .fr-back:hover { border-color:var(--color-accent,#818cf8); }
    .fr-stages { display:flex; flex-direction:column; gap:4px; }
    .fr-stage { border:1px solid var(--color-border,#334155); border-radius:6px; overflow:hidden; }
    .fr-stage--open { border-color:var(--color-accent,#818cf8); }
    .fr-stage__head { display:flex; align-items:center; gap:10px; width:100%; text-align:left; cursor:pointer;
                      background:var(--color-surface,#1e293b); border:0; padding:8px 10px; color:inherit; }
    .fr-stage__head:hover { background:rgba(129,140,248,.08); }
    .fr-stage__caret { width:12px; color:var(--color-text-muted,#94a3b8); }
    .fr-stage__name { flex:1; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .fr-stage__meta { color:var(--color-text-muted,#94a3b8); font-size:11px; white-space:nowrap; }
    .fr-events { display:flex; flex-direction:column; }
    .fr-ev { display:flex; align-items:center; gap:10px; padding:4px 10px 4px 32px;
             border-top:1px solid var(--color-border,#334155); }
    .fr-ev__type { font-size:10px; font-weight:600; padding:1px 6px; border-radius:4px;
                   background:rgba(148,163,184,.14); }
    .fr-ev__type--error, .fr-ev__type--gate_failed { background:rgba(239,68,68,.18); color:var(--color-danger,#ef4444); }
    .fr-ev__type--gate_passed { background:rgba(34,197,94,.18); color:var(--color-success,#22c55e); }
    .fr-ev__tool { min-width:90px; }
    .fr-ev__agent { flex:1; color:var(--color-text-muted,#94a3b8); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .fr-ev__meta { color:var(--color-text-muted,#94a3b8); font-size:11px; white-space:nowrap; }
    .fr-ev__artifacts { display:flex; gap:4px; flex-wrap:wrap; }
  `;
  document.head.appendChild(style);
  stylesInjected = true;
}

// ── Registration via the host ui-ext panel registry ──────────
//
// The host `registerPanel` (from /js/ui-ext/panels.js) expects a `panelId`
// string and a lazy `render(bodyEl)` callback; `mount(container)` fills the
// supplied panel body on first activation. Host panels are always-on
// (`capability: null`), so no `gate` is declared — the nav tab always shows.

registerPanel({
  panelId: panel.id,
  label: panel.title,
  render: mount,
});
