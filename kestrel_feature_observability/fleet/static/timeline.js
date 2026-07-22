// Realtime swimlane Timeline — live scrolling time-axis with agent lanes and
// nested hierarchical blocks (#48).
//
// The original ask of the observability effort. Where the Navigator (#46) gives
// hierarchy DRILL-DOWN, this gives the TIME AXIS: a live, left-to-right scrolling
// window with agents as horizontal lanes and their spans painted as blocks.
//
//   - X = time. A scrolling window (default 30 min) with a ruler. Live mode is
//     ON by default: the window follows wall-clock (rAF-smooth) and new spans
//     stream in on the right. Panning back through history pauses follow (the
//     "Live" button resumes it). Wheel / +/- zoom the window (1 min … 24 h);
//     panning left lazily loads older pages.
//   - Y = lanes. One lane per agent (`kestrel.agent_name`), grouped under
//     collapsible project headers (`kestrel-fleet`, each `owner/repo`). Worker
//     subagents (`talon/implement`, `talon/review`) render as sub-lanes.
//   - Blocks = hierarchical spans. A session/turn root paints as an outer band;
//     its children (tool/LLM/gate/hook) pack into tracks below it — the
//     "hierarchical blocks" idiom — colored by `openinference.span.kind`
//     (TOOL/LLM/CHAIN/AGENT), error state = red accent, instant events = ticks.
//     At wide zooms sub-second blocks coalesce into density strips so we never
//     draw thousands of sub-pixel rects.
//   - Interaction: hover → tooltip; click → a detail popover with the span's
//     attributes (LLM spans reveal input.value / output.value inline) plus
//     "open in Navigator" (reveal the tree at that session/turn) and "open in
//     Phoenix" (deep-link the embed to the trace).
//
// Pure read-model over Phoenix GraphQL through the same-origin `/phoenix/graphql`
// proxy — no store, no new host routes. Live mode polls every POLL_MS for spans
// with `startTime > watermark` per project (the shared span-page query, factored
// into ./phoenix.js and reused with a `timeRange`); history paging is the same
// query with a bounded window. Phoenix down → the same friendly notice as the
// Navigator / embed sub-views. Canvas rendering keeps it smooth with thousands
// of in-window spans.

import {
  PHOENIX_URL,
  DEFAULT_PROJECT,
  UNKNOWN_AGENT,
  ATTR_INPUT_VALUE,
  ATTR_OUTPUT_VALUE,
  ATTR_MODEL_NAME,
  mintPhoenixSession,
  gql,
  PROJECTS_QUERY,
  SPAN_PAGE_QUERY,
  escapeHtml,
  parseAttributes,
  getAttr,
  ts,
  relTime,
  fmtDuration,
  clip,
  baseAgentName,
  workerOf,
  sessionKeyOf,
  spanKindOf,
} from "./phoenix.js";

// ── Tuning ────────────────────────────────────────────────────
const POLL_MS = 5_000; // live-follow poll cadence
const DEFAULT_WINDOW_MS = 30 * 60 * 1000; // 30 min visible window
const MIN_WINDOW_MS = 60 * 1000; // 1 min (max zoom-in)
const MAX_WINDOW_MS = 24 * 60 * 60 * 1000; // 24 h (max zoom-out)
const PAGE_SIZE = 500; // spans per GraphQL page
const MAX_POLL_PAGES = 6; // per-project drain cap per tick (backlog catch-up)
const SPAN_CAP = 60_000; // memory guard — prune oldest beyond this

// ── Layout ────────────────────────────────────────────────────
const RULER_H = 26; // time-ruler strip height
const GUTTER_W = 168; // left lane-label column width
const PROJECT_H = 26; // project header row height
const TRACK_H = 15; // one packed track within a lane
const LANE_VPAD = 5; // vertical padding inside a lane band
const SUBLANE_INDENT = 14; // worker sub-lane label indent
const MIN_BLOCK_PX = 3; // narrower than this → density/tick treatment

// Block color by `openinference.span.kind`. Concrete values (canvas can't read
// CSS custom properties): a dark-theme-friendly palette, distinct per kind.
const KIND_COLORS = {
  AGENT: "#6366f1",
  CHAIN: "#0ea5e9",
  LLM: "#10b981",
  TOOL: "#f59e0b",
  GUARDRAIL: "#f472b6",
  RETRIEVER: "#22d3ee",
};
const KIND_DEFAULT = "#64748b";
const ERROR_COLOR = "#ef4444";
const DENSITY_COLOR = "#94a3b8";

function kindColor(kind) {
  return KIND_COLORS[kind] || KIND_DEFAULT;
}

// Local wall-clock HH:MM:SS for the ruler ticks and tooltips.
function fmtClock(ms, withSeconds) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return withSeconds ? `${hm}:${p(d.getSeconds())}` : hm;
}

// "Nice" ruler steps (ms) — the smallest that keeps ≲10 gridlines in-window.
const NICE_STEPS = [
  1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000,
  600_000, 900_000, 1_800_000, 3_600_000, 7_200_000, 10_800_000, 21_600_000,
  43_200_000, 86_400_000,
];
function niceStep(windowMs) {
  for (const step of NICE_STEPS) if (windowMs / step <= 10) return step;
  return NICE_STEPS[NICE_STEPS.length - 1];
}

// ── View / mount ──────────────────────────────────────────────

export function mount(container, opts = {}) {
  ensureStyles();

  const openTrace = typeof opts.openTrace === "function" ? opts.openTrace : null;
  const openNavigator = typeof opts.openNavigator === "function" ? opts.openNavigator : null;

  let destroyed = false;

  // ── Time-window state ──
  let windowMs = DEFAULT_WINDOW_MS;
  let viewEnd = Date.now(); // right edge (ms); tracks wall-clock while live
  let live = true; // follow wall-clock; a manual time-pan turns this off
  let laneScrollY = 0; // vertical lane scroll offset

  const viewStart = () => viewEnd - windowMs;

  // ── Data ──
  const spans = new Map(); // spanId → normalized span
  const projects = []; // [{id, name}] — DEFAULT_PROJECT first
  const watermarks = new Map(); // projectId → newest startTime ms fetched (live)
  const historyFloor = new Map(); // projectId → oldest startTime ms fetched
  const projectFetching = new Set(); // projectId → history fetch in flight

  // ── Layout cache (rebuilt on data / collapse change, projected each frame) ──
  const collapsed = new Set(); // collapsed project names
  let layout = { rows: [], contentH: 0 };
  let drawn = []; // {x,y,w,h,span?,density?,count} for hit-testing (per frame)

  // ── DOM scaffold ──
  container.innerHTML = `
    <div class="obs-tl">
      <div class="obs-tl__toolbar">
        <span class="obs-tl__title">Timeline</span>
        <button type="button" class="obs-tl__btn" data-live title="Live-follow the wall-clock">● Live</button>
        <span class="obs-tl__grow"></span>
        <button type="button" class="obs-tl__btn" data-zoomout title="Zoom out (longer window)">−</button>
        <span class="obs-tl__window" data-window></span>
        <button type="button" class="obs-tl__btn" data-zoomin title="Zoom in (shorter window)">+</button>
        <button type="button" class="obs-tl__btn" data-refresh title="Poll now">Refresh</button>
      </div>
      <div class="obs-tl__body" data-body>
        <canvas class="obs-tl__canvas" data-canvas></canvas>
        <div class="obs-tl__tip" data-tip hidden></div>
        <div class="obs-tl__pop" data-pop hidden></div>
      </div>
    </div>`;

  const bodyEl = container.querySelector("[data-body]");
  const canvas = container.querySelector("[data-canvas]");
  const tipEl = container.querySelector("[data-tip]");
  const popEl = container.querySelector("[data-pop]");
  const liveBtn = container.querySelector("[data-live]");
  const windowEl = container.querySelector("[data-window]");
  const ctx = canvas.getContext("2d");

  let cssW = 0;
  let cssH = 0;
  let dpr = 1;
  const theme = { text: "#e2e8f0", muted: "#94a3b8", border: "#334155", surface: "#1e293b" };

  function readTheme() {
    try {
      const cs = getComputedStyle(container);
      const pick = (name, fb) => {
        const v = cs.getPropertyValue(name).trim();
        return v || fb;
      };
      theme.text = pick("--color-text", theme.text);
      theme.muted = pick("--color-text-muted", theme.muted);
      theme.border = pick("--color-border", theme.border);
      theme.surface = pick("--color-surface", theme.surface);
    } catch (_e) {
      /* keep fallbacks */
    }
  }

  function resizeCanvas() {
    if (!bodyEl) return;
    const rect = bodyEl.getBoundingClientRect();
    cssW = Math.max(1, Math.floor(rect.width));
    cssH = Math.max(1, Math.floor(rect.height));
    dpr = Math.min(3, window.devicePixelRatio || 1);
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // ── Normalize a raw Phoenix span into the read-model the timeline draws ──
  function normalize(raw, projectId, projectName) {
    const start = ts(raw.startTime);
    if (start == null) return null;
    let end = ts(raw.endTime);
    if (end == null || end < start) end = start;
    const attrs = parseAttributes(raw.attributes);
    const agentRaw = getAttr(attrs, "kestrel.agent_name");
    const agent =
      agentRaw != null && agentRaw !== "" ? baseAgentName(agentRaw) : UNKNOWN_AGENT;
    const sess = sessionKeyOf(attrs);
    const model = getAttr(attrs, ATTR_MODEL_NAME);
    const input = getAttr(attrs, ATTR_INPUT_VALUE);
    const output = getAttr(attrs, ATTR_OUTPUT_VALUE);
    return {
      id: raw.id,
      name: raw.name || "(span)",
      start,
      end,
      instant: end <= start,
      kind: spanKindOf(raw),
      status: raw.statusCode === "ERROR" ? "error" : "ok",
      agent,
      worker: workerOf(attrs),
      sessionId: sess ? sess.id : null,
      traceId: (raw.context && raw.context.traceId) || null,
      projectId,
      projectName,
      model: model != null ? String(model) : null,
      input: input != null ? String(input) : null,
      output: output != null ? String(output) : null,
      attrs,
    };
  }

  function mergeSpans(rawSpans, projectId, projectName) {
    let added = 0;
    let newestStart = null;
    let oldestStart = null;
    for (const raw of rawSpans) {
      if (!raw || !raw.id) continue;
      const s = normalize(raw, projectId, projectName);
      if (!s) continue;
      if (newestStart == null || s.start > newestStart) newestStart = s.start;
      if (oldestStart == null || s.start < oldestStart) oldestStart = s.start;
      if (!spans.has(s.id)) added += 1;
      spans.set(s.id, s);
    }
    if (newestStart != null) {
      const prev = watermarks.get(projectId);
      if (prev == null || newestStart > prev) watermarks.set(projectId, newestStart);
    }
    if (oldestStart != null) {
      const prev = historyFloor.get(projectId);
      if (prev == null || oldestStart < prev) historyFloor.set(projectId, oldestStart);
    }
    if (spans.size > SPAN_CAP) pruneSpans();
    return added;
  }

  // Memory guard: when we blow past the cap, drop the oldest-ending spans.
  function pruneSpans() {
    const all = [...spans.values()].sort((a, b) => a.end - b.end);
    const drop = all.length - SPAN_CAP;
    for (let i = 0; i < drop; i++) spans.delete(all[i].id);
  }

  // ── Fetch ──

  async function loadProjects() {
    const data = await gql(PROJECTS_QUERY);
    const nodes = ((data.projects && data.projects.edges) || [])
      .map((e) => e && e.node)
      .filter((p) => p && p.id);
    nodes.sort((a, b) => {
      const ap = a.name === DEFAULT_PROJECT ? 0 : 1;
      const bp = b.name === DEFAULT_PROJECT ? 0 : 1;
      return ap - bp || String(a.name).localeCompare(String(b.name));
    });
    projects.length = 0;
    for (const p of nodes) projects.push({ id: p.id, name: String(p.name) });
  }

  // One page of a project's spans in [start, end] (either bound optional),
  // sorted ascending so a watermark walk never skips spans on backlog.
  async function fetchSpanPage(projectId, { startMs, endMs, after }) {
    const timeRange = {};
    if (startMs != null) timeRange.start = new Date(startMs).toISOString();
    if (endMs != null) timeRange.end = new Date(endMs).toISOString();
    const data = await gql(SPAN_PAGE_QUERY, {
      projectId,
      first: PAGE_SIZE,
      after: after || null,
      filter: null,
      rootOnly: false,
      sort: { col: "startTime", dir: "asc" },
      timeRange: Object.keys(timeRange).length ? timeRange : null,
    });
    const conn = data.node && data.node.spans;
    const raw = ((conn && conn.edges) || []).map((e) => e && e.node).filter(Boolean);
    const pageInfo = (conn && conn.pageInfo) || {};
    return { raw, hasNext: Boolean(pageInfo.hasNextPage), cursor: pageInfo.endCursor || null };
  }

  // Live/initial poll: pull everything since the project's watermark (or the
  // visible window's start on the first pass), draining backlog up to a cap.
  async function pollProject(projectId, projectName) {
    let startMs = watermarks.get(projectId);
    if (startMs == null) startMs = viewStart();
    else startMs += 1; // startTime > watermark (right-open), avoid re-pulling it
    let after = null;
    let added = 0;
    for (let page = 0; page < MAX_POLL_PAGES; page++) {
      const { raw, hasNext, cursor } = await fetchSpanPage(projectId, { startMs, after });
      added += mergeSpans(raw, projectId, projectName);
      if (!hasNext || !cursor) break;
      after = cursor;
    }
    return added;
  }

  let polling = false;
  async function pollTick(manual) {
    if (destroyed || polling || (!manual && !live) || (!manual && document.hidden)) return;
    polling = true;
    let added = 0;
    try {
      for (const p of projects) {
        if (destroyed || (!manual && !live)) break;
        added += await pollProject(p.id, p.name);
      }
    } catch (_e) {
      /* transient poll errors are non-fatal — next tick retries */
    } finally {
      polling = false;
    }
    if (added) {
      buildLayout();
      requestDraw();
    }
  }

  // History paging: when the user pans left of what we've loaded, pull the gap
  // [viewStart, floor) for each project. Lazy + guarded against re-entrancy.
  async function loadHistory() {
    const target = viewStart();
    for (const p of projects) {
      if (destroyed) break;
      const floor = historyFloor.get(p.id);
      if (floor != null && floor <= target) continue; // already covered
      if (projectFetching.has(p.id)) continue;
      projectFetching.add(p.id);
      try {
        let after = null;
        let added = 0;
        const endMs = floor != null ? floor : viewEnd;
        for (let page = 0; page < MAX_POLL_PAGES; page++) {
          const { raw, hasNext, cursor } = await fetchSpanPage(p.id, {
            startMs: target,
            endMs,
            after,
          });
          added += mergeSpans(raw, p.id, p.name);
          if (!hasNext || !cursor) break;
          after = cursor;
        }
        // Mark the requested floor as covered even if the page was empty, so we
        // don't refetch the same empty gap every frame while panned back.
        const prev = historyFloor.get(p.id);
        if (prev == null || target < prev) historyFloor.set(p.id, target);
        if (added) {
          buildLayout();
          requestDraw();
        }
      } catch (_e) {
        /* non-fatal */
      } finally {
        projectFetching.delete(p.id);
      }
    }
  }

  // ── Layout: group spans → project → agent lane → worker sub-lanes ──

  function greedyPack(items) {
    // items: [{span}] → assign each a non-overlapping track (Gantt packing).
    items.sort((a, b) => a.span.start - b.span.start || a.span.end - b.span.end);
    const trackEnds = [];
    for (const it of items) {
      let placed = false;
      for (let t = 0; t < trackEnds.length; t++) {
        if (trackEnds[t] <= it.span.start) {
          it.track = t;
          trackEnds[t] = it.span.end;
          placed = true;
          break;
        }
      }
      if (!placed) {
        it.track = trackEnds.length;
        trackEnds.push(it.span.end);
      }
    }
    return trackEnds.length || 1;
  }

  function buildLayout() {
    // Bucket by project → agent → worker(null = the agent's own band).
    const byProject = new Map();
    for (const s of spans.values()) {
      let pm = byProject.get(s.projectName);
      if (!pm) {
        pm = new Map();
        byProject.set(s.projectName, pm);
      }
      let am = pm.get(s.agent);
      if (!am) {
        am = new Map();
        pm.set(s.agent, am);
      }
      const wk = s.worker || "";
      let list = am.get(wk);
      if (!list) {
        list = [];
        am.set(wk, list);
      }
      list.push({ span: s });
    }

    // Order projects: known projects first (DEFAULT_PROJECT, then repos), then
    // any leftover names present in spans but not in the projects list.
    const orderedNames = [];
    for (const p of projects) if (byProject.has(p.name)) orderedNames.push(p.name);
    for (const name of byProject.keys()) if (!orderedNames.includes(name)) orderedNames.push(name);

    const rows = [];
    let y = RULER_H;
    for (const name of orderedNames) {
      const projId = (projects.find((p) => p.name === name) || {}).id || null;
      const isCollapsed = collapsed.has(name);
      rows.push({ type: "project", name, projectId: projId, collapsed: isCollapsed, y, h: PROJECT_H });
      y += PROJECT_H;
      if (isCollapsed) continue;

      const agents = [...byProject.get(name).entries()].sort((a, b) =>
        String(a[0]).localeCompare(String(b[0])),
      );
      for (const [agent, workerMap] of agents) {
        // The agent's own band (worker-less spans: emitter roots, talon run roots).
        const mainItems = workerMap.get("") || [];
        const mainTracks = greedyPack(mainItems);
        const mainH = mainTracks * TRACK_H + 2 * LANE_VPAD;
        rows.push({
          type: "lane",
          projectName: name,
          projectId: projId,
          agent,
          worker: null,
          label: agent,
          level: 1,
          items: mainItems,
          tracks: mainTracks,
          y,
          h: mainH,
        });
        y += mainH;

        // Worker sub-lanes (talon/implement, talon/review, gate, …).
        const workers = [...workerMap.keys()].filter((w) => w !== "").sort();
        for (const wk of workers) {
          const items = workerMap.get(wk);
          const tracks = greedyPack(items);
          const h = tracks * TRACK_H + 2 * LANE_VPAD;
          rows.push({
            type: "lane",
            projectName: name,
            projectId: projId,
            agent,
            worker: wk,
            label: `${agent}/${wk}`,
            level: 2,
            items,
            tracks,
            y,
            h,
          });
          y += h;
        }
      }
    }
    layout = { rows, contentH: y };
    clampScroll();
  }

  function clampScroll() {
    const maxScroll = Math.max(0, layout.contentH - cssH);
    if (laneScrollY > maxScroll) laneScrollY = maxScroll;
    if (laneScrollY < 0) laneScrollY = 0;
  }

  // ── Projection ──
  const plotW = () => Math.max(1, cssW - GUTTER_W);
  const pxPerMs = () => plotW() / windowMs;
  const timeToX = (t) => GUTTER_W + (t - viewStart()) * pxPerMs();
  const xToTime = (x) => viewStart() + (x - GUTTER_W) / pxPerMs();

  // ── Draw ──

  let drawScheduled = false;
  function requestDraw() {
    if (destroyed || drawScheduled) return;
    drawScheduled = true;
    requestAnimationFrame(() => {
      drawScheduled = false;
      if (!destroyed) draw();
    });
  }

  function draw() {
    if (destroyed || !ctx) return;
    windowEl.textContent = fmtDuration(windowMs);
    ctx.clearRect(0, 0, cssW, cssH);
    drawn = [];

    // Ruler.
    ctx.fillStyle = theme.surface;
    ctx.fillRect(0, 0, cssW, RULER_H);
    ctx.strokeStyle = theme.border;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, RULER_H + 0.5);
    ctx.lineTo(cssW, RULER_H + 0.5);
    ctx.stroke();

    const step = niceStep(windowMs);
    const withSeconds = step < 60_000;
    const vs = viewStart();
    const first = Math.ceil(vs / step) * step;
    ctx.textBaseline = "middle";
    ctx.font = "11px ui-monospace, monospace";
    for (let t = first; t <= viewEnd; t += step) {
      const x = timeToX(t);
      if (x < GUTTER_W - 1) continue;
      ctx.strokeStyle = theme.border;
      ctx.globalAlpha = 0.5;
      ctx.beginPath();
      ctx.moveTo(x + 0.5, RULER_H);
      ctx.lineTo(x + 0.5, cssH);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = theme.muted;
      ctx.fillText(fmtClock(t, withSeconds), x + 4, RULER_H / 2);
    }

    // Lanes clip region (below the ruler).
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, RULER_H, cssW, cssH - RULER_H);
    ctx.clip();

    for (const row of layout.rows) {
      const y = row.y - laneScrollY;
      if (y + row.h < RULER_H || y > cssH) continue; // off-screen vertically
      if (row.type === "project") drawProjectHeader(row, y);
      else drawLane(row, y);
    }
    ctx.restore();

    // "now" marker while live.
    if (live) {
      const x = timeToX(viewEnd);
      if (x >= GUTTER_W && x <= cssW) {
        ctx.strokeStyle = "#22d3ee";
        ctx.globalAlpha = 0.8;
        ctx.beginPath();
        ctx.moveTo(x + 0.5, RULER_H);
        ctx.lineTo(x + 0.5, cssH);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }

    // Left gutter divider.
    ctx.strokeStyle = theme.border;
    ctx.beginPath();
    ctx.moveTo(GUTTER_W + 0.5, 0);
    ctx.lineTo(GUTTER_W + 0.5, cssH);
    ctx.stroke();

    if (!layout.rows.length) drawEmpty();
  }

  function drawProjectHeader(row, y) {
    ctx.fillStyle = theme.surface;
    ctx.fillRect(0, y, cssW, row.h);
    ctx.fillStyle = theme.text;
    ctx.font = "600 12px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    const caret = row.collapsed ? "▸" : "▾";
    ctx.fillText(`${caret} ${row.name}`, 10, y + row.h / 2);
    drawn.push({ x: 0, y, w: GUTTER_W, h: row.h, project: row });
  }

  function drawLane(row, y) {
    // Lane label (left gutter).
    ctx.fillStyle = row.level === 2 ? theme.muted : theme.text;
    ctx.font = row.level === 2 ? "11px system-ui, sans-serif" : "12px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    const labelX = 10 + (row.level - 1) * SUBLANE_INDENT;
    ctx.fillText(truncLabel(row.label, GUTTER_W - labelX - 6), labelX, y + row.h / 2);

    // Lane separator.
    ctx.strokeStyle = theme.border;
    ctx.globalAlpha = 0.4;
    ctx.beginPath();
    ctx.moveTo(GUTTER_W, y + row.h + 0.5);
    ctx.lineTo(cssW, y + row.h + 0.5);
    ctx.stroke();
    ctx.globalAlpha = 1;

    // Blocks — packed by track, coalesced into density strips when sub-pixel.
    const vs = viewStart();
    const ve = viewEnd;
    const byTrack = new Map();
    for (const it of row.items) {
      const s = it.span;
      if (s.end < vs || s.start > ve) continue; // outside window
      let arr = byTrack.get(it.track);
      if (!arr) {
        arr = [];
        byTrack.set(it.track, arr);
      }
      arr.push(s);
    }
    for (const [track, list] of byTrack) {
      const ry = y + LANE_VPAD + track * TRACK_H;
      const bh = TRACK_H - 2;
      list.sort((a, b) => a.start - b.start);
      let run = null; // pending density run {x0,x1,count,errored}
      const flush = () => {
        if (!run) return;
        ctx.fillStyle = DENSITY_COLOR;
        ctx.fillRect(run.x0, ry, Math.max(2, run.x1 - run.x0), bh);
        if (run.errored) {
          ctx.fillStyle = ERROR_COLOR;
          ctx.fillRect(run.x0, ry, Math.max(2, run.x1 - run.x0), 2);
        }
        drawn.push({ x: run.x0, y: ry, w: Math.max(2, run.x1 - run.x0), h: bh, density: run.count });
        run = null;
      };
      for (const s of list) {
        const x = timeToX(s.start);
        const rawW = s.instant ? 2 : (s.end - s.start) * pxPerMs();
        const cx = Math.max(GUTTER_W, x);
        const w = Math.max(1, x + rawW - cx);
        if (w < MIN_BLOCK_PX && !s.instant) {
          // Coalesce sub-pixel blocks into a density strip.
          if (run && cx <= run.x1 + 1) {
            run.x1 = Math.max(run.x1, cx + w);
            run.count += 1;
            if (s.status === "error") run.errored = true;
          } else {
            flush();
            run = { x0: cx, x1: cx + w, count: 1, errored: s.status === "error" };
          }
          continue;
        }
        flush();
        if (s.instant) {
          // Instant event → a tick.
          ctx.fillStyle = s.status === "error" ? ERROR_COLOR : kindColor(s.kind);
          ctx.fillRect(cx, ry, 2, bh);
          drawn.push({ x: cx - 2, y: ry, w: 6, h: bh, span: s });
          continue;
        }
        ctx.fillStyle = kindColor(s.kind);
        ctx.fillRect(cx, ry, w, bh);
        if (s.status === "error") {
          ctx.fillStyle = ERROR_COLOR;
          ctx.fillRect(cx, ry, w, 2);
        }
        // Label the block when it's wide enough to read.
        if (w > 46) {
          ctx.fillStyle = "#0b1120";
          ctx.font = "10px system-ui, sans-serif";
          const prevClip = ctx.getLineDash;
          ctx.save();
          ctx.beginPath();
          ctx.rect(cx, ry, w, bh);
          ctx.clip();
          ctx.fillText(s.name, cx + 3, ry + bh / 2);
          ctx.restore();
          void prevClip;
        }
        drawn.push({ x: cx, y: ry, w, h: bh, span: s });
      }
      flush();
    }
  }

  function drawEmpty() {
    ctx.fillStyle = theme.muted;
    ctx.font = "13px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    ctx.fillText(
      live ? "Waiting for spans…" : "No spans in this window",
      GUTTER_W + plotW() / 2,
      RULER_H + (cssH - RULER_H) / 2,
    );
    ctx.textAlign = "left";
  }

  function truncLabel(text, maxPx) {
    const s = String(text);
    if (ctx.measureText(s).width <= maxPx) return s;
    let lo = 0;
    let hi = s.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (ctx.measureText(`${s.slice(0, mid)}…`).width <= maxPx) lo = mid;
      else hi = mid - 1;
    }
    return `${s.slice(0, lo)}…`;
  }

  // ── Hit-testing ──
  function hitTest(px, py) {
    // Topmost-drawn wins (later draws overlay earlier ones).
    for (let i = drawn.length - 1; i >= 0; i--) {
      const d = drawn[i];
      if (px >= d.x && px <= d.x + d.w && py >= d.y && py <= d.y + d.h) return d;
    }
    return null;
  }

  // ── Tooltip ──
  function showTip(d, clientX, clientY) {
    if (d.project) {
      hideTip();
      return;
    }
    let html;
    if (d.density) {
      html = `<b>${d.density} spans</b><div class="obs-tl__tipdim">coalesced · zoom in to expand</div>`;
    } else {
      const s = d.span;
      const dur = s.instant ? "instant" : fmtDuration(s.end - s.start);
      html =
        `<b>${escapeHtml(s.name)}</b>` +
        `<div class="obs-tl__tipdim">${escapeHtml(s.kind)} · ${escapeHtml(dur)} · ${escapeHtml(s.status)}</div>` +
        (s.model ? `<div class="obs-tl__tipdim">model: ${escapeHtml(s.model)}</div>` : "");
    }
    tipEl.innerHTML = html;
    tipEl.hidden = false;
    const rect = bodyEl.getBoundingClientRect();
    let x = clientX - rect.left + 12;
    let y = clientY - rect.top + 12;
    const tw = tipEl.offsetWidth;
    const th = tipEl.offsetHeight;
    if (x + tw > cssW) x = cssW - tw - 4;
    if (y + th > cssH) y = clientY - rect.top - th - 8;
    tipEl.style.left = `${Math.max(2, x)}px`;
    tipEl.style.top = `${Math.max(2, y)}px`;
  }
  function hideTip() {
    tipEl.hidden = true;
  }

  // ── Detail popover ──
  function openTraceUrl(s) {
    if (!s.traceId || !s.projectId) return null;
    return `${PHOENIX_URL}projects/${encodeURIComponent(s.projectId)}/traces/${encodeURIComponent(s.traceId)}`;
  }

  function showPopover(s, clientX, clientY) {
    const rows = [];
    const add = (k, v) => {
      if (v == null || v === "") return;
      rows.push(
        `<div class="obs-tl__prow"><span class="obs-tl__pk">${escapeHtml(k)}</span>` +
          `<span class="obs-tl__pv">${escapeHtml(String(v))}</span></div>`,
      );
    };
    add("kind", s.kind);
    add("agent", s.agent);
    if (s.worker) add("worker", s.worker);
    add("duration", s.instant ? "instant" : fmtDuration(s.end - s.start));
    add("status", s.status);
    add("started", `${fmtClock(s.start, true)} · ${relTime(s.start)}`);
    if (s.model) add("model", s.model);
    if (s.sessionId) add("session", s.sessionId);
    if (s.traceId) add("trace", s.traceId);

    const io =
      s.input != null || s.output != null
        ? `<div class="obs-tl__io">` +
          (s.input != null
            ? `<div class="obs-tl__iolabel">${escapeHtml(ATTR_INPUT_VALUE)}</div><pre class="obs-tl__iopre">${escapeHtml(clip(s.input))}</pre>`
            : "") +
          (s.output != null
            ? `<div class="obs-tl__iolabel">${escapeHtml(ATTR_OUTPUT_VALUE)}</div><pre class="obs-tl__iopre">${escapeHtml(clip(s.output))}</pre>`
            : "") +
          `</div>`
        : "";

    const canNav = Boolean(openNavigator && s.sessionId);
    const canPhx = Boolean(openTrace && s.traceId && s.projectId);
    popEl.innerHTML = `
      <div class="obs-tl__phead">
        <span class="obs-tl__ptitle" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
        <button type="button" class="obs-tl__pclose" data-pclose aria-label="Close">✕</button>
      </div>
      <div class="obs-tl__pbody">${rows.join("")}${io}</div>
      <div class="obs-tl__pfoot">
        ${canNav ? `<button type="button" class="obs-tl__plink" data-pnav>open in Navigator</button>` : ""}
        ${canPhx ? `<button type="button" class="obs-tl__plink" data-pphx>open in Phoenix</button>` : ""}
      </div>`;
    popEl.hidden = false;
    const rect = bodyEl.getBoundingClientRect();
    let x = clientX - rect.left + 12;
    let y = clientY - rect.top + 12;
    const pw = popEl.offsetWidth;
    const ph = popEl.offsetHeight;
    if (x + pw > cssW) x = Math.max(2, cssW - pw - 4);
    if (y + ph > cssH) y = Math.max(2, cssH - ph - 4);
    popEl.style.left = `${x}px`;
    popEl.style.top = `${y}px`;

    const closeBtn = popEl.querySelector("[data-pclose]");
    if (closeBtn) closeBtn.addEventListener("click", hidePopover);
    const navBtn = popEl.querySelector("[data-pnav]");
    if (navBtn) {
      navBtn.addEventListener("click", () => {
        hidePopover();
        openNavigator({
          projectId: s.projectId,
          projectName: s.projectName,
          agentName: s.agent,
          worker: s.worker,
          sessionId: s.sessionId,
          traceId: s.traceId,
        });
      });
    }
    const phxBtn = popEl.querySelector("[data-pphx]");
    if (phxBtn) {
      phxBtn.addEventListener("click", () => {
        const url = openTraceUrl(s);
        if (url && openTrace) openTrace(url);
      });
    }
  }
  function hidePopover() {
    popEl.hidden = true;
    popEl.innerHTML = "";
  }

  // ── Interaction ──
  function pauseLive() {
    if (!live) return;
    setLive(false);
  }
  function setLive(on) {
    live = on;
    liveBtn.classList.toggle("obs-tl__btn--on", on);
    liveBtn.setAttribute("aria-pressed", String(on));
    if (on) {
      viewEnd = Date.now();
      loop();
    }
    requestDraw();
  }

  function zoomAt(factor, anchorX) {
    const anchorT = anchorX != null ? xToTime(anchorX) : viewEnd;
    const next = Math.min(MAX_WINDOW_MS, Math.max(MIN_WINDOW_MS, windowMs * factor));
    if (next === windowMs) return;
    if (!live && anchorX != null) {
      // Keep the point under the cursor fixed while zooming when paused.
      const frac = (anchorT - viewStart()) / windowMs;
      windowMs = next;
      viewEnd = anchorT + (1 - frac) * windowMs;
    } else {
      windowMs = next;
    }
    clampScroll();
    loadHistory();
    requestDraw();
  }

  canvas.addEventListener("wheel", (e) => {
    if (e.deltaY !== 0 && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
      // Vertical wheel → zoom around the cursor.
      e.preventDefault();
      zoomAt(e.deltaY > 0 ? 1.15 : 1 / 1.15, e.offsetX);
    } else if (e.deltaX !== 0) {
      // Horizontal wheel → pan time (history), pausing follow.
      e.preventDefault();
      pauseLive();
      viewEnd += (e.deltaX / pxPerMs());
      if (viewEnd > Date.now()) {
        viewEnd = Date.now();
        setLive(true);
      }
      loadHistory();
      requestDraw();
    }
  }, { passive: false });

  // Drag: horizontal → pan time (pauses live); vertical → scroll lanes.
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, moved: false, startX: e.offsetX, startY: e.offsetY };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        drag.x = e.clientX;
        drag.y = e.clientY;
        if (Math.abs(dx) >= Math.abs(dy)) {
          pauseLive();
          viewEnd -= dx / pxPerMs();
          const now = Date.now();
          if (viewEnd > now) viewEnd = now;
          loadHistory();
        } else {
          laneScrollY -= dy;
          clampScroll();
        }
        hideTip();
        requestDraw();
      }
      return;
    }
    // Hover → tooltip.
    const d = hitTest(e.offsetX, e.offsetY);
    if (d && !d.project) {
      canvas.style.cursor = "pointer";
      showTip(d, e.clientX, e.clientY);
    } else {
      canvas.style.cursor = d && d.project ? "pointer" : "default";
      hideTip();
    }
  });
  const endDrag = (e) => {
    if (!drag) return;
    const wasClick = !drag.moved;
    const sx = drag.startX;
    const sy = drag.startY;
    drag = null;
    try {
      canvas.releasePointerCapture(e.pointerId);
    } catch (_e) {
      /* ignore */
    }
    if (wasClick) onClick(sx, sy, e.clientX, e.clientY);
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", () => (drag = null));
  canvas.addEventListener("pointerleave", () => hideTip());

  function onClick(px, py, clientX, clientY) {
    const d = hitTest(px, py);
    if (!d) {
      hidePopover();
      return;
    }
    if (d.project) {
      // Toggle project collapse.
      if (collapsed.has(d.project.name)) collapsed.delete(d.project.name);
      else collapsed.add(d.project.name);
      buildLayout();
      requestDraw();
      return;
    }
    if (d.density) {
      // Zoom in centered on the density strip so its spans separate out.
      pauseLive();
      const t = xToTime(d.x + d.w / 2);
      windowMs = Math.max(MIN_WINDOW_MS, windowMs / 4);
      viewEnd = t + windowMs / 2;
      if (viewEnd > Date.now()) {
        viewEnd = Date.now();
      }
      loadHistory();
      requestDraw();
      return;
    }
    if (d.span) showPopover(d.span, clientX, clientY);
  }

  // Toolbar.
  liveBtn.addEventListener("click", () => setLive(!live));
  container.querySelector("[data-zoomin]").addEventListener("click", () => zoomAt(1 / 1.6, null));
  container.querySelector("[data-zoomout]").addEventListener("click", () => zoomAt(1.6, null));
  container.querySelector("[data-refresh]").addEventListener("click", () => pollTick(true));

  bodyEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hidePopover();
  });

  // ── Live render loop (rAF-smooth follow) ──
  let looping = false;
  function loop() {
    if (looping) return;
    looping = true;
    const step = () => {
      if (destroyed) {
        looping = false;
        return;
      }
      if (!live) {
        looping = false;
        return;
      }
      viewEnd = Date.now();
      draw();
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ── Resize ──
  let resizeRAF = false;
  function onResize() {
    if (resizeRAF) return;
    resizeRAF = true;
    requestAnimationFrame(() => {
      resizeRAF = false;
      if (destroyed) return;
      resizeCanvas();
      clampScroll();
      draw();
    });
  }
  window.addEventListener("resize", onResize);

  // ── Boot ──
  let pollTimer = null;

  function renderNotice() {
    if (destroyed || !bodyEl) return;
    bodyEl.innerHTML = `
      <div class="obs-notice">
        <div class="obs-notice__title">Phoenix is not running on this host</div>
        <div class="obs-notice__body">
          Install <code>kestrel-sovereign[phoenix]</code> and restart, or set
          <code>KESTREL_PHOENIX_ENABLED=1</code>.
        </div>
        <button type="button" class="obs-tl__btn" data-retry>Retry</button>
      </div>`;
    const retry = bodyEl.querySelector("[data-retry]");
    if (retry) {
      retry.addEventListener("click", () => {
        if (destroyed) return;
        teardown();
        const replacement = mount(container, opts);
        handleProxy.destroy = replacement.destroy;
      });
    }
  }

  async function boot() {
    try {
      await mintPhoenixSession();
    } catch (_e) {
      renderNotice();
      return;
    }
    if (destroyed) return;
    readTheme();
    resizeCanvas();
    requestDraw();
    try {
      await loadProjects();
    } catch (_e) {
      renderNotice();
      return;
    }
    if (destroyed) return;
    await pollTick(true); // initial fill of the visible window
    if (destroyed) return;
    buildLayout();
    pollTimer = setInterval(() => pollTick(false), POLL_MS);
    setLive(true); // live by default → starts the follow loop
  }

  function teardown() {
    destroyed = true;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    window.removeEventListener("resize", onResize);
  }

  boot();

  const handleProxy = { destroy: teardown };
  return handleProxy;
}

// ── Styles (scoped, theme-aware — console-native) ──────────────

let stylesInjected = false;
function ensureStyles() {
  if (stylesInjected || typeof document === "undefined") return;
  const style = document.createElement("style");
  style.setAttribute("data-observability-timeline", "");
  style.textContent = `
    .obs-tl { display:flex; flex-direction:column; height:100%; min-height:0;
              color:var(--color-text,#e2e8f0); font-size:13px; }
    .obs-tl__toolbar { display:flex; align-items:center; gap:8px; padding:6px 12px;
                       border-bottom:1px solid var(--color-border,#334155); }
    .obs-tl__title { font-weight:600; }
    .obs-tl__grow { flex:1; }
    .obs-tl__window { min-width:52px; text-align:center; font-size:12px; font-variant-numeric:tabular-nums;
                      color:var(--color-text-muted,#94a3b8); }
    .obs-tl__btn { background:transparent; color:var(--color-text-muted,#94a3b8);
                   border:1px solid var(--color-border,#334155); border-radius:999px;
                   padding:2px 12px; cursor:pointer; font-size:12px; font-weight:600; line-height:18px; }
    .obs-tl__btn:hover { background:var(--color-surface,#1e293b); color:var(--color-text,#e2e8f0); }
    .obs-tl__btn--on { background:var(--color-accent,#818cf8); border-color:var(--color-accent,#818cf8);
                       color:#0b1120; }
    .obs-tl__body { position:relative; flex:1; min-height:0; overflow:hidden;
                    outline:none; touch-action:none; }
    .obs-tl__canvas { position:absolute; inset:0; display:block; }
    .obs-tl__tip { position:absolute; z-index:5; pointer-events:none; max-width:320px;
                   background:var(--color-surface,#1e293b); border:1px solid var(--color-border,#334155);
                   border-radius:6px; padding:6px 9px; font-size:12px; line-height:1.35;
                   box-shadow:0 6px 20px rgba(0,0,0,.35); }
    .obs-tl__tipdim { color:var(--color-text-muted,#94a3b8); font-size:11px; }
    .obs-tl__pop { position:absolute; z-index:6; width:360px; max-width:calc(100% - 8px);
                   max-height:calc(100% - 8px); display:flex; flex-direction:column;
                   background:var(--color-surface,#1e293b); border:1px solid var(--color-border,#334155);
                   border-radius:8px; box-shadow:0 10px 30px rgba(0,0,0,.45); overflow:hidden; }
    .obs-tl__phead { display:flex; align-items:center; gap:8px; padding:8px 10px;
                     border-bottom:1px solid var(--color-border,#334155); }
    .obs-tl__ptitle { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
                      font-weight:600; }
    .obs-tl__pclose { background:transparent; border:0; color:var(--color-text-muted,#94a3b8);
                      cursor:pointer; font-size:13px; padding:2px 4px; }
    .obs-tl__pclose:hover { color:var(--color-text,#e2e8f0); }
    .obs-tl__pbody { padding:8px 10px; overflow:auto; min-height:0; }
    .obs-tl__prow { display:flex; gap:8px; padding:1px 0; font-size:12px; }
    .obs-tl__pk { flex:none; width:74px; color:var(--color-text-muted,#94a3b8); text-transform:uppercase;
                  font-size:10px; font-weight:700; letter-spacing:.04em; padding-top:2px; }
    .obs-tl__pv { flex:1; min-width:0; word-break:break-word; font-family:ui-monospace,monospace; font-size:11px; }
    .obs-tl__io { margin-top:6px; display:flex; flex-direction:column; gap:3px; }
    .obs-tl__iolabel { font-size:10px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
                       color:var(--color-text-muted,#94a3b8); }
    .obs-tl__iopre { margin:0; max-height:150px; overflow:auto; white-space:pre-wrap; word-break:break-word;
                     font-family:ui-monospace,monospace; font-size:11px; background:var(--color-bg,#0b1120);
                     border:1px solid var(--color-border,#334155); border-radius:6px; padding:6px 8px; }
    .obs-tl__pfoot { display:flex; gap:8px; padding:8px 10px; border-top:1px solid var(--color-border,#334155); }
    .obs-tl__plink { background:transparent; border:1px solid var(--color-border,#334155); border-radius:999px;
                     color:var(--color-accent,#818cf8); cursor:pointer; font-size:11px; font-weight:600;
                     padding:2px 10px; }
    .obs-tl__plink:hover { background:var(--color-surface,#1e293b); }
    .obs-tl .obs-notice { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center;
                          justify-content:center; gap:8px; padding:24px; text-align:center;
                          color:var(--color-text-muted,#94a3b8); }
    .obs-tl .obs-notice__title { font-size:15px; font-weight:600; color:var(--color-text,#e2e8f0); }
    .obs-tl .obs-notice__body { max-width:520px; line-height:1.5; }
    .obs-tl .obs-notice code { font-family:ui-monospace,monospace; background:var(--color-surface,#1e293b);
                               border:1px solid var(--color-border,#334155); border-radius:4px; padding:1px 5px; }
  `;
  document.head.appendChild(style);
  stylesInjected = true;
}
