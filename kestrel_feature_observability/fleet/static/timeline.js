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
const SESSION_BAND_COLOR = "#64748b"; // translucent outermost session envelope
const OPEN_EDGE_COLOR = "#22d3ee"; // live/provisional bar right-edge cap

// A `kestrel.marker == "start"` attribute tags a provisional "<name> (started)"
// span whose real closed span may not have arrived yet (talon in-flight): it
// renders open-ended until the closed span pairs with it by name.
const ATTR_MARKER = "kestrel.marker";
const MARKER_START = "start";

// A non-sensitive per-call correlation id (the Claude hook's `tool_use_id`)
// stamped on BOTH the "<tool> (started)" marker and its completed tool span, so
// concurrent same-name tools (parallel `Bash`) pair one-to-one instead of the
// first close hiding every same-name marker (#62 P2).
const ATTR_TOOL_CALL_ID = "tool.call_id";

function kindColor(kind) {
  return KIND_COLORS[kind] || KIND_DEFAULT;
}

// A span still "running" for layout/paint: no closed end yet (null endTime), or
// a provisional start-marker whose real closed span hasn't arrived. Open spans
// paint as a band from their start to the current right edge. `annotateRenderModel`
// resolves this per span (`rOpen`) — a marker whose twin/close signal has arrived
// is NOT open; a genuinely live tail is — so prefer the annotation when present.
function isOpen(s) {
  if (s.rOpen != null) return s.rOpen;
  return s.openEnded || s.marker === MARKER_START;
}

// Effective end for layout/paint: an open span extends to `nowMs` (right edge);
// a closed span uses its annotated end (`rEnd` folds in a turn's summary/next-turn
// close), falling back to the raw span end before annotation.
function effEnd(s, nowMs) {
  if (isOpen(s)) return nowMs;
  return s.rEnd != null ? s.rEnd : s.end;
}

// The base name a "<name> (started)" marker pairs with its real closed span on.
function startedBase(name) {
  return String(name).replace(/\s*\(started\)\s*$/i, "");
}

// ── Render-model resolution (marker↔parent pairing, turn extents, summaries) ──
//
// The producers (hook.py / kestrel_obs_claude_hook.py / talon via tracing.py)
// emit three span shapes the raw geometry can't paint directly (#62):
//
//   - "<x> (started)" markers — instant points whose REAL bar is a SIBLING (the
//     emitter/Claude tool-start marker, paired with its PostToolUse span) OR a
//     PARENT (talon parents the marker UNDER the span it marks). A marker must
//     never draw its own open-ended bar when its twin exists: the twin is the
//     bar; the marker is dropped. Only a genuinely orphaned/in-flight marker
//     survives as the single provisional open band (#54.5).
//   - turn roots ("<agent> turn <n>", kestrel.marker=start) — instant points that
//     ARE the turn's start; their close signal is the "turn <n> summary" CHILD
//     span, else the next turn's start in the session, else session end, else
//     (live tail only) the right edge. A closed turn never renders open-ended.
//   - "turn <n> summary" / "session summary" spans — folded into their owning
//     band (never their own bar): the band end + click stats come from them.
//
// Annotates each span in place with the fields the layout/draw read:
//   rHide    — never render (paired marker / folded summary)
//   rOpen    — render open-ended (out to the live right edge)
//   rEnd     — effective closed end (== start for a true instant)
//   rSummary — folded summary stats {kind, turnCount, toolCount, successRatio, durationMs, end}
//   rLabel   — informative band label ("turn 16 · 12 tools · 3m 40s"), else the bare name
const TURN_SUMMARY_RE = /^turn\s+\d+\s+summary$/i;
const SESSION_SUMMARY_RE = /^session\s+summary$/i;
const STARTED_RE = /\(started\)\s*$/i;

function isMarker(s) {
  return s.marker === MARKER_START;
}
function isNamedStartMarker(s) {
  return isMarker(s) && STARTED_RE.test(String(s.name || ""));
}
// A marker=start span that is NOT a "(started)" twin marker is a turn root — it
// IS the turn's start (its close signal is the summary child), never a paired bar.
function isTurnRoot(s) {
  return isMarker(s) && !isNamedStartMarker(s);
}
function isTurnSummary(s) {
  return TURN_SUMMARY_RE.test(String(s.name || ""));
}
function isSessionSummary(s) {
  return SESSION_SUMMARY_RE.test(String(s.name || ""));
}
function isSummary(s) {
  return isTurnSummary(s) || isSessionSummary(s);
}

// The session grouping key for turn-ordering / session-end lookup — the session
// id when stamped (emitter / Claude), else the trace (a lone talon-style run).
function sessionKeyFor(s) {
  return s.sessionId != null ? `s:${s.sessionId}` : `t:${s.traceId || s.id}`;
}

function numAttr(s, key) {
  const v = getAttr(s.attrs, key);
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// The per-call correlation id (`tool.call_id`) stamped on a marker + its twin,
// or null when absent (the in-process emitter has no per-call id, so those pair
// by name-order instead).
function toolCallId(s) {
  const v = getAttr(s.attrs, ATTR_TOOL_CALL_ID);
  return v != null && v !== "" ? String(v) : null;
}

// Read the folded summary stats off a "turn <n> summary" / "session summary" span.
function summaryStats(sum) {
  const turnDur = numAttr(sum, "kestrel.turn_duration_ms");
  return {
    kind: isSessionSummary(sum) ? "session" : "turn",
    turnCount: numAttr(sum, "kestrel.turn_count"),
    toolCount: numAttr(sum, "kestrel.tool_count"),
    successRatio: numAttr(sum, "kestrel.success_ratio"),
    durationMs: turnDur != null ? turnDur : numAttr(sum, "kestrel.session_duration_ms"),
    end: sum.end,
  };
}

function turnIndexOf(s) {
  const v = getAttr(s.attrs, "kestrel.turn_index");
  if (v != null && v !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  const m = /turn\s+(\d+)/i.exec(String(s.name || ""));
  return m ? Number(m[1]) : null;
}

// "turn 16 · 12 tools · 3m 40s" composed from the summary attrs; bare name fallback.
function turnLabel(s, stats) {
  const idx = turnIndexOf(s);
  const parts = [idx != null ? `turn ${idx}` : String(s.name || "")];
  if (stats) {
    if (stats.toolCount != null) {
      parts.push(`${stats.toolCount} tool${stats.toolCount === 1 ? "" : "s"}`);
    }
    if (stats.durationMs != null) {
      const d = fmtDuration(stats.durationMs);
      if (d) parts.push(d);
    }
  }
  return parts.join(" · ");
}

// Human duration for a span's own bar (folded summary duration wins for turn
// bands; a zero-width closed span is "instant"; an open span is "running").
function durText(s) {
  if (isOpen(s)) return "running";
  if (s.rSummary && s.rSummary.durationMs != null) {
    const d = fmtDuration(s.rSummary.durationMs);
    if (d) return d;
  }
  const end = s.rEnd != null ? s.rEnd : s.end;
  if (end <= s.start) return "instant";
  return fmtDuration(end - s.start);
}

// Resolve the render model over a set of normalized spans (see block comment
// above). Mutates each span in place and returns the materialized list. Pure and
// self-contained (builds its own parent index) so it's unit-testable under node.
export function annotateRenderModel(spanIter, nowMs) {
  const list = [...spanIter];
  const bySpanId = new Map();
  for (const s of list) if (s.spanId) bySpanId.set(s.spanId, s);
  const childrenOf = new Map(); // parent OTel spanId → [child records]
  for (const s of list) {
    if (!s.parentId || !bySpanId.has(s.parentId)) continue;
    let arr = childrenOf.get(s.parentId);
    if (!arr) {
      arr = [];
      childrenOf.set(s.parentId, arr);
    }
    arr.push(s);
  }
  const parentOf = (s) => (s.parentId ? bySpanId.get(s.parentId) || null : null);

  // Reset annotations. Default: a real span with no closed end is open-ended.
  for (const s of list) {
    s.rHide = false;
    s.rOpen = s.openEnded === true;
    s.rEnd = s.end;
    s.rSummary = null;
    s.rLabel = null;
  }

  // 1. Fold summaries into their owning root. A turn root absorbs the summary
  //    end (renders as a closed, labeled band); a session root keeps its instant
  //    marker tick but carries the session stats for the session band. The
  //    summary span itself never draws a bar.
  const sessionEnd = new Map(); // session key → session-summary end
  for (const s of list) {
    if (!isSummary(s)) continue;
    s.rHide = true;
    const stats = summaryStats(s);
    if (isSessionSummary(s)) sessionEnd.set(sessionKeyFor(s), s.end);
    const p = parentOf(s);
    if (!p) continue;
    p.rSummary = stats;
    if (isTurnRoot(p)) {
      p.rOpen = false;
      p.rEnd = Math.max(p.end, s.end);
      p.rLabel = turnLabel(p, stats);
    }
  }

  // 2. "<x> (started)" markers pair ONE-TO-ONE with their real twin so
  //    concurrent same-name calls never collapse into one bar. A twin is the
  //    marker's PARENT (talon parents the marker UNDER the marked span) or a
  //    SIBLING of the same base name (emitter / Claude tool-start ↔ the
  //    PostToolUse span). Sibling twins match by a stamped correlation id
  //    (`tool.call_id`) when both carry one, else are consumed in start-time
  //    order — so two live `Bash (started)` markers with only ONE completed
  //    `Bash` drop exactly one marker and leave the still-running one open
  //    (never hide EVERY same-name marker, the P2 bug). Only a genuinely
  //    unpaired marker survives as the single provisional open band (#62).

  // 2a. Parent-paired markers (talon): the marker's own PARENT is the real span.
  for (const s of list) {
    if (!isNamedStartMarker(s)) continue;
    const p = parentOf(s);
    if (p && p.name === startedBase(s.name)) s.rHide = true; // twin is the parent
  }

  // 2b. Sibling pairing, one-to-one within each parent group.
  for (const kids of childrenOf.values()) {
    const markers = [];
    const reals = [];
    for (const c of kids) {
      if (isSummary(c)) continue;
      if (isNamedStartMarker(c)) {
        if (!c.rHide) markers.push(c); // not already parent-paired
      } else if (!isMarker(c)) {
        reals.push(c);
      }
    }
    if (!markers.length || !reals.length) continue;
    const consumed = new Set();
    // (i) Exact correlation-id pairing (Claude's tool_use_id → tool.call_id).
    const realsById = new Map();
    for (const r of reals) {
      const id = toolCallId(r);
      if (id == null) continue;
      let arr = realsById.get(id);
      if (!arr) {
        arr = [];
        realsById.set(id, arr);
      }
      arr.push(r);
    }
    const rest = [];
    for (const m of markers) {
      const id = toolCallId(m);
      const arr = id != null ? realsById.get(id) : null;
      const hit = arr && arr.find((r) => !consumed.has(r));
      if (hit) {
        consumed.add(hit);
        m.rHide = true;
      } else {
        rest.push(m);
      }
    }
    // (ii) Name-order pairing for the rest: consume one unclaimed real per
    //      marker in start-time order; leftover markers stay open (running).
    if (rest.length) {
      const realsByName = new Map();
      for (const r of reals) {
        if (consumed.has(r)) continue;
        let arr = realsByName.get(r.name);
        if (!arr) {
          arr = [];
          realsByName.set(r.name, arr);
        }
        arr.push(r);
      }
      const markersByBase = new Map();
      for (const m of rest) {
        const base = startedBase(m.name);
        let arr = markersByBase.get(base);
        if (!arr) {
          arr = [];
          markersByBase.set(base, arr);
        }
        arr.push(m);
      }
      for (const [base, ms] of markersByBase) {
        ms.sort((a, b) => a.start - b.start);
        const rs = realsByName.get(base) || [];
        const n = Math.min(ms.length, rs.length);
        for (let i = 0; i < n; i++) ms[i].rHide = true; // paired → drop the marker
      }
    }
  }

  // 2c. Any named start marker still unpaired is the single provisional open
  //     band out to the live edge until its twin arrives.
  for (const s of list) {
    if (!isNamedStartMarker(s) || s.rHide) continue;
    s.rOpen = true;
    s.rEnd = s.end;
  }

  // 3. Turn roots: close at the summary child (step 1), else the next turn's
  //    start in the same session, else session end, else — live tail only — the
  //    right edge. A closed turn must never render open-ended.
  const turnsBySession = new Map();
  for (const s of list) {
    if (!isTurnRoot(s)) continue;
    const key = sessionKeyFor(s);
    let arr = turnsBySession.get(key);
    if (!arr) {
      arr = [];
      turnsBySession.set(key, arr);
    }
    arr.push(s);
  }
  for (const [key, turns] of turnsBySession) {
    turns.sort((a, b) => a.start - b.start);
    for (let i = 0; i < turns.length; i++) {
      const t = turns[i];
      if (t.rSummary) {
        t.rOpen = false; // closed by its own summary
        continue;
      }
      const next = turns[i + 1];
      if (next) {
        t.rOpen = false;
        t.rEnd = Math.max(t.end, next.start);
        continue;
      }
      const ended = sessionEnd.get(key);
      if (ended != null) {
        t.rOpen = false;
        t.rEnd = Math.max(t.end, ended);
      } else {
        t.rOpen = true; // genuinely the live tail
      }
    }
  }

  // 4. Invariant: no descendant of a CLOSED turn root may extend past its end.
  //    A descendant still open (or closing later) is pinned to the turn end — an
  //    open child of a closed turn would otherwise paint to the live right edge.
  for (const t of list) {
    if (!isTurnRoot(t) || t.rOpen) continue;
    const limit = t.rEnd;
    const stack = (childrenOf.get(t.spanId) || []).slice();
    const seen = new Set();
    while (stack.length) {
      const d = stack.pop();
      if (seen.has(d)) continue;
      seen.add(d);
      const eff = d.rOpen ? nowMs : d.rEnd;
      if (eff > limit) {
        d.rOpen = false;
        d.rEnd = Math.max(d.start, limit);
      }
      for (const c of childrenOf.get(d.spanId) || []) stack.push(c);
    }
  }

  return list;
}

// Live-poll re-fetch floor per project: the EARLIEST start among still-open
// spans (as resolved by `annotateRenderModel` — call it first). The producers
// backdate every close/summary/twin to an earlier start — a completed tool span
// starts at its pre-tool marker's timestamp, a turn/session summary at its
// turn/session start — so a forward-only `startTime > watermark` poll, whose
// watermark already passed those anchors, would NEVER re-fetch them and the turn
// would stay open / the marker unpaired until a reload. Backing the next poll's
// startTime down to this floor (an open anchor's start ≤ its awaited close's
// start) guarantees the close is pulled; once it lands the anchor resolves
// (rOpen=false) and drops out of the floor, so polling stops re-fetching it
// (#62 P1). Pure + exported for the render-model tests.
export function openStartFloors(spanIter) {
  const floors = new Map(); // projectId → earliest still-open span start
  for (const s of spanIter) {
    if (s.rOpen !== true) continue;
    const key = s.projectId != null ? s.projectId : null;
    const cur = floors.get(key);
    if (cur == null || s.start < cur) floors.set(key, s.start);
  }
  return floors;
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
  const spans = new Map(); // Phoenix node id → normalized span
  // Incremental parent-link indexes, maintained on every merge/prune so the
  // layout can rebuild the span tree cheaply and tolerate orphans (children
  // paged before parents; talon leaves exported before their held-open roots;
  // a pruned parent kept children) — an orphan renders at its best-known depth
  // and re-parents when the parent arrives on a later rebuild (#54.2).
  const spanIdToId = new Map(); // OTel context.spanId → Phoenix node id
  const childrenByParent = new Map(); // parent OTel spanId → Set<Phoenix node id>
  const projects = []; // [{id, name}] — DEFAULT_PROJECT first
  const watermarks = new Map(); // projectId → newest startTime ms fetched (live)
  const historyFloor = new Map(); // projectId → oldest startTime ms fetched
  const openFloors = new Map(); // projectId → earliest still-open span start (live re-fetch floor, #62 P1)
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
    // Distinguish a real zero-duration point span (instant tick) from a span
    // with NO closed end yet (open-ended, held-open talon run/stage): the former
    // has a valid endTime == start; the latter has a null/invalid endTime, and
    // in live mode renders as a provisional band out to the right edge (#54.5).
    const rawEnd = ts(raw.endTime);
    const hasEnd = rawEnd != null && rawEnd >= start;
    const end = hasEnd ? rawEnd : start;
    const attrs = parseAttributes(raw.attributes);
    const agentRaw = getAttr(attrs, "kestrel.agent_name");
    const agent =
      agentRaw != null && agentRaw !== "" ? baseAgentName(agentRaw) : UNKNOWN_AGENT;
    const sess = sessionKeyOf(attrs);
    const model = getAttr(attrs, ATTR_MODEL_NAME);
    const input = getAttr(attrs, ATTR_INPUT_VALUE);
    const output = getAttr(attrs, ATTR_OUTPUT_VALUE);
    const marker = getAttr(attrs, ATTR_MARKER);
    return {
      id: raw.id,
      name: raw.name || "(span)",
      start,
      end,
      instant: hasEnd && end <= start,
      openEnded: !hasEnd,
      marker: marker != null && marker !== "" ? String(marker) : null,
      kind: spanKindOf(raw),
      status: raw.statusCode === "ERROR" ? "error" : "ok",
      agent,
      worker: workerOf(attrs),
      sessionId: sess ? sess.id : null,
      // Phoenix's `parentId` is the OTel parent SPAN id (not the GraphQL node
      // id); it links to another span's `context.spanId`. Keep both so the
      // layout can rebuild the span tree (#54.1).
      spanId: (raw.context && raw.context.spanId) || null,
      parentId: raw.parentId || null,
      traceId: (raw.context && raw.context.traceId) || null,
      projectId,
      projectName,
      model: model != null ? String(model) : null,
      input: input != null ? String(input) : null,
      output: output != null ? String(output) : null,
      attrs,
    };
  }

  // Add/remove a span from the parent-link indexes.
  function indexSpan(s) {
    if (s.spanId) spanIdToId.set(s.spanId, s.id);
    if (s.parentId) {
      let set = childrenByParent.get(s.parentId);
      if (!set) {
        set = new Set();
        childrenByParent.set(s.parentId, set);
      }
      set.add(s.id);
    }
  }
  function deindexSpan(s) {
    if (s.spanId && spanIdToId.get(s.spanId) === s.id) spanIdToId.delete(s.spanId);
    if (s.parentId) {
      const set = childrenByParent.get(s.parentId);
      if (set) {
        set.delete(s.id);
        if (!set.size) childrenByParent.delete(s.parentId);
      }
    }
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
      const prev = spans.get(s.id);
      if (prev) deindexSpan(prev);
      else added += 1;
      spans.set(s.id, s);
      indexSpan(s);
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
  // Dropping a parent while keeping its children is fine — the orphans fall
  // back to depth-0 roots until (if) the parent is re-fetched (#54.2).
  function pruneSpans() {
    const all = [...spans.values()].sort((a, b) => a.end - b.end);
    const drop = all.length - SPAN_CAP;
    for (let i = 0; i < drop; i++) {
      deindexSpan(all[i]);
      spans.delete(all[i].id);
    }
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
    else {
      startMs += 1; // startTime > watermark (right-open), avoid re-pulling it
      // Back the cursor down to this project's earliest still-open span so the
      // poll re-fetches BACKDATED closes/summaries/twins (their start ≤ that
      // open anchor's start, ≤ the watermark) that a forward-only walk skips —
      // else live turns stay open and markers unpaired until reload (#62 P1).
      // Re-merges are idempotent; only genuinely new closes count as `added`.
      const floor = openFloors.get(projectId);
      if (floor != null && floor < startMs) startMs = floor;
    }
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

  // ── Layout: project → agent lane → worker sub-lanes → session bands → tree ──
  //
  // Each lane's spans group into SESSION bands, and inside each band a span TREE
  // (rebuilt from the parent index) packs depth-by-depth into tracks — the
  // russian-doll nesting the header promises: session ⊃ depth-0 roots (turns for
  // agent lanes, the run root for talon) ⊃ depth-1 (stages/tools) ⊃ depth-2
  // (tool events/markers). Session identity is derived from each span's
  // lane-local ROOT through the parent index, NOT from a per-span session id —
  // child spans don't carry one (that's issue #55, which this rendering must not
  // depend on) — so a whole trace/turn stays one band even though only its root
  // is tagged.

  // Greedy Gantt packing of one depth level: assign each span a non-overlapping
  // track, writing the ABSOLUTE track (offset + local index) into `trackOf`.
  function packInto(arr, trackOf, offset, nowMs) {
    arr.sort((a, b) => a.start - b.start || effEnd(a, nowMs) - effEnd(b, nowMs));
    const ends = [];
    for (const s of arr) {
      let placed = false;
      for (let t = 0; t < ends.length; t++) {
        if (ends[t] <= s.start) {
          trackOf.set(s.id, offset + t);
          ends[t] = effEnd(s, nowMs);
          placed = true;
          break;
        }
      }
      if (!placed) {
        trackOf.set(s.id, offset + ends.length);
        ends.push(effEnd(s, nowMs));
      }
    }
    return ends.length;
  }

  // Build one session band: the parent tree over `members`, depth-packed into
  // tracks. Tolerates orphans — a member whose parent isn't in the band renders
  // as a depth-0 root and re-parents when the parent arrives on a later build.
  function buildBand(members, nowMs) {
    const memberIds = new Set(members.map((s) => s.id));
    // Children within the band, via the incremental parentSpanId→children index
    // (filtered to members: a talon run root's stage children live in a separate
    // worker sub-lane, so they're excluded here and the root reads as a leaf).
    const kids = new Map(); // node id → [child spans]
    for (const s of members) {
      const set = s.spanId ? childrenByParent.get(s.spanId) : null;
      if (!set) continue;
      const arr = [];
      for (const cid of set) {
        if (cid === s.id || !memberIds.has(cid)) continue;
        const c = spans.get(cid);
        if (c) arr.push(c);
      }
      if (arr.length) kids.set(s.id, arr);
    }
    const hasInBandParent = (s) => {
      if (!s.parentId) return false;
      const pid = spanIdToId.get(s.parentId);
      return pid != null && pid !== s.id && memberIds.has(pid);
    };
    const roots = members
      .filter((s) => !hasInBandParent(s))
      .sort((a, b) => a.start - b.start);

    // Depth via DFS from the roots (visited-guarded against pathological cycles).
    const depthOf = new Map();
    const visited = new Set();
    (function assign(list, depth) {
      for (const s of list) {
        if (visited.has(s.id)) continue;
        visited.add(s.id);
        depthOf.set(s.id, depth);
        const cs = kids.get(s.id);
        if (cs && cs.length) assign(cs.slice().sort((a, b) => a.start - b.start), depth + 1);
      }
    })(roots, 0);
    for (const s of members) if (!depthOf.has(s.id)) depthOf.set(s.id, 0);

    // Per-depth greedy packing → each depth occupies a contiguous track range,
    // stacked below the previous so children always sit under their parents.
    const byDepth = new Map();
    for (const s of members) {
      const d = depthOf.get(s.id);
      let arr = byDepth.get(d);
      if (!arr) {
        arr = [];
        byDepth.set(d, arr);
      }
      arr.push(s);
    }
    const trackOf = new Map();
    let total = 0;
    for (const d of [...byDepth.keys()].sort((a, b) => a - b)) {
      total += packInto(byDepth.get(d), trackOf, total, nowMs);
    }

    // Subtree extents (memoized; self-first write guards cycles) → each non-leaf
    // span's envelope spans its whole subtree horizontally AND vertically, so an
    // instant parent (the emitter's zero-width AGENT marker) still wraps its
    // children.
    const subExtent = new Map();
    function computeSub(s) {
      const cached = subExtent.get(s.id);
      if (cached) return cached;
      const self = {
        start: s.start,
        end: effEnd(s, nowMs),
        maxTrack: trackOf.get(s.id),
        open: isOpen(s),
      };
      subExtent.set(s.id, self);
      for (const c of kids.get(s.id) || []) {
        const sub = computeSub(c);
        if (sub.start < self.start) self.start = sub.start;
        if (sub.end > self.end) self.end = sub.end;
        if (sub.maxTrack > self.maxTrack) self.maxTrack = sub.maxTrack;
        if (sub.open) self.open = true;
      }
      return self;
    }

    const placed = members.map((s) => ({
      span: s,
      depth: depthOf.get(s.id),
      track: trackOf.get(s.id),
    }));
    const envelopes = [];
    for (const s of members) {
      if (!kids.has(s.id)) continue; // leaves get no envelope
      const sub = computeSub(s);
      const top = trackOf.get(s.id);
      envelopes.push({
        span: s,
        depth: depthOf.get(s.id),
        trackTop: top,
        trackCount: sub.maxTrack - top + 1,
        start: sub.start,
        end: sub.end,
        open: sub.open,
      });
    }

    let start = Infinity;
    let end = -Infinity;
    let open = false;
    for (const s of members) {
      if (s.start < start) start = s.start;
      const e = effEnd(s, nowMs);
      if (e > end) end = e;
      if (isOpen(s)) open = true;
    }
    // Fold the session summary (parented to the session root, hidden as a bar)
    // into the band: its stats power the band click popover and its end closes
    // the band even when the last turn's own summary landed earlier (#62).
    let summary = null;
    for (const s of members) {
      if (s.rSummary && s.rSummary.kind === "session") {
        summary = s.rSummary;
        break;
      }
    }
    if (summary && !open && summary.end > end) end = summary.end;
    const rep = roots[0] || members[0];
    return {
      tracks: total || 1,
      placed,
      envelopes,
      start,
      end,
      open,
      summary,
      sessionId: rep ? rep.sessionId : null,
      traceId: rep ? rep.traceId : null,
      count: members.length,
    };
  }

  // Group a lane's spans into session bands keyed by each span's lane-local ROOT
  // (walk parentId within the lane): child spans inherit their root's session,
  // so a session/turn stays ONE band even though only roots carry the id. A
  // null-session root falls back to its trace id (one band per trace); a lone
  // single-span trace is just a plain bar at band level. Sessions stack in
  // start-time order — concurrent sessions own disjoint track ranges and can
  // never interleave (the bug this kills). Lane height = Σ per-session tracks.
  function laneBands(laneItems, nowMs) {
    // Marker↔twin pairing and summary folding are resolved up front in
    // `annotateRenderModel` (rHide spans are already filtered out in buildLayout),
    // so a lane's items are just what should paint.
    const items = laneItems;
    const bySpanId = new Map();
    for (const it of items) if (it.span.spanId) bySpanId.set(it.span.spanId, it.span);
    const laneRoot = (s) => {
      let cur = s;
      let guard = 0;
      while (cur.parentId && bySpanId.has(cur.parentId) && guard++ < 100000) {
        const p = bySpanId.get(cur.parentId);
        if (!p || p === cur) break;
        cur = p;
      }
      return cur;
    };
    const groups = new Map();
    for (const it of items) {
      const root = laneRoot(it.span);
      const key = root.sessionId != null ? `s:${root.sessionId}` : `t:${root.traceId || root.id}`;
      let arr = groups.get(key);
      if (!arr) {
        arr = [];
        groups.set(key, arr);
      }
      arr.push(it.span);
    }
    const minStart = (list) => {
      let m = Infinity;
      for (const s of list) if (s.start < m) m = s.start;
      return m;
    };
    const ordered = [...groups.values()].sort((a, b) => minStart(a) - minStart(b));

    const outItems = [];
    const sessionBands = [];
    const envelopes = [];
    let laneTracks = 0;
    for (const members of ordered) {
      const band = buildBand(members, nowMs);
      const offset = laneTracks;
      for (const p of band.placed) {
        outItems.push({ span: p.span, depth: p.depth, track: offset + p.track });
      }
      for (const e of band.envelopes) {
        envelopes.push({
          span: e.span,
          depth: e.depth,
          trackTop: offset + e.trackTop,
          trackCount: e.trackCount,
          start: e.start,
          end: e.end,
          open: e.open,
        });
      }
      sessionBands.push({
        sessionId: band.sessionId,
        traceId: band.traceId,
        start: band.start,
        end: band.end,
        open: band.open,
        summary: band.summary,
        trackTop: offset,
        trackCount: band.tracks,
        count: band.count,
      });
      laneTracks += band.tracks;
    }
    return { items: outItems, sessionBands, envelopes, tracks: laneTracks || 1 };
  }

  function buildLayout() {
    const nowMs = Date.now();
    // Resolve the render model first: pair "(started)" markers with their twin,
    // close turn bands at their summary/next-turn, fold summaries. rHide spans
    // (paired markers, summary bars) are then excluded from every lane (#62).
    annotateRenderModel(spans.values(), nowMs);
    // Recompute the live re-fetch floors from the just-resolved openness so the
    // next poll pulls backdated closes for still-open work (#62 P1).
    openFloors.clear();
    for (const [k, v] of openStartFloors(spans.values())) openFloors.set(k, v);
    // Bucket by project → agent → worker(null = the agent's own band).
    const byProject = new Map();
    for (const s of spans.values()) {
      if (s.rHide) continue;
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
        const mainLane = laneBands(workerMap.get("") || [], nowMs);
        const mainH = mainLane.tracks * TRACK_H + 2 * LANE_VPAD;
        rows.push({
          type: "lane",
          projectName: name,
          projectId: projId,
          agent,
          worker: null,
          label: agent,
          level: 1,
          items: mainLane.items,
          sessionBands: mainLane.sessionBands,
          envelopes: mainLane.envelopes,
          tracks: mainLane.tracks,
          y,
          h: mainH,
        });
        y += mainH;

        // Worker sub-lanes (talon/implement, talon/review, gate, …).
        const workers = [...workerMap.keys()].filter((w) => w !== "").sort();
        for (const wk of workers) {
          const lane = laneBands(workerMap.get(wk), nowMs);
          const h = lane.tracks * TRACK_H + 2 * LANE_VPAD;
          rows.push({
            type: "lane",
            projectName: name,
            projectId: projId,
            agent,
            worker: wk,
            label: `${agent}/${wk}`,
            level: 2,
            items: lane.items,
            sessionBands: lane.sessionBands,
            envelopes: lane.envelopes,
            tracks: lane.tracks,
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

    const vs = viewStart();
    const ve = viewEnd;
    const rightT = viewEnd; // open-ended spans/bands extend to the live right edge

    // Project a time+track rect into canvas space, clipped to the plot area.
    const rectFor = (startT, endT, open, trackTop, trackCount) => {
      const eT = open ? Math.max(endT, rightT) : endT;
      const x = timeToX(startT);
      const cx = Math.max(GUTTER_W, x);
      const w = Math.max(1, timeToX(eT) - cx);
      const ry = y + LANE_VPAD + trackTop * TRACK_H;
      const rh = Math.max(2, trackCount * TRACK_H - 1);
      return { cx, w, ry, rh };
    };
    const onScreen = (startT, endT, open) => !((open ? rightT : endT) < vs || startT > ve);

    // 1. Session bands FIRST — the lightest, outermost envelope. Pushed to
    //    drawn[] before everything so real spans (drawn later) win the topmost
    //    hit-test; its own exposed area gives a session-level hover.
    for (const b of row.sessionBands || []) {
      if (!onScreen(b.start, b.end, b.open)) continue;
      const r = rectFor(b.start, b.end, b.open, b.trackTop, b.trackCount);
      ctx.fillStyle = SESSION_BAND_COLOR;
      ctx.globalAlpha = 0.1;
      ctx.fillRect(r.cx, r.ry, r.w, r.rh);
      ctx.globalAlpha = 1;
      drawn.push({ x: r.cx, y: r.ry, w: r.w, h: r.rh, band: b });
    }

    // 2. Parent (subtree) envelopes, shallow → deep so a deeper envelope wins
    //    the hit-test within its sub-region. Tinted by the parent's span kind;
    //    the exposed part of each envelope hovers/clicks as that parent span.
    const envs = (row.envelopes || []).slice().sort((a, b) => a.depth - b.depth);
    for (const e of envs) {
      if (!onScreen(e.start, e.end, e.open)) continue;
      const r = rectFor(e.start, e.end, e.open, e.trackTop, e.trackCount);
      ctx.fillStyle = e.span.status === "error" ? ERROR_COLOR : kindColor(e.span.kind);
      ctx.globalAlpha = 0.14 + Math.min(0.16, e.depth * 0.05);
      ctx.fillRect(r.cx, r.ry, r.w, r.rh);
      ctx.globalAlpha = 1;
      drawn.push({ x: r.cx, y: r.ry, w: r.w, h: r.rh, span: e.span });
    }

    // 3. Span identity bars, grouped by ABSOLUTE track (each track belongs to a
    //    single session+depth), coalescing sub-pixel runs into density strips
    //    PER track — so a wide session band never coalesces with its sub-second
    //    children (the coalescer runs per depth level for free).
    const byTrack = new Map();
    for (const it of row.items || []) {
      const s = it.span;
      if (!onScreen(s.start, s.end, isOpen(s))) continue;
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
        const open = isOpen(s);
        const closedEnd = s.rEnd != null ? s.rEnd : s.end;
        const sEnd = open ? rightT : closedEnd;
        // A true instant (zero-width) is a tick; a turn root whose band end was
        // folded in from its summary (rEnd > start) paints as a labeled bar.
        const tick = !open && closedEnd <= s.start;
        const x = timeToX(s.start);
        const rawW = tick ? 2 : (sEnd - s.start) * pxPerMs();
        const cx = Math.max(GUTTER_W, x);
        const w = Math.max(1, x + rawW - cx);
        if (!tick && w < MIN_BLOCK_PX) {
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
        if (tick) {
          // Instant event → a 2px tick (track-assigned inside its parent band).
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
        if (open) {
          // Still-running / provisional: a bright cap at the live right edge.
          ctx.fillStyle = OPEN_EDGE_COLOR;
          ctx.globalAlpha = 0.6;
          ctx.fillRect(cx + w - 2, ry, 2, bh);
          ctx.globalAlpha = 1;
        }
        // Label the block when it's wide enough to read — an informative band
        // label ("turn 16 · 12 tools · 3m 40s") when folded from a summary, else
        // the bare span name. Clipped to the bar; truncation is the clip.
        if (w > 46) {
          ctx.fillStyle = "#0b1120";
          ctx.font = "10px system-ui, sans-serif";
          ctx.save();
          ctx.beginPath();
          ctx.rect(cx, ry, w, bh);
          ctx.clip();
          ctx.fillText(s.rLabel || s.name, cx + 3, ry + bh / 2);
          ctx.restore();
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
    } else if (d.band) {
      const b = d.band;
      const title = b.sessionId
        ? `session ${b.sessionId}`
        : b.traceId
          ? `trace ${b.traceId}`
          : "session";
      const dur = b.end > b.start ? fmtDuration((b.open ? viewEnd : b.end) - b.start) : "";
      // Fold the session summary stats (tool count, success ratio) into the
      // band hover when present (#62).
      const sum = b.summary;
      const stats = sum
        ? `${sum.toolCount != null ? `${sum.toolCount} tool${sum.toolCount === 1 ? "" : "s"}` : ""}${
            sum.successRatio != null ? ` · ${Math.round(sum.successRatio * 100)}% ok` : ""
          }`
        : "";
      html =
        `<b>${escapeHtml(title)}</b>` +
        `<div class="obs-tl__tipdim">${b.count} span${b.count === 1 ? "" : "s"}${
          dur ? ` · ${escapeHtml(dur)}` : ""
        }${b.open ? " · live" : ""}</div>` +
        (stats.trim() ? `<div class="obs-tl__tipdim">${escapeHtml(stats.trim())}</div>` : "");
    } else if (d.span) {
      const s = d.span;
      html =
        `<b>${escapeHtml(s.rLabel || s.name)}</b>` +
        `<div class="obs-tl__tipdim">${escapeHtml(s.kind)} · ${escapeHtml(durText(s))} · ${escapeHtml(s.status)}</div>` +
        (s.model ? `<div class="obs-tl__tipdim">model: ${escapeHtml(s.model)}</div>` : "");
    } else {
      hideTip();
      return;
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

  // A tool/event child span carries no session attribute — only the trace root
  // does (the emitter's `web_search` tool child has agent_name but no
  // session_id, while its root marker/summary carry it). Walk the parentId →
  // spanId chain (the same index the layout uses) to the nearest ancestor that
  // carries a session id, so the popover's "open in Navigator" reveal works for
  // the hierarchical children too. Orphan / not-yet-loaded parent → null (the
  // button stays hidden, best-effort) (#54.6).
  function resolveSessionId(s) {
    let cur = s;
    let guard = 0;
    while (cur && guard++ < 100000) {
      if (cur.sessionId != null) return cur.sessionId;
      if (!cur.parentId) return null;
      const pid = spanIdToId.get(cur.parentId);
      if (pid == null || pid === cur.id) return null;
      cur = spans.get(pid);
    }
    return null;
  }

  function showPopover(s, clientX, clientY) {
    const sessionId = resolveSessionId(s);
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
    add("duration", durText(s));
    add("status", s.status);
    add("started", `${fmtClock(s.start, true)} · ${relTime(s.start)}`);
    // Folded summary stats — a turn/session band answers "what was this turn":
    // tool count, success ratio, turn count (read off the summary attrs, #62).
    if (s.rSummary) {
      const sum = s.rSummary;
      if (sum.turnCount != null) add("turns", sum.turnCount);
      if (sum.toolCount != null) add("tools", sum.toolCount);
      if (sum.successRatio != null) add("success", `${Math.round(sum.successRatio * 100)}%`);
    }
    if (s.model) add("model", s.model);
    if (sessionId) add("session", sessionId);
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

    const canNav = Boolean(openNavigator && sessionId);
    const canPhx = Boolean(openTrace && s.traceId && s.projectId);
    popEl.innerHTML = `
      <div class="obs-tl__phead">
        <span class="obs-tl__ptitle" title="${escapeHtml(s.name)}">${escapeHtml(s.rLabel || s.name)}</span>
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
          sessionId,
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

  // The session band's click popover: the folded `session summary` stats (turn /
  // tool count, success ratio, duration) so the band answers "what was this
  // session" without a duplicate summary bar (#62).
  function showBandPopover(b, clientX, clientY) {
    const sum = b.summary;
    if (!sum) return;
    const rows = [];
    const add = (k, v) => {
      if (v == null || v === "") return;
      rows.push(
        `<div class="obs-tl__prow"><span class="obs-tl__pk">${escapeHtml(k)}</span>` +
          `<span class="obs-tl__pv">${escapeHtml(String(v))}</span></div>`,
      );
    };
    if (sum.turnCount != null) add("turns", sum.turnCount);
    if (sum.toolCount != null) add("tools", sum.toolCount);
    if (sum.successRatio != null) add("success", `${Math.round(sum.successRatio * 100)}%`);
    add("duration", fmtDuration((b.open ? viewEnd : b.end) - b.start));
    if (b.sessionId) add("session", b.sessionId);
    if (b.traceId) add("trace", b.traceId);
    const title = b.sessionId ? `session ${b.sessionId}` : "session";
    popEl.innerHTML = `
      <div class="obs-tl__phead">
        <span class="obs-tl__ptitle" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
        <button type="button" class="obs-tl__pclose" data-pclose aria-label="Close">✕</button>
      </div>
      <div class="obs-tl__pbody">${rows.join("")}</div>`;
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
    if (d && (d.span || d.density)) {
      canvas.style.cursor = "pointer";
      showTip(d, e.clientX, e.clientY);
    } else if (d && d.band) {
      // A band with folded summary stats is clickable (session popover); a bare
      // grouping envelope stays informational.
      canvas.style.cursor = d.band.summary ? "pointer" : "default";
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
    if (d.band) {
      // A band with folded summary stats opens a session popover; a bare
      // grouping envelope stays decorative.
      if (d.band.summary) showBandPopover(d.band, clientX, clientY);
      else hidePopover();
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
