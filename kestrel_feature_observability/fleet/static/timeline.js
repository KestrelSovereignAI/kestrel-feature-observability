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
//     "Live" button resumes it), and the present edge is MAGNETIC: any time-pan
//     that lands within a few px of `now` snaps there and re-engages Live.
//     +/- (and the density strip) zoom the window (1 min … 24 h); panning left
//     lazily loads older pages.
//   - Y = lanes. One lane per (agent, orchestrator) pair (`kestrel.agent_name`
//     × `kestrel.orchestrator`), grouped under collapsible project headers
//     (`kestrel-fleet`, each `owner/repo`). A run launched BY another agent
//     nests under that agent's lane — Emma's talon run is `Emma/talon` UNDER
//     Emma — while a launcher with no lane in that project (`claude-code`) keeps
//     its own labeled lane at the top level, and unattributed runs keep the
//     plain `talon` lane (#101). Worker subagents (`talon/implement`,
//     `talon/review`) render as sub-lanes of their agent's lane.
//   - Blocks = hierarchical spans. A session/turn root paints as an outer band;
//     its children (tool/LLM/gate/hook) pack into tracks below it — the
//     "hierarchical blocks" idiom — colored by `openinference.span.kind`
//     (TOOL/LLM/CHAIN/AGENT), error state = red accent, instant events = ticks.
//     At wide zooms sub-second blocks coalesce into density strips so we never
//     draw thousands of sub-pixel rects.
//   - Interaction: scroll PANS, never zooms (#94) — a plain vertical scroll or
//     vertical drag moves through the lanes, a plain horizontal scroll or
//     horizontal drag pans time, and a diagonal scroll does both. Zoom is an
//     explicit action: ctrl/⌘+scroll (which is also how trackpad pinch arrives)
//     around the cursor, the +/- buttons, or a density-strip click. Hover →
//     tooltip; click → a detail popover with the span's attributes (LLM spans
//     reveal input.value / output.value inline) plus "open in Navigator"
//     (reveal the tree at that session/turn) and "open in Phoenix" (deep-link
//     the embed to the trace).
//
// Pure read-model over Phoenix GraphQL through the same-origin `/phoenix/graphql`
// proxy — no store, no new host routes. Live mode polls every POLL_MS for spans
// with `startTime >= watermark` per project (the shared span-page query, factored
// into ./phoenix.js and reused with a `timeRange`) — inclusive, because the
// millisecond the watermark sits on may still be half-ingested (#109); history
// paging is the same query with a bounded window. Every paged fetch — live,
// history gap, reveal window — is a WALK held in one registry keyed by (project,
// purpose): capped at MAX_POLL_PAGES per pass, resumed on its own cursor and
// fixed bounds, and counted as covering its range only once it finishes
// (#109). Phoenix down → the same friendly notice as the
// Navigator / embed sub-views. Canvas rendering keeps it smooth with thousands
// of in-window spans.

import {
  PHOENIX_URL,
  DEFAULT_PROJECT,
  UNKNOWN_AGENT,
  ATTR_INPUT_VALUE,
  ATTR_OUTPUT_VALUE,
  ATTR_MODEL_NAME,
  ATTR_RUN_ID,
  ATTR_STAGE,
  ATTR_MARKER,
  MARKER_START,
  ATTR_TOOL_OUTCOME,
  ATTR_FEATURE_NAME,
  ATTR_ORCHESTRATOR,
  ATTR_TURN_ID,
  ATTR_AGENT_DID,
  OUTCOME_COMPLETED,
  OUTCOME_IDLE,
  mintPhoenixSession,
  gql,
  PROJECTS_QUERY,
  SPAN_PAGE_QUERY,
  walkTraceSpans,
  spanIdFilter,
  escapeHtml,
  parseAttributes,
  getAttr,
  ts,
  fmtDuration,
  baseAgentName,
  workerOf,
  sessionKeyOf,
  spanKindOf,
  isTurnSummarySpan,
  isSessionSummarySpan,
  ROLE_TURN_ROOT,
  spanRoleOf,
  spanSummaryOf,
  normalizeSpanDetail,
  renderSpanDetail,
  spanTooltipLines,
  buildNavigatorRevealTarget,
  stopActionModel,
  stopTargetFromDetail,
} from "./phoenix.js";

/** Advance retained Stop targets from a focused full-trace completion read. */
export function observeFocusedTurnCompletion(controller, detail, evidence) {
  if (!controller || typeof controller.observe !== "function") return false;
  return controller.observe(stopTargetFromDetail(detail, evidence));
}

// ── Tuning ────────────────────────────────────────────────────
const POLL_MS = 5_000; // live-follow poll cadence
const DEFAULT_WINDOW_MS = 30 * 60 * 1000; // 30 min visible window
const MIN_WINDOW_MS = 60 * 1000; // 1 min (max zoom-in)
const MAX_WINDOW_MS = 24 * 60 * 60 * 1000; // 24 h (max zoom-out)
const PAGE_SIZE = 500; // spans per GraphQL page
const MAX_POLL_PAGES = 6; // per-project drain cap per tick (backlog catch-up)
const MAX_HISTORY_ROUNDS = 4; // bounded walks per history pass (viewport moved mid-fetch)
const SPAN_CAP = 60_000; // memory guard — evict oldest CLOSED SESSIONS beyond this (#111)
// Ancestor backfill (#108) — bounds of ONE on-demand resolve; see `resolveAncestors`.
const ANCESTOR_HOPS = 8; // parent generations one RUN may walk up (depth — never per pass)
const ANCESTOR_BATCH = 100; // span ids per exact-id request (breadth of one request)
const ANCESTOR_PASS_IDS = 400; // ids one pass may ask for; the surplus CARRIES to the next
const ANCESTOR_SETTLE_MS = 250; // pan/zoom debounce — resolve once the gesture stops
// Why a project can have a walk in flight — the second half of a walk's identity
// in the registry, so the three fetch paths resume independently of each other.
const WALK_LIVE = "live"; // watermark-forward drain (the poll)
const WALK_HISTORY = "history"; // bounded gap left of what's loaded (a pan)
const WALK_REVEAL = "reveal"; // bounded window around a cross-view reveal target
const WHEEL_ZOOM_STEP = 1.15; // one modifier-scroll notch (out; 1/step zooms in)
const LIVE_SNAP_PX = 8; // pan within this many px of `now` → snap + re-engage Live

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
const HIGHLIGHT_COLOR = "#facc15"; // exact cross-view reveal
const ABANDONED_FILL = "#475569"; // SIGKILL'd/never-completed run — muted slate
const ABANDONED_HATCH = "#94a3b8"; // diagonal hatch over the muted fill
const ABANDONED_STUB_PX = 24; // childless abandoned marker → fixed stub width (paint-time)
// A denied/incomplete tool never RAN, so the producers emit it as a truthful
// zero-duration point span — which would paint as an unreadable 2px tick, making
// a refusal LESS visible than the orphaned band it used to leave (#84). Paint
// separates from data (the ABANDONED_STUB_PX precedent): the span data stays
// zero-duration, while the bar gets a fixed width wide enough to carry its
// "<tool> · denied" label. Visible and labeled, never claiming runtime it
// didn't have.
const OUTCOME_STUB_PX = 72;

// An IDLE scheduler heartbeat (#87): a tick that ran successfully and did
// nothing. Painted as a distinct narrow beat rather than the wide refusal stub —
// heartbeats arrive every minute, so stubs would overdraw into an unreadable
// smear — and coalesced into a COUNTED "N heartbeats" run only when the beats
// would actually overlap at the current zoom (#92). Distinct color so idle reads
// as "alive, nothing to do" at a glance, never mistaken for work (kind color) or
// a refusal (the outcome stub).
const IDLE_COLOR = "#2dd4bf"; // teal — alive but idle
const HEARTBEAT_PX = 3; // one heartbeat beat's paint width
const HEARTBEAT_LABEL_PX = 56; // a coalesced run at least this wide is labeled

// The virtual scheduler session's own envelope (#92). It IS drawn across the
// band's real min→max extent — hiding the extent hid the session's duration and
// made a 29-minute band read as a 0ms stub — but never as a solid task bar: a
// faint translucent wash under a dashed outline says "virtual/heartbeat session,
// not a running task", which is what the original 5-hour-Claw-bar complaint was
// actually about.
const VIRTUAL_BAND_ALPHA = 0.07;
const VIRTUAL_BAND_EDGE_ALPHA = 0.55;
const VIRTUAL_BAND_DASH = [5, 4];

// The marker / tool-outcome contract lives in phoenix.js (imported above), so the
// Timeline's pairing rules and the shared detail model read the SAME keys:
// `kestrel.marker == "start"` tags a provisional "<name> (started)" span whose
// real closed span may not have arrived yet (talon in-flight) — it renders
// open-ended until the closed span pairs with it by name — and
// `kestrel.tool_outcome` (#84/#87) says how a tool call ended: "completed" (it
// ran and did something), "idle" (it ran and did nothing — a scheduler
// heartbeat), else "denied" / "incomplete" — refused or aborted before it ever
// ran. The terminal non-completed span pairs with its "(started)" marker like
// any other twin, so a refusal renders as one visible stub instead of an orphan.

// The emitter's per-process scheduler pseudo-session (`kestrel.session_id`), the
// VIRTUAL session every cron tick parents into. Its band renders as a distinctly
// virtual envelope across its real extent plus its discrete ticks and an
// aggregate count (#87/#92).
const SCHEDULER_SESSION_ID = "scheduler";

// A non-sensitive per-call correlation id (the Claude hook's `tool_use_id`)
// stamped on BOTH the "<tool> (started)" marker and its completed tool span, so
// concurrent same-name tools (parallel `Bash`) pair one-to-one instead of the
// first close hiding every same-name marker (#62 P2).
const ATTR_TOOL_CALL_ID = "tool.call_id";

// The producers stamp `kestrel.orchestrator` with the literal "Direct" when a
// session has NO orchestrator (a human-driven Claude Code run). It is a sentinel,
// not an agent, so it never nests a lane under anything (#101).
const DIRECT_ORCHESTRATOR = "Direct";

// SIGKILL / power-loss / hard-reboot guard: the producers close their spans on
// CATCHABLE exits, but a hard kill leaves work OPEN forever — an unpaired
// "<x> (started)" marker, a summary-less live-tail turn root, or a held-open
// talon run/stage root — and the earlier resolution would paint any of them
// running-to-now. A still-open span whose start is older than this AND whose
// whole RUN COHORT has been silent for at least this long is treated as
// ABANDONED: bounded to observed child activity (or an instant stub), never the
// live edge. Liveness is judged by the cohort, NOT the span's own subtree — a
// held-open talon run parents its "<stage> (started)" markers and its tool spans
// as SIBLINGS, so a marker's subtree is structurally empty even while its run is
// busy; cohort activity within the window keeps the whole run live (genuinely
// in-flight). Re-resolved every poll, so a fresh cohort span flips it back to
// running (#67/#69). 15 min (not 5): the cap only needs to eventually retire
// truly-dead (typically hours-old) orphans, and a tighter bound false-positives a
// legitimately-quiet-but-alive run (a long test gate / long generation with no
// tool spans).
const STALE_MARKER_MS = 15 * 60 * 1000;

// Visual abandonment (rAbandoned) is separate from ingestion: even after a span
// is capped, a late BACKDATED completion (a twin/summary whose start == the
// span's own start, ≤ the live-poll watermark) may still be exported — a run
// that was merely quiet for a while, not truly dead. For a BOUNDED grace past
// the staleness threshold the abandoned span keeps anchoring the re-fetch floor
// (`rReconcile`) so that late close can still be pulled and un-abandon it. The
// bound is essential: an ancient SIGKILL'd run (whose twin will NEVER arrive)
// must eventually drop out of the floor, else the poll would peg its cursor to
// days-ago forever and re-scan that whole span every tick (#67 P1). Tracks
// STALE_MARKER_MS (raised to 15 min in #69).
const STALE_RECONCILE_MS = 15 * 60 * 1000;

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

// ── Talon stage labels (#104) ─────────────────────────────────
//
// Talon opens a stage scope as `start_span(stage)`, so the stage span is NAMED
// by the stage it is — `implement`, `review`, `coordinate`, `gate`,
// `completion-check` — and the Timeline painted both its lane gutter and its
// bar as that raw identifier. To an operator these are prose, so they read as
// prose: `Implement`, `Completion check`. A hyphenated stage becomes SENTENCE
// case (one leading capital), not Title-Case-Every-Word.
//
// DISPLAY ONLY. `kestrel.stage` is a producer contract matched on elsewhere
// (`workerOf` keys the worker sub-lanes off it), so neither the attribute nor
// the span's own `name` is ever rewritten — only what gets painted changes.
function stageTitle(stage) {
  const s = String(stage).replace(/-+/g, " ");
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// The prose display name of a talon STAGE span, else null (leave it be).
//
// A stage span is one carrying a non-empty `kestrel.stage` whose NAME is that
// stage value, or that value plus the `" (started)"` suffix of its
// live-visibility twin (#80) — the same suffix the marker↔parent pairing below
// already keys on. Both spellings must land on the same prose or one stage
// paints two: a marker never equals its own stage value, which is precisely
// what an equality-only rule misses (#103).
//
// The NAME test alone is the whole rule — necessary AND sufficient.
// `kestrel.stage` is stamped on EVERY span inside the stage (the
// `command_execution` / `Bash` tool spans, the `ci` and `self-review` gate
// checks, `ci (waiting)`), and none of those is named for its stage, so the
// name test excludes them exactly. Gating on the span KIND on top of it is not
// merely redundant but wrong: the producer picks the kind per stage (LLM for
// implement/review, AGENT for coordinate, CHAIN for gate) and that mapping
// drifts, so a kind gate silently stops recognizing the bars it was written
// for while the gutter — which has no such gate — keeps reading as prose, and
// one stage renders under two spellings.
function stageDisplayName(s) {
  const stage = getAttr(s.attrs, ATTR_STAGE);
  if (stage == null || stage === "") return null;
  const token = String(stage);
  const raw = String(s.name || "");
  const base = startedBase(raw);
  if (base !== token) return null;
  return stageTitle(token) + raw.slice(base.length); // " (started)" survives
}

// The name a composed band label builds on: a stage span's prose form, else the
// span name as the producer emitted it.
function labelBase(s) {
  const stage = stageDisplayName(s);
  return stage != null ? stage : s.name;
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
//   rHide      — never render (paired marker / folded summary)
//   rOpen      — render open-ended (out to the live right edge)
//   rEnd       — effective closed end (== start for a true instant)
//   rSummary   — folded summary stats {kind, turnCount, toolCount, successRatio, durationMs, end}
//   rLabel     — informative band label ("turn 16 · 12 tools · 3m 40s"), else the bare name
//   rAbandoned — a SIGKILL'd/never-completed still-open span: capped (rOpen=false,
//                rEnd=latest child end else start), drawn muted/hatched not live (#67)
//   rReconcile — ingestion-only: an abandoned span still within the bounded
//                reconcile grace that keeps anchoring the live re-fetch floor so
//                a late backdated twin/summary can still be pulled (#67 P1). NOT
//                read by the draw layer — purely a poll-floor signal.
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
  return isTurnSummarySpan(s);
}
function isSessionSummary(s) {
  return isSessionSummarySpan(s);
}
function isSummary(s) {
  return isTurnSummary(s) || isSessionSummary(s);
}

// The session grouping key for turn-ordering / session-end lookup — the session
// id when stamped (emitter / Claude), else the trace (a lone talon-style run).
function sessionKeyFor(s) {
  return s.sessionId != null ? `s:${s.sessionId}` : `t:${s.traceId || s.id}`;
}

// The RUN COHORT key for liveness (#69): the whole talon run / agent session /
// lone trace a span belongs to. `kestrel.run_id` (stamped on EVERY talon span)
// wins so a held-open run's markers and its SIBLING tool spans share one cohort;
// else the session id (emitter / Claude stamp `kestrel.session_id` on every span
// of a session); else the trace. The abandoned-cap judges liveness by this cohort
// rather than a span's own subtree, since talon parents markers and tool spans as
// siblings (a marker's subtree is empty even mid-run).
function cohortKeyFor(s) {
  const runId = getAttr(s.attrs, ATTR_RUN_ID);
  if (runId != null && runId !== "") return `r:${runId}`;
  if (s.sessionId != null) return `s:${s.sessionId}`;
  return `t:${s.traceId || s.id}`;
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

// The raw `kestrel.tool_outcome` of a span, else null (older producers, non-tool
// spans).
function toolOutcome(s) {
  const v = getAttr(s.attrs, ATTR_TOOL_OUTCOME);
  return v == null || v === "" ? null : String(v);
}

// The REFUSED tool outcome of a span ("denied" / "incomplete"), else null. A
// completed tool, an idle heartbeat (it DID run — it just did nothing, so it is
// not "unfinished"), and any span with no outcome stamped are all null, so only
// an explicit refusal gets the wide stub treatment.
function unfinishedOutcome(s) {
  const outcome = toolOutcome(s);
  if (outcome == null) return null;
  if (outcome === OUTCOME_COMPLETED || outcome === OUTCOME_IDLE) return null;
  return outcome;
}

// An idle scheduler heartbeat (#87) — a tick that ran successfully and did
// nothing. Its own visual category: neither work nor refusal.
function isIdleBeat(s) {
  return toolOutcome(s) === OUTCOME_IDLE;
}

// Whether a span belongs to the emitter's virtual `scheduler` pseudo-session.
// The emitter stamps `kestrel.session_id` on EVERY span it emits (roots, ticks
// and markers alike), so this holds for a tick as well as its band root.
function isSchedulerSpan(s) {
  return s.sessionId === SCHEDULER_SESSION_ID;
}

// Read the folded summary stats off a "turn <n> summary" / "session summary" span.
function summaryStats(sum) {
  const shared = spanSummaryOf(sum);
  return {
    kind: isSessionSummary(sum) ? "session" : "turn",
    ...shared,
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

// Resolve the render model over a set of normalized spans (see block comment
// above). Mutates each span in place and returns the materialized list. Pure and
// self-contained (builds its own parent index) so it's unit-testable under node.
export function annotateRenderModel(spanIter, nowMs) {
  const list = [...spanIter];
  const bySpanId = new Map();
  for (const s of list) if (s.spanId) bySpanId.set(s.spanId, s);
  const childrenOf = new Map(); // parent OTel spanId → [child records]
  for (const s of list) {
    if (!s.parentId) continue;
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
    s.rAbandoned = false;
    s.rReconcile = false;
    s.rOutcome = null;
    s.rIdle = false;
    s.rScheduler = isSchedulerSpan(s);
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

  // 2d. Terminal denied/incomplete tool spans (#84): a refused tool never ran,
  //     so its span is honestly zero-duration — which alone would paint as an
  //     unlabeled 2px tick. Flag it so the draw layer gives it a fixed visible
  //     stub, and name the outcome in the label. Its "(started)" marker was
  //     already paired away above, so the stub is the ONE bar for that call.
  for (const s of list) {
    if (s.rHide) continue;
    const outcome = unfinishedOutcome(s);
    if (!outcome) continue;
    s.rOutcome = outcome;
    s.rOpen = false; // terminal: never a live/provisional band
    if (s.rLabel == null) s.rLabel = `${labelBase(s)} · ${outcome}`;
  }

  // 2e. Idle scheduler heartbeats (#87): a tick that ran and did nothing. The
  //     emitter used to DROP these, which made an idle-but-alive scheduler
  //     indistinguishable from a dead one; they now emit, so the renderer owns
  //     making them legible — a distinct narrow beat (not the wide refusal stub:
  //     they arrive every minute), labeled "<tick> · idle", coalesced into a
  //     COUNTED run when dense (draw layer), never silently collapsed to nothing.
  for (const s of list) {
    if (s.rHide || !isIdleBeat(s)) continue;
    s.rIdle = true;
    s.rOpen = false; // terminal: the tick completed, it just did no work
    if (s.rLabel == null) s.rLabel = `${labelBase(s)} · ${OUTCOME_IDLE}`;
  }

  // 2f. Talon stage bars and their "(started)" markers read as prose (#104):
  //     `implement` → `Implement`, `completion-check` → `Completion check`. The
  //     draw layer paints `rLabel || name`, so the prose form rides on the same
  //     label channel every other composed band uses — the span's `name` and its
  //     `kestrel.stage` attribute are left exactly as the producer emitted them.
  //     Resolved for EVERY stage span, paired-away marker included: whether a
  //     marker paints depends on whether its twin has landed yet, and the label
  //     a span carries must not depend on that.
  for (const s of list) {
    if (s.rLabel != null) continue;
    const stage = stageDisplayName(s);
    if (stage != null) s.rLabel = stage;
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

  // 5. Abandoned-run cap (SIGKILL / power loss / hard reboot). Steps 2c/3 and the
  //    openEnded default all leave a still-running span `rOpen=true`; a hard kill
  //    can't be caught, so a run that died days ago would keep painting open-ended
  //    out to the live edge. Apply ONE unified pass over the resolved model: any
  //    span still open past STALE_MARKER_MS whose ENTIRE RUN COHORT has been silent
  //    that long is abandoned — it never got a completion.
  //
  //    Liveness is judged by the span's cohort, NOT its own subtree. A LIVE talon
  //    run holds its run/stage spans OPEN (not yet exported), so forward-poll loads
  //    only their "<stage> (started)" markers and the `command_execution` tool
  //    spans — BOTH parented under the (missing) held-open stage, so the tools are
  //    SIBLINGS of the marker, not its children. A marker's own subtree is
  //    therefore structurally always empty, so per-span subtree liveness would flag
  //    every stage/run marker as abandoned the moment it crosses the window despite
  //    constant sibling tool activity under the same run — the #69 flicker. Instead
  //    the run is in-flight iff its cohort (`kestrel.run_id`, else session, else
  //    trace) saw activity within the window; a truly-dead run's ENTIRE cohort is
  //    silent, so its held-open markers still cap (the genuine SIGKILL case). Bound
  //    the cap to observed evidence (the latest exported child end) or, childless,
  //    to an instant stub — never `nowMs`. Re-resolved every poll, so it's
  //    order-independent and self-correcting — a fresh cohort span flips it back to
  //    live on the next poll. This narrows ONLY the fate of a span that stays open
  //    past the threshold; all pairing / turn-extent logic above is unchanged
  //    (#67/#69).

  // Latest activity per cohort: max over members of max(start, effective end).
  // Computed ONCE here — after steps 1–4 set `rEnd` (folded summaries / turn
  // extents count as activity), before step 5 caps it below.
  const cohortActivity = new Map();
  for (const s of list) {
    const key = cohortKeyFor(s);
    const act = Math.max(s.start, s.rEnd != null ? s.rEnd : s.end);
    const cur = cohortActivity.get(key);
    if (cur == null || act > cur) cohortActivity.set(key, act);
  }
  for (const s of list) {
    if (s.rOpen !== true) continue;
    if (nowMs - s.start <= STALE_MARKER_MS) continue; // recent → genuinely live
    // The run is in-flight if ANYTHING in its cohort started or ended within the
    // window — a sibling tool under a held-open run counts, unlike the marker's
    // (empty) own subtree. Keep it open-ended when the cohort is still alive.
    const act = cohortActivity.get(cohortKeyFor(s));
    if (act != null && nowMs - act <= STALE_MARKER_MS) continue;
    // Cohort silent past the window → abandoned. Retain the subtree walk ONLY to
    // bound the cap `rEnd` to the latest observed child end (evidence).
    let latestEnd = s.start; // latest observed child end (evidence for rEnd)
    const stack = (childrenOf.get(s.spanId) || []).slice();
    const seen = new Set();
    while (stack.length) {
      const d = stack.pop();
      if (seen.has(d)) continue;
      seen.add(d);
      const dEnd = d.rEnd != null ? d.rEnd : d.end;
      if (dEnd > latestEnd) latestEnd = dEnd;
      for (const c of childrenOf.get(d.spanId) || []) stack.push(c);
    }
    s.rAbandoned = true;
    s.rOpen = false;
    s.rEnd = latestEnd; // latest child end, else s.start (childless → instant stub)
    // Visual cap ≠ giving up on ingestion. For a BOUNDED grace past the
    // staleness threshold, keep anchoring the live re-fetch floor so a late,
    // backdated twin/summary (start == s.start ≤ the poll watermark) can still
    // be pulled and un-abandon this span. Beyond the grace we stop — an ancient
    // dead run must not peg the poll cursor to its start forever (#67 P1).
    if (nowMs - s.start <= STALE_MARKER_MS + STALE_RECONCILE_MS) s.rReconcile = true;
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
// (#62 P1). An abandoned span within its bounded reconcile grace (`rReconcile`)
// ALSO anchors the floor: visual abandonment must not sever the backdated-twin
// re-fetch path, so a late completion for a merely-quiet run can still be pulled
// and un-abandon it — the grace bound then drops truly-dead runs (#67 P1). Pure
// + exported for the render-model tests.
export function openStartFloors(spanIter) {
  const floors = new Map(); // projectId → earliest open/reconciling span start
  for (const s of spanIter) {
    if (s.rOpen !== true && s.rReconcile !== true) continue;
    const key = s.projectId != null ? s.projectId : null;
    const cur = floors.get(key);
    if (cur == null || s.start < cur) floors.set(key, s.start);
  }
  return floors;
}

// A summary closes a turn only when it is the direct child of that turn's
// stamped root. Name, kind, DID, and turn attributes are not enough: nested
// workflow spans legitimately inherit all of them and may use the same name.
// Materialize and index once so both the focused read and loaded-inventory
// index retain linear complexity.
function completedTurnIdentityKeys(spanIter) {
  const spans = [...(spanIter || [])];
  const rootsBySpanId = new Map();
  const completed = new Set();

  for (const candidate of spans) {
    if (spanRoleOf(candidate) !== ROLE_TURN_ROOT) continue;
    const detail = normalizeSpanDetail(candidate);
    const key = turnCompletionIdentityKey(detail);
    if (candidate.rSummary && candidate.rOpen === false && key) {
      completed.add(key);
    }
    if (detail.spanId && key) rootsBySpanId.set(detail.spanId, key);
  }

  for (const candidate of spans) {
    if (!isTurnSummarySpan(candidate)) continue;
    const detail = normalizeSpanDetail(candidate);
    const parentKey = detail.parentSpanId
      ? rootsBySpanId.get(detail.parentSpanId)
      : null;
    if (!parentKey) continue;
    const summaryKey = turnCompletionIdentityKey(detail);
    if (summaryKey && summaryKey !== parentKey) continue;
    completed.add(parentKey);
  }
  return completed;
}

/** Resolve completion only when the supplied turn inventory is authoritative. */
export function turnCompletionEvidence(
  spanIter,
  detail,
  { truncated = false } = {},
) {
  const target = detail && typeof detail === "object" ? detail : {};
  const targetKey = turnCompletionIdentityKey(target);
  const completed = targetKey
    ? completedTurnIdentityKeys(spanIter).has(targetKey)
    : false;
  return Object.freeze({
    completed,
    completionKnown: completed || truncated !== true,
  });
}

function turnCompletionIdentityKey(detail) {
  return detail?.turnId && detail?.agentDid
    ? `${detail.agentDid}\u0000${detail.turnId}`
    : null;
}

/** Canonical key for the one focused completion read shared by a turn trace. */
export function timelineTurnCompletionKey(value) {
  return value?.projectId && value?.traceId
    ? `${value.projectId}\u0000${value.traceId}`
    : null;
}

/** Whether two rendered spans share the one trace-scoped turn completion read. */
export function sameTimelineTurnCompletion(left, right) {
  const leftKey = timelineTurnCompletionKey(left);
  const rightKey = timelineTurnCompletionKey(right);
  return leftKey !== null && leftKey === rightKey;
}

/** Index every completed turn in one pass over a loaded span inventory. */
export function turnCompletionIndex(spanIter) {
  return completedTurnIdentityKeys(spanIter);
}

/** Whether a popover needs the bounded focused trace read used only by Stop. */
export function needsFocusedTurnCompletion(
  spanIter,
  detail,
  { stopAvailable = true } = {},
) {
  if (!stopAvailable || !stopTargetFromDetail(detail).addressable) return false;
  return !turnCompletionEvidence(spanIter, detail, { truncated: true }).completed;
}

// ── Ancestor backfill (#108) ──────────────────────────────────
//
// Every paged fetch is windowed on `startTime`, and Phoenix's `timeRange` filters
// on startTime ALONE. So a run/turn root that STARTED before the visible window
// is never pulled while its children — which started inside it — are: pan back,
// or open the panel mid-run, and the run renders as a scatter of orphaned tool
// ticks with no parent band. The draw-time overlap test is correct; the spans are
// simply not loaded, which is why this is a fetch fix and not a paint one.
//
// The missing parents are fetched EXACTLY, by OTel span id (`spanIdFilter`),
// which needs no time window at all — so there are no page cursors, no retry
// ledger and no per-trace settled state here.
//
// Key an ancestor request by (project, span id): the fetch goes through a project
// node, so the same id under two projects is two different asks.
export function ancestorAskKey(projectId, spanId) {
  return `${projectId} ${spanId}`;
}

// The pure half: which parents are missing, and at what DEPTH each would be
// fetched. An entry is `{ projectId, spanId, depth }` — a parent id some loaded
// span names that is not itself loaded.
//
// Depth belongs to the ID, not to the pass or the loop that asks for it. A parent
// named by a span this run never fetched is one hop out (depth 1); one revealed by
// a span the run fetched at depth d is d + 1, read from `depths` (ask key → the
// depth that id was asked at). Anything past `maxDepth` is not returned at all —
// that chain is left orphaned, which the render model already draws at its
// best-known depth. Carrying the depth this way is what makes the bound hold when
// a wide frontier is drained across several passes: a carried id keeps the depth
// it was discovered at instead of starting over at one, so breadth can never buy
// depth.
//
// Deduplicated per (project, parent), keeping the SHALLOWEST claim — two children
// at different depths naming one parent is one ask, at the honest hop count.
export function ancestorFrontier(spanIter, depths, maxDepth) {
  const list = [...spanIter];
  const loaded = new Set();
  for (const s of list) if (s.spanId) loaded.add(s.spanId);
  const seen = new Map(); // ask key → entry
  const out = [];
  for (const s of list) {
    const pid = s.parentId;
    if (!pid || loaded.has(pid)) continue;
    const projectId = s.projectId != null ? s.projectId : null;
    const childDepth = (depths && depths.get(ancestorAskKey(projectId, s.spanId))) || 0;
    const depth = childDepth + 1;
    if (maxDepth != null && depth > maxDepth) continue; // past the hop cap — leave it
    const key = ancestorAskKey(projectId, pid);
    const prev = seen.get(key);
    if (prev) {
      if (depth < prev.depth) prev.depth = depth;
      continue;
    }
    const entry = { projectId, spanId: pid, depth };
    seen.set(key, entry);
    out.push(entry);
  }
  return out;
}

// What ONE pass should ask for out of a frontier: every entry not asked for yet,
// grouped into requests of at most `batchSize` ids sharing a (project, depth) —
// EXCEPT the surplus past `budget`, which comes back as `carried` ENTRIES, depth
// intact, for the next pass to ask for.
//
// Breadth and depth are separate, and this function only ever bounds breadth: the
// budget caps the ids one pass asks for before the caller re-checks, never how
// deep the run may walk. The surplus is carried, not dropped, which is exactly
// what #105 got wrong when it truncated at its first 800 ids and never came back
// for the rest — and each carried id keeps its own accumulated depth, so a
// frontier too wide for one pass costs more passes, never more hops.
//
// Pure (it does not touch `asked`) + exported for the render-model tests.
export function ancestorRequestPlan(frontier, asked, opts) {
  const o = opts || {};
  const budget = Number.isFinite(o.budget) ? o.budget : ANCESTOR_PASS_IDS;
  const batchSize = Number.isFinite(o.batchSize) ? o.batchSize : ANCESTOR_BATCH;
  const seen = asked || new Set();
  const requests = [];
  const carried = [];
  const open = new Map(); // `${projectId} ${depth}` → the request still filling
  let remaining = budget;
  for (const entry of frontier) {
    if (seen.has(ancestorAskKey(entry.projectId, entry.spanId))) continue;
    if (remaining <= 0) {
      carried.push(entry); // over budget for this pass — the next one asks for it
      continue;
    }
    remaining -= 1;
    const key = `${entry.projectId} ${entry.depth}`;
    let req = open.get(key);
    if (!req) {
      req = { projectId: entry.projectId, depth: entry.depth, ids: [] };
      open.set(key, req);
      requests.push(req);
    }
    req.ids.push(entry.spanId);
    if (req.ids.length >= batchSize) open.delete(key); // full — the next id opens another
  }
  return { requests, carried };
}

// ── Virtual scheduler-session band (#87/#92) ──
//
// Every scheduler tick parents into ONE immortal `session=scheduler` pseudo-root
// (minted once per emitter process and reused for its whole lifetime), so the raw
// band geometry paints one continuous envelope from the first tick to the last —
// observed live as a 5-HOUR bar for two 0-duration ticks 5h apart. The problem
// with that bar was never that it EXISTED: it was that a plain session envelope
// reads as a solid running task. Suppressing the envelope outright (#87) traded
// that for the opposite lie — a 29-minute band of 71 heartbeats painting as a
// short 0ms-looking stub, hiding both its real extent and its ticks.
//
// So the band reports its REAL extent (min start → max end over its members) and
// asks for an envelope, which the paint layer draws in the distinctly VIRTUAL
// style (translucent wash + dashed outline, never a solid bar) under a label that
// names the session, its heartbeat count and its duration. Aggregation stays
// strictly render-time: the emitter suppresses nothing, and the count is always
// shown, so heartbeats are never silently collapsed to nothing.
//
// Pure + exported for the render-model tests: takes a band's members (as
// annotated by `annotateRenderModel`) and the live clock (optional — only open
// members need it) and returns the band's scheduler model, or null when this
// isn't a scheduler band.
export function schedulerBandModel(members, nowMs) {
  let scheduler = false;
  let idleCount = 0;
  let workCount = 0;
  let spanCount = 0;
  let sessionId = null;
  let startMs = Infinity;
  let endMs = -Infinity;
  let open = false;
  for (const s of members) {
    spanCount += 1;
    if (!isSchedulerSpan(s)) continue;
    scheduler = true;
    if (sessionId == null) sessionId = s.sessionId;
    // The band's real time extent (#92) — the same min→max the layout gives any
    // other band, so the envelope the paint layer draws covers exactly the
    // 14:07→14:41 the operator is looking for.
    if (s.start < startMs) startMs = s.start;
    const e = nowMs != null ? effEnd(s, nowMs) : s.rEnd != null ? s.rEnd : s.end;
    if (Number.isFinite(e) && e > endMs) endMs = e;
    if (isOpen(s)) open = true;
    if (isIdleBeat(s)) {
      idleCount += 1;
      continue;
    }
    // A work tick: the terminal span of a tick that actually did something.
    // Markers, summaries and the session root itself are band chrome, not ticks;
    // an outcome-less TOOL span still counts, so a pre-#84 producer's ticks are
    // represented too.
    if (isMarker(s) || isSummary(s)) continue;
    if (s.kind === "TOOL") workCount += 1;
  }
  if (!scheduler) return null;
  if (!Number.isFinite(startMs)) startMs = null;
  if (!Number.isFinite(endMs) || (startMs != null && endMs < startMs)) endMs = startMs;
  const durationMs = startMs != null && endMs != null ? endMs - startMs : 0;
  const beats = `${idleCount} heartbeat${idleCount === 1 ? "" : "s"}`;
  const ticks = workCount ? ` · ${workCount} tick${workCount === 1 ? "" : "s"}` : "";
  const span = durationMs > 0 ? ` · ${fmtDuration(durationMs)}` : "";
  return {
    sessionId,
    virtual: true,
    // Draw the extent — but in the virtual style, which is what keeps it from
    // reading as a task (#92).
    envelope: true,
    startMs,
    endMs,
    durationMs,
    open,
    idleCount,
    workCount,
    tickCount: idleCount + workCount,
    spanCount, // every member still renders — nothing is dropped
    label: `scheduler · ${beats}${ticks}${span}`,
  };
}

// ── Zoom-adaptive heartbeat coalescing (#92) ──
//
// Heartbeats are zero-duration point spans arriving about once a minute, so at a
// wide zoom their beats would overdraw into an unreadable smear — but at a tight
// zoom each one is individually meaningful, and collapsing them unconditionally
// is what made a 29-minute band of 71 ticks look like one stub. Coalescing is
// therefore a PAINT decision taken against the CURRENT px/ms scale, never a fixed
// model-level collapse: two beats merge only when the pixels they paint would
// actually collide. Zoom in → every tick stands at its real time; zoom out → the
// run carries its COUNT, so nothing is ever silently dropped.
//
// Pure + exported for the render-model tests: `ticks` are the idle spans sharing
// one track and `pxPerMs` is the current scale. Returns the runs to paint, in
// time order, each with the beats it stands for.
export function heartbeatRuns(ticks, pxPerMs, beatPx = HEARTBEAT_PX) {
  const sorted = [...ticks].sort((a, b) => a.start - b.start);
  const runs = [];
  let cur = null;
  for (const s of sorted) {
    // The previous beat paints [x, x + beatPx]; this one collides only when it
    // lands inside that width at the current scale.
    if (cur && (s.start - cur.endMs) * pxPerMs <= beatPx) {
      cur.endMs = s.start;
      cur.count += 1;
      cur.coalesced = true;
      cur.spans.push(s);
      continue;
    }
    cur = { startMs: s.start, endMs: s.start, count: 1, coalesced: false, spans: [s] };
    runs.push(cur);
  }
  return runs;
}

// ── Member rollup: what a container span actually holds (#88) ──
//
// A session/turn root is a zero-duration POINT — its own geometry says nothing
// about its subtree — and the virtual `scheduler` pseudo-root is the extreme
// case (#87): the immortal band whose members are every heartbeat. Walk the same
// parent index the layout uses, counting members, their true time extent, and
// (for the scheduler) the heartbeat/work split, the features ticking under it
// and when each last ran. Feeds the shared detail model's "covers N spans over
// Xh" row, so hover, popover and the Navigator inspector all agree.
//
// Counting matches what the operator can SEE: `buildLayout` drops `rHide` spans
// (paired-away "(started)" markers, folded turn/session summaries) from every
// lane, so counting them here would report a 5-tool turn as ~11 spans — the same
// rule `schedulerBandModel` already applies to its tick counts. Run
// `annotateRenderModel` first; without it nothing is hidden and every span
// counts.
//
// Pure + exported for the render-model tests: `spans` is the Phoenix node id →
// span map and `childrenByParent` the parent OTel spanId → Set<node id> index,
// exactly as `mount` maintains them.
export function spanMemberRollup(s, spans, childrenByParent) {
  const kids = s.spanId ? childrenByParent.get(s.spanId) : null;
  if (!kids || !kids.size) return null;
  const scheduler = isSchedulerSpan(s);
  const stack = [...kids];
  const seen = new Set();
  const features = new Set();
  let count = 0;
  let firstMs = s.start;
  let lastMs = s.rEnd != null ? s.rEnd : s.end;
  let heartbeatCount = 0;
  let workCount = 0;
  let lastIdleMs = null;
  let lastWorkMs = null;
  while (stack.length) {
    const id = stack.pop();
    if (seen.has(id)) continue;
    seen.add(id);
    const c = spans.get(id);
    if (!c) continue;
    // Time extent covers the WHOLE subtree, hidden spans included: a folded
    // `turn <n> summary` never renders a bar but carries the turn's honest end.
    if (c.start < firstMs) firstMs = c.start;
    const cEnd = c.rEnd != null ? c.rEnd : c.end;
    if (cEnd > lastMs) lastMs = cEnd;
    for (const gid of childrenByParent.get(c.spanId) || []) stack.push(gid);
    if (c.rHide) continue; // render chrome, not a member
    count += 1;
    const feature = getAttr(c.attrs, ATTR_FEATURE_NAME);
    if (feature != null && feature !== "") features.add(String(feature));
    if (isIdleBeat(c)) {
      heartbeatCount += 1;
      if (lastIdleMs == null || c.start > lastIdleMs) lastIdleMs = c.start;
    } else if (c.kind === "TOOL" && !isMarker(c) && !isSummary(c)) {
      workCount += 1;
      if (lastWorkMs == null || c.start > lastWorkMs) lastWorkMs = c.start;
    }
  }
  if (!count) return null;
  return {
    count,
    startMs: firstMs,
    endMs: lastMs,
    virtual: scheduler,
    // Heartbeats/ticks identify the virtual scheduler session; elsewhere they
    // are noise, so they're reported only when the band IS one or actually has
    // beats.
    heartbeatCount: scheduler || heartbeatCount ? heartbeatCount : null,
    workCount: scheduler ? workCount : null,
    features: [...features],
    lastIdleMs: scheduler ? lastIdleMs : null,
    lastWorkMs: scheduler ? lastWorkMs : null,
  };
}

// ── Lane grouping: agent lanes, worker sub-lanes, orchestrator nesting (#101) ──
//
// A talon run launched BY an agent is fully attributed — `kestrel.orchestrator`
// carries the launching agent's name on every span of the run — but the layout
// bucketed project → agent → worker and sorted agents alphabetically, so Emma's
// talon run rendered as a sibling top-level `talon` lane BELOW Emma. Lanes are
// therefore keyed by the (agent, orchestrator) PAIR — the same deliberate key the
// retired fleet swimlane used — so one agent driven by two orchestrators is two
// lanes: `Emma/talon` nested under Emma, `claude-code/talon` top-level.
//
// Lane A nests under agent B iff ALL of:
//   1. `A.orchestrator === B` — the attribution names it.
//   2. B has its own lane in the SAME project. An orchestrator with no lane here
//      (`claude-code` in `kestrel-fleet`) leaves A top-level.
//   3. `A.agent !== B` — no self-nesting. `Claw` spans stamped
//      `orchestrator=Claw` stay one plain top-level `Claw` lane.
//   4. `A.orchestrator` is not the `Direct` sentinel — that means "no
//      orchestrator", never a parent.
//
// IDENTITY vs PLACEMENT are separate. The lane key is the (agent, orchestrator)
// pair after normalizing only the cases that genuinely mean "nobody launched
// this": no attribute, the `Direct` sentinel (rule 4), and self-orchestration
// (rule 3) all collapse to the agent's plain lane. Rule 2 decides PLACEMENT
// ONLY — an orchestrator with no lane in this project can't be nested under, so
// its lane stays top-level, but it keeps its own identity: `claude-code/talon`
// and `codex/talon` are two distinct top-level lanes, not one pooled `talon`.
// Erasing the orchestrator there would merge runs from unrelated launchers into
// a single band and make the lane a lie about what it holds.
//
// Levels: a top-level agent lane is 1 and its workers 2; an orchestrator-nested
// agent lane is 2 and its workers 3 (a deeper chain keeps counting — the label
// indent and muted style generalize).
//
// Pure + exported for the render-model tests. Run `annotateRenderModel` first:
// `rHide` chrome (paired markers, folded summaries) is dropped here exactly as
// the layout drops it, so it never invents a lane. Each bucketed span is stamped
// with its lane's orchestrator identity (`rLaneOrchestrator`) — with several
// `talon` lanes, (project, agent, worker) no longer identifies one row, so
// scroll-to-lane matches on the normalized value rather than the raw attribute.
export function laneGroups(spanIter) {
  // Pass 1: which agents actually have a lane in each project (rule 2).
  const byProject = new Map();
  for (const s of spanIter) {
    if (s.rHide) continue;
    let p = byProject.get(s.projectName);
    if (!p) {
      p = { agents: new Set(), spans: [] };
      byProject.set(s.projectName, p);
    }
    p.agents.add(s.agent);
    p.spans.push(s);
  }
  const out = new Map();
  for (const [name, p] of byProject) out.set(name, projectLanes(name, p.spans, p.agents));
  return out;
}

// The orchestrator half of a span's lane IDENTITY: the raw attribution, with
// only the "nobody launched this" cases normalized away to null (the agent's
// plain lane). Placement — whether that lane actually nests — is rule 2, decided
// per project in `projectLanes`; it never rewrites the identity.
function laneOrchestratorOf(s) {
  const orch = s.orchestrator;
  if (orch == null || orch === "") return null;
  if (orch === DIRECT_ORCHESTRATOR) return null; // rule 4 — "no orchestrator"
  if (orch === s.agent) return null; // rule 3 — no self-nesting
  return String(orch); // rule 1
}

function cmpGroups(a, b) {
  return (
    String(a.agent).localeCompare(String(b.agent)) ||
    String(a.orchestrator || "").localeCompare(String(b.orchestrator || ""))
  );
}

// One project's ordered lanes: each agent lane followed by its worker sub-lanes,
// then the agent lanes orchestrated BY it (each with their own workers).
function projectLanes(projectName, projectSpans, agents) {
  const groups = new Map(); // `<agent>\0<laneOrchestrator>` → agent lane + workers
  const groupFor = (agent, orch) => {
    const key = `${agent}\u0000${orch || ""}`;
    let g = groups.get(key);
    if (!g) {
      g = { key, agent, orchestrator: orch, items: [], workers: new Map() };
      groups.set(key, g);
    }
    return g;
  };
  for (const s of projectSpans) {
    const orch = laneOrchestratorOf(s);
    s.rLaneOrchestrator = orch;
    // Touch the agent's own band even for a worker-only span, so the agent keeps
    // its (possibly empty) row exactly as the pre-nesting layout gave it.
    const g = groupFor(s.agent, orch);
    const wk = s.worker || null;
    if (wk == null) {
      g.items.push({ span: s });
    } else {
      let list = g.workers.get(wk);
      if (!list) {
        list = [];
        g.workers.set(wk, list);
      }
      list.push({ span: s });
    }
  }

  // An agent can hold several lanes (one per orchestrator); a child nests under
  // that agent's plain lane when there is one, else its first lane.
  const byAgent = new Map();
  for (const g of groups.values()) {
    let arr = byAgent.get(g.agent);
    if (!arr) {
      arr = [];
      byAgent.set(g.agent, arr);
    }
    arr.push(g);
  }
  const parentOfAgent = new Map();
  for (const [agent, arr] of byAgent) {
    const sorted = arr.slice().sort(cmpGroups);
    parentOfAgent.set(agent, sorted.find((g) => g.orchestrator == null) || sorted[0]);
  }
  // Placement (rule 2): a lane nests only under an orchestrator that HAS a lane
  // in this project. One that doesn't keeps its identity and stays top-level.
  const children = new Map(); // parent group key → nested agent groups
  const nested = new Set(); // group keys that are somebody's child
  for (const g of groups.values()) {
    if (g.orchestrator == null) continue;
    if (!agents.has(g.orchestrator)) continue; // rule 2 — no lane here to nest under
    const parent = parentOfAgent.get(g.orchestrator);
    if (!parent || parent === g) continue;
    let arr = children.get(parent.key);
    if (!arr) {
      arr = [];
      children.set(parent.key, arr);
    }
    arr.push(g);
    nested.add(g.key);
  }

  const lanes = [];
  const emitted = new Set();
  const emit = (g, level) => {
    if (emitted.has(g.key)) return; // also the cycle guard (A→B→A)
    emitted.add(g.key);
    lanes.push({
      projectName,
      agent: g.agent,
      orchestrator: g.orchestrator,
      worker: null,
      label: g.orchestrator ? `${g.orchestrator}/${g.agent}` : g.agent,
      level,
      items: g.items,
    });
    for (const wk of [...g.workers.keys()].sort()) {
      lanes.push({
        projectName,
        agent: g.agent,
        orchestrator: g.orchestrator,
        worker: wk,
        // The worker segment IS a stage token (`workerOf`: `kestrel.stage`, else
        // the `talon/review` agent-name suffix), so it reads as prose (#104);
        // the agent segment is a name and is never re-cased. Display only — the
        // lane's `worker` stays the raw value scroll-to-lane matches on.
        label: `${g.agent}/${stageTitle(wk)}`,
        level: level + 1,
        items: g.workers.get(wk),
      });
    }
    for (const child of (children.get(g.key) || []).slice().sort(cmpGroups)) {
      emit(child, level + 1);
    }
  };
  for (const g of [...groups.values()].filter((g) => !nested.has(g.key)).sort(cmpGroups)) {
    emit(g, 1);
  }
  // A nested group no top-level lane ever reached (mutual orchestration) must
  // still render — never drop a lane on the floor.
  for (const g of [...groups.values()].sort(cmpGroups)) emit(g, 1);
  return lanes;
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

// ── Wheel intent: scroll pans, only a modifier zooms (#94) ──
//
// A Magic Mouse (and any trackpad) is a touch surface with no detented wheel, so
// an ordinary one-finger drag emits `wheel` deltas — mapping vertical wheel to
// zoom made plain scrolling rescale the window unexpectedly. Scroll therefore
// PANS on both axes (deltaY through the lanes, deltaX through time, diagonal
// doing both in one event) and zoom becomes explicit: ctrl+wheel — which is also
// how a trackpad pinch arrives — or ⌘+wheel, plus the +/- buttons and the
// density-strip click that were always the primary path.
//
// Pure + exported so the interaction contract is unit-testable: takes the wheel
// event's fields, returns either a zoom factor or the two pan deltas — never
// both.
export function wheelIntent({ deltaX = 0, deltaY = 0, ctrlKey = false, metaKey = false } = {}) {
  if (ctrlKey || metaKey) {
    // Vertical drives the factor; a purely horizontal modifier-scroll still
    // zooms rather than falling through to a pan the modifier didn't ask for.
    const d = deltaY !== 0 ? deltaY : deltaX;
    if (d !== 0) return { zoom: d > 0 ? WHEEL_ZOOM_STEP : 1 / WHEEL_ZOOM_STEP };
  }
  return { panTimeDx: deltaX, scrollLanesDy: deltaY };
}

// ── Magnetic present edge (#94) ──
//
// Wheel-pan already snapped to `now` and resumed Live on overshoot while
// drag-pan clamped at `now` and stayed paused — the same gesture, two answers.
// Both now run their candidate right edge through this: land within `snapMs` of
// the present and the view sticks to it with Live back on; stay left of that and
// follow stays paused. Only an actual pan gesture calls it, so a static
// paused-at-the-edge view (and the persisted-state restore path) never
// self-resumes.
//
// Pure + exported for the interaction tests.
export function magneticViewEnd(candidateViewEnd, nowMs, snapMs) {
  const snap = Number.isFinite(snapMs) && snapMs > 0 ? snapMs : 0;
  if (candidateViewEnd >= nowMs - snap) return { viewEnd: nowMs, live: true };
  return { viewEnd: candidateViewEnd, live: false };
}

// ── Live poll walk boundary (#109) ──
//
// Where a project's next live walk starts. Two rules, both learned the hard way:
//
//   - RESUME BEATS RECOMPUTE. A walk is bounded by `MAX_POLL_PAGES`, so a deep
//     backlog does not drain in one tick. An unfinished walk keeps the exact
//     bounds it started with and the cursor it stopped on; recomputing a start
//     for it would restart the walk at a boundary that moved while its own range
//     is still un-ingested.
//   - THE BOUNDARY IS INCLUSIVE. A fresh walk starts AT the watermark, never one
//     millisecond past it: `+1` skips every span sharing the watermark's
//     millisecond, and ties are routine (a burst flush emits several summaries in
//     the same instant). If a truncated walk stopped mid-millisecond, that `+1`
//     dropped the rest of that millisecond permanently — a parent and child
//     emitted together could be split across the page bound with the parent gone
//     for good. Re-pulling the boundary millisecond is idempotent: `mergeSpans`
//     keys on span id.
//
// Pure + exported so the boundary rules are unit-testable without a mount.
export function pollWalkBounds({
  pending = null,
  watermark = null,
  openFloor = null,
  viewStart = null,
} = {}) {
  if (pending) {
    return {
      startMs: pending.startMs,
      endMs: pending.endMs != null ? pending.endMs : null,
      after: pending.after || null,
      resumed: true,
    };
  }
  if (watermark == null) return { startMs: viewStart, endMs: null, after: null, resumed: false };
  let startMs = watermark;
  // Back the cursor down to this project's earliest still-open span so the poll
  // re-fetches BACKDATED closes/summaries/twins (their start ≤ that open
  // anchor's start, ≤ the watermark) that a forward-only walk skips — else live
  // turns stay open and markers unpaired until reload (#62 P1).
  if (openFloor != null && openFloor < startMs) startMs = openFloor;
  return { startMs, endMs: null, after: null, resumed: false };
}

// ── View / mount ──────────────────────────────────────────────

export function mount(container, opts = {}) {
  ensureStyles();

  const openTrace = typeof opts.openTrace === "function" ? opts.openTrace : null;
  const openNavigator = typeof opts.openNavigator === "function" ? opts.openNavigator : null;
  const stopController = opts.stopController || null;
  const revealTarget = opts.revealTarget || null;

  let destroyed = false;
  let booted = false; // boot() finished wiring the poll timer + live loop

  // ── Time-window state ──
  let windowMs = DEFAULT_WINDOW_MS;
  let viewEnd = Date.now(); // right edge (ms); tracks wall-clock while live
  let live = !revealTarget; // cross-view reveal opens paused around its timestamp
  let laneScrollY = 0; // vertical lane scroll offset
  let highlightedSpanId =
    revealTarget && revealTarget.spanId != null ? String(revealTarget.spanId) : null;
  const revealStart = Number(revealTarget && revealTarget.startTime);
  if (revealTarget && Number.isFinite(revealStart)) {
    windowMs = MIN_WINDOW_MS;
    viewEnd = revealStart + windowMs / 2;
  }

  const viewStart = () => viewEnd - windowMs;

  // ── Data ──
  const spans = new Map(); // Phoenix node id → normalized span
  // Incremental parent-link indexes, maintained on every merge/prune so the
  // layout can rebuild the span tree cheaply and tolerate orphans (children
  // paged before parents; talon leaves exported before their held-open roots) —
  // an orphan renders at its best-known depth and re-parents when the parent
  // arrives on a later rebuild (#54.2). Eviction no longer makes orphans: it
  // takes whole sessions, never a parent out from under its children (#111).
  const spanIdToId = new Map(); // OTel context.spanId → Phoenix node id
  const childrenByParent = new Map(); // parent OTel spanId → Set<Phoenix node id>
  const projects = []; // [{id, name}] — DEFAULT_PROJECT first
  // COVERAGE, not progress: a range is recorded here only once the walk over it
  // ran out of pages (`hasNextPage: false`). A walk still in flight has ingested
  // spans without covering its range, and `historyFloor` in particular is what
  // later decides a range is already loaded (#109).
  const watermarks = new Map(); // projectId → newest startTime ms COVERED (live)
  const historyFloor = new Map(); // projectId → oldest startTime ms COVERED
  const openFloors = new Map(); // projectId → earliest still-open span start (live re-fetch floor, #62 P1)
  const projectFetching = new Set(); // projectId → history fetch in flight
  // THE registry of unfinished paged walks — one map, keyed by (project,
  // purpose), held across ticks AND across errors so the next pass resumes on
  // the cursor + bounds it stopped on instead of restarting from a recomputed
  // boundary (#109). Every walk site goes through it: a site that keeps its own
  // state map is a site whose truncated walk is never resumed by anything.
  const walks = new Map(); // `${purpose} ${projectId}` → pending walk
  let revealPending = Boolean(revealTarget); // reveal owes a real outcome

  // ── Layout cache (rebuilt on data / collapse change, projected each frame) ──
  const collapsed = new Set(); // collapsed project names
  let layout = { rows: [], contentH: 0 };
  let drawn = []; // {x,y,w,h,span?,density?,count} for hit-testing (per frame)
  const rollupCache = new Map(); // spanId → memberRollup (invalidated by buildLayout)
  const turnCompletionCache = new Map(); // project+trace → authoritative focused read
  const turnCompletionLoads = new Map(); // same key → one in-flight GraphQL read
  const turnCompletionAborts = new Map(); // same key → owned trace-walk abort
  let completedTurnKeys = new Set(); // rebuilt once with the render model/layout

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
        <div class="obs-tl__reveal" data-reveal-notice hidden></div>
      </div>
    </div>`;

  const bodyEl = container.querySelector("[data-body]");
  const canvas = container.querySelector("[data-canvas]");
  const tipEl = container.querySelector("[data-tip]");
  const popEl = container.querySelector("[data-pop]");
  const revealNoticeEl = container.querySelector("[data-reveal-notice]");
  const liveBtn = container.querySelector("[data-live]");
  const windowEl = container.querySelector("[data-window]");
  const ctx = canvas.getContext("2d");
  let activePopoverSpan = null;
  const stopUnsubscribe = stopController
    ? stopController.subscribe(() => syncPopoverStopActions())
    : () => {};

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
    const orch = getAttr(attrs, ATTR_ORCHESTRATOR);
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
      // Who LAUNCHED this run (talon attribution). Drives lane nesting (#101).
      orchestrator: orch != null && orch !== "" ? String(orch) : null,
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

  // Ingest one page. Merging is idempotent — a span already held is replaced in
  // place and not counted as `added` — so re-pulling an overlapping range (the
  // boundary millisecond of a live walk, a resumed page) costs nothing but the
  // request. Coverage is NOT recorded here: a page says what was ingested, only
  // a finished walk says what range is covered (see `commitWalkCoverage`, #109).
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
    return { added, newestStart, oldestStart };
  }

  // ── Memory guard: evict whole CLOSED SESSIONS, never individual spans (#111) ──
  //
  // The session is the self-contained unit — session root/marker, turn roots,
  // their summaries, marker twins, tool calls — and its members are exactly what
  // `sessionKeyFor` groups (the stamped session id, else the trace, the same
  // fallback `laneBands` bands by). Nothing smaller is safe to drop on its own,
  // because the render model is a pure function of the loaded set: a turn root
  // separated from its `turn <n> summary` re-annotates as STILL RUNNING (and drags
  // the live re-fetch floors with it), a "(started)" marker separated from its
  // twin repaints as a phantom open band. So the unit goes whole or stays whole.
  //
  // This replaces span-level eviction ordered by the raw `end` — the note that
  // stood here ("dropping a parent while keeping its children is fine — the
  // orphans fall back to depth-0 roots until (if) the parent is re-fetched") is
  // the opposite intent and must not come back. It was wrong twice over:
  // `normalize()` gives an OPEN span `end = start`, so a live talon run root or
  // an in-flight turn sorted to the very front and was evicted BEFORE its own
  // finished children — destroying exactly the band an operator is watching —
  // and individual eviction shredded the units above.
  //
  // The policy deliberately has NO memory: no tombstones, no suppression table,
  // nothing keyed on span names. Spans of an evicted session that are fetched
  // again merge back as an ordinary session — that is correct behaviour, not a
  // leak to suppress.
  //
  //   - a session holding ANY open span is never a candidate (that is live work);
  //   - candidates are ordered oldest-first by the session's latest end;
  //   - whole sessions are evicted until the store is back under the cap.
  //
  // Runs at the end of a COMPLETED poll cycle (#109) and after
  // `annotateRenderModel` has resolved, so membership, summaries and twins are
  // known — never per page inside `mergeSpans`, where the parent chain is still
  // half-loaded and no twin is paired yet.

  // Is a span still open? The resolved render model answers it (`rOpen` — an
  // abandoned run is closed, a summary-less live turn is open); a span the model
  // has not seen falls back to its raw open-endedness.
  function spanStillOpen(s) {
    if (s.rOpen != null) return s.rOpen === true;
    return s.openEnded === true;
  }

  // Group the store into eviction units: session key → members, latest end, and
  // whether any member is still open.
  function sessionUnits() {
    const units = new Map();
    for (const s of spans.values()) {
      const key = sessionKeyFor(s);
      let unit = units.get(key);
      if (!unit) {
        unit = { key, members: [], latestEnd: -Infinity, open: false };
        units.set(key, unit);
      }
      unit.members.push(s);
      const end = Math.max(s.start, s.rEnd != null ? s.rEnd : s.end);
      if (end > unit.latestEnd) unit.latestEnd = end;
      if (spanStillOpen(s)) unit.open = true;
    }
    return units;
  }

  // The cap decisions below repeat every cycle for as long as they hold, so only
  // a CHANGE of decision is logged — an explicit record, not a per-poll stream.
  let capDecision = null;
  function noteCapDecision(message) {
    if (message === capDecision) return;
    capDecision = message;
    if (typeof console !== "undefined" && console.warn) console.warn(message);
  }

  // Returns whether anything was evicted (the caller rebuilds the layout).
  function pruneSpans() {
    if (spans.size <= SPAN_CAP) {
      capDecision = null;
      return false;
    }
    const units = [...sessionUnits().values()];
    // A session bigger than the whole cap can never be retained AND under it, and
    // it cannot be split — dropping it would trade the operator's entire view for
    // a byte count. It is not a candidate; the cap gives way instead (below).
    const candidates = units
      .filter((u) => !u.open && u.members.length <= SPAN_CAP)
      .sort((a, b) => a.latestEnd - b.latestEnd);
    let evicted = 0;
    for (const unit of candidates) {
      if (spans.size <= SPAN_CAP) break;
      for (const s of unit.members) {
        deindexSpan(s);
        spans.delete(s.id);
      }
      evicted += 1;
    }
    if (spans.size > SPAN_CAP) {
      // The explicit branch: every session is live, or one is bigger than the cap.
      // Exceed the cap for this cycle and say why — splitting a unit to satisfy a
      // span count is the behaviour being removed, so there is no other fallback.
      // Re-decided next cycle, when a session may have closed, grown or been
      // evicted. The message is a function of that state alone, so an unchanged
      // decision is logged once rather than every poll.
      const oversized = units.filter((u) => u.members.length > SPAN_CAP).length;
      const live = units.filter((u) => u.open).length;
      noteCapDecision(
        `kestrel timeline: ${spans.size} spans over the ${SPAN_CAP} cap — ` +
          `${live} of ${units.length - evicted} session(s) live, ${oversized} larger than the cap. ` +
          `Exceeding the cap this cycle rather than splitting a session ` +
          `(spans are never evicted individually).`,
      );
    } else {
      capDecision = null;
    }
    return evicted > 0;
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

  // ── Resumable paged walks (#109) ──

  // A walk over a FIXED range: its bounds never move once it starts, so its
  // cursor stays meaningful across ticks and across a failed page.
  function newWalk({ startMs = null, endMs = null, after = null } = {}) {
    return { startMs, endMs, after, newestStart: null, oldestStart: null };
  }

  // A walk is identified by the project it pages and WHY it is paging it, so the
  // live drain, a history gap and a reveal window can each be owed at once on the
  // same project without overwriting one another's cursor.
  const walkKey = (purpose, projectId) => `${purpose} ${projectId}`;
  const pendingWalk = (purpose, projectId) => walks.get(walkKey(purpose, projectId)) || null;
  const dropWalk = (purpose, projectId) => walks.delete(walkKey(purpose, projectId));
  function walkOwed(purpose) {
    for (const key of walks.keys()) {
      if (key.startsWith(`${purpose} `)) return true;
    }
    return false;
  }

  // A finished walk (and only a finished walk) is what makes its range covered.
  function commitWalkCoverage(projectId, walk) {
    if (walk.newestStart != null) {
      const prev = watermarks.get(projectId);
      if (prev == null || walk.newestStart > prev) watermarks.set(projectId, walk.newestStart);
    }
    if (walk.oldestStart != null) {
      const prev = historyFloor.get(projectId);
      if (prev == null || walk.oldestStart < prev) historyFloor.set(projectId, walk.oldestStart);
    }
  }

  // Drain up to MAX_POLL_PAGES of `walk`, merging each page.
  //
  // The walk is registered under (project, purpose) up front and its cursor
  // advanced after every SUCCESSFUL page, so a throw mid-walk leaves it exactly
  // where the last good page ended — the next pass resumes rather than restarts.
  // It leaves the registry only when Phoenix reports `hasNextPage: false`, which
  // is also the only moment its range becomes covered; a walk stopped by the page
  // cap (or by `stopEarly`) keeps its range explicitly incomplete.
  async function drainWalk(walk, projectId, projectName, purpose, stopEarly) {
    walks.set(walkKey(purpose, projectId), walk);
    let added = 0;
    let done = false;
    for (let page = 0; page < MAX_POLL_PAGES; page++) {
      const { raw, hasNext, cursor } = await fetchSpanPage(projectId, {
        startMs: walk.startMs,
        endMs: walk.endMs,
        after: walk.after,
      });
      const merged = mergeSpans(raw, projectId, projectName);
      added += merged.added;
      if (
        merged.newestStart != null &&
        (walk.newestStart == null || merged.newestStart > walk.newestStart)
      ) {
        walk.newestStart = merged.newestStart;
      }
      if (
        merged.oldestStart != null &&
        (walk.oldestStart == null || merged.oldestStart < walk.oldestStart)
      ) {
        walk.oldestStart = merged.oldestStart;
      }
      if (!hasNext || !cursor) {
        done = true;
        break;
      }
      walk.after = cursor;
      if (stopEarly && stopEarly(raw)) break;
    }
    if (done) {
      dropWalk(purpose, projectId);
      commitWalkCoverage(projectId, walk);
    }
    return { added, done };
  }

  function revealProject() {
    if (!revealTarget) return null;
    return (
      projects.find(
        (p) =>
          (revealTarget.projectId != null && p.id === revealTarget.projectId) ||
          (revealTarget.projectName != null && p.name === revealTarget.projectName),
      ) || null
    );
  }

  // The reveal target, if it has been ingested — the same lookup `finishReveal`
  // reports on, so "keep paging" and "what the notice says" can never disagree.
  function revealHit() {
    if (!revealTarget) return null;
    if (revealTarget.spanId != null) {
      const nodeId = spanIdToId.get(String(revealTarget.spanId));
      return nodeId != null ? spans.get(nodeId) || null : null;
    }
    if (revealTarget.nodeId != null) return spans.get(String(revealTarget.nodeId)) || null;
    return null;
  }

  // A Navigator round-trip may target history far outside the normal live
  // window. Load a bounded, timestamp-centered slice from the exact project
  // instead of walking every span from that time through "now".
  //
  // It stops the moment the exact span lands, which — like the page cap — leaves
  // the rest of the window un-ingested, so this walk covers nothing unless it
  // genuinely runs out of pages; the live poll re-walks whatever it skipped.
  //
  // A target past the page cap does NOT settle the reveal: the walk is persisted
  // like any other and resumed by the poll timer until the span lands or the
  // window is genuinely drained, because a reveal opens the view PAUSED and
  // nothing else would ever finish it. Reporting "could not be loaded" off the
  // first truncated pass is a lie about a span that is right there on page 7
  // (#109).
  async function loadRevealWindow() {
    if (!revealPending) return;
    const project = revealProject();
    if (!project) {
      revealPending = false; // no such project — an outcome, just not a happy one
      return;
    }
    const walk =
      pendingWalk(WALK_REVEAL, project.id) || newWalk({ startMs: viewStart(), endMs: viewEnd });
    const { done } = await drainWalk(walk, project.id, project.name, WALK_REVEAL, () => {
      return revealHit() != null;
    });
    // Settled either way it can be: the span landed, or the window is drained
    // and it genuinely is not there. A walk stopped short of both is still owed.
    if (revealHit() != null || done) {
      revealPending = false;
      dropWalk(WALK_REVEAL, project.id);
    }
  }

  // Resume the reveal walk from the poll timer and settle the view the moment it
  // reaches an outcome: the layout has to be built before `finishReveal` (it
  // reads `rHide` and the lane rows), and rebuilt after (it may un-collapse a
  // project and re-anchor the window) — which the caller's own repaint does.
  async function continueReveal() {
    await loadRevealWindow();
    if (destroyed || revealPending) return;
    buildLayout();
    finishReveal();
    // A reveal that settles on a later tick re-anchors the window on its target:
    // a viewport change like any other, landing on history whose run/turn roots
    // were never inside any window this view has asked for. Its walk is done by
    // definition here (`revealPending` is false), but the obligation is still
    // spent through the same gate — the live walk this tick resumed may not be,
    // and one obligation shared with boot's is one resolve, not two (#108).
    armAncestorResolve();
  }

  function showRevealNotice(message, isFallback) {
    if (!revealNoticeEl) return;
    revealNoticeEl.textContent = message;
    revealNoticeEl.classList.toggle("obs-tl__reveal--fallback", Boolean(isFallback));
    revealNoticeEl.hidden = false;
  }

  function finishReveal() {
    if (!revealTarget) return;
    const hit = revealHit();
    if (!hit) {
      highlightedSpanId = null;
      showRevealNotice(
        `Exact span ${revealTarget.spanId || revealTarget.nodeId || ""} could not be loaded; no other span was highlighted.`,
        true,
      );
      return;
    }
    if (hit.rHide) {
      highlightedSpanId = null;
      showRevealNotice(
        `Exact span ${hit.spanId || hit.id} is folded into its owning Timeline band; no other span was highlighted.`,
        true,
      );
      return;
    }

    highlightedSpanId = hit.spanId;
    viewEnd = hit.start + windowMs / 2;
    collapsed.delete(hit.projectName);
    // (project, agent, worker) stopped identifying ONE row once an agent can hold
    // a lane per orchestrator (#101) — an orchestrated `talon` lane and the
    // plain one share that triple. Match the lane identity `laneGroups` stamped
    // on this very span, not its raw attribute (`Direct`/self-orchestration
    // normalize to null, i.e. the agent's plain lane).
    const lane = layout.rows.find(
      (row) =>
        row.type === "lane" &&
        row.projectName === hit.projectName &&
        row.agent === hit.agent &&
        (row.orchestrator || null) === (hit.rLaneOrchestrator || null) &&
        (row.worker || null) === (hit.worker || null),
    );
    if (lane) {
      laneScrollY = Math.max(0, lane.y - Math.max(0, cssH - lane.h) / 2);
      clampScroll();
    }
    showRevealNotice(`Exact span ${hit.spanId || hit.id} highlighted.`, false);
  }

  // Live/initial poll: pull everything since the project's covered watermark (or
  // the visible window's start on the first pass), draining backlog up to a cap.
  // A backlog deeper than that cap continues on the next tick from this walk's
  // own cursor — never from a recomputed boundary (#109).
  async function pollProject(projectId, projectName) {
    const pending = pendingWalk(WALK_LIVE, projectId);
    const bounds = pollWalkBounds({
      pending,
      watermark: watermarks.get(projectId),
      openFloor: openFloors.get(projectId),
      viewStart: viewStart(),
    });
    const walk = bounds.resumed ? pending : newWalk(bounds);
    const { added } = await drainWalk(walk, projectId, projectName, WALK_LIVE);
    return added;
  }

  // ── Ancestor backfill: fetch the parents no window can see (#108) ──
  //
  // Runs ON DEMAND — once the initial load settles, and debounced when the
  // viewport settles after a pan/zoom, which is precisely when the operator is
  // looking at history whose ancestors were never fetched. Each of those OWES one
  // resolve and none of them spends it: the gesture is not the settle, so the
  // obligation waits for the ingestion to go quiet (`settleAncestorResolve`, the
  // gate #111 prunes on). There is deliberately
  // NO periodic resolver: a five-second retry loop is what turned #105 into
  // request storms, per-tick budgets, id rotation and live-poll starvation. A
  // given orphan set is resolved at most once per settle, and a parent that
  // genuinely does not come back (never exported, aged out of Phoenix) is simply
  // left orphaned — the render model already tolerates that, drawing an orphan at
  // its best-known depth, and chasing it with retry/backoff bookkeeping is the
  // failure mode of the two previous attempts, not a fix.
  //
  // Nothing here has to interact with the memory guard: eviction takes whole
  // CLOSED sessions (#111), so an ancestor inside a retained session cannot be
  // dropped out from under the child that needed it, and there are no tombstones
  // or suppression tables for a re-fetch to trip over.
  //
  // The one place a backfilled span reaches back into the poll is `openFloors`: a
  // pre-window ancestor that is still OPEN anchors the live re-fetch floor at its
  // start, which can be far behind the watermark. That is the #62 floor working as
  // designed and it no longer starves live-follow — since #109 a walk truncated by
  // the page cap resumes on its own cursor instead of restarting at the floor, so
  // the drain reaches the live edge and commits — and a run that is merely dead
  // stops anchoring the floor once the abandoned cap fires (#67).

  // Aborts whatever is on the wire at teardown, so a sub-tab switch cannot leave
  // a resolve running against a detached mount.
  const ancestorAbort = typeof AbortController === "function" ? new AbortController() : null;

  // Spans this run's exact merges ADDED, counted AT each merge rather than as a
  // net `spans.size` delta: it survives a hop that throws (whatever already
  // landed still repaints) and cannot be confused by an eviction that happens to
  // offset the insert.
  let ancestorMerged = 0;

  // Fetch exactly these spans by OTel span id — no `timeRange`, which is the
  // whole point: an ancestor starts BEFORE the window its children sit in, so no
  // windowed page can ever see it. Ids Phoenix does not have come back empty.
  async function fetchAncestors(projectId, spanIds) {
    if (destroyed) return;
    const filter = spanIdFilter(spanIds);
    if (!filter) return;
    const project = projects.find((p) => p.id === projectId);
    if (!project) return; // no project node to query against
    const data = await gql(
      SPAN_PAGE_QUERY,
      {
        projectId,
        first: Math.min(PAGE_SIZE, ANCESTOR_BATCH),
        after: null,
        filter,
        rootOnly: false,
        sort: null,
        timeRange: null,
      },
      { signal: ancestorAbort ? ancestorAbort.signal : undefined },
    );
    // Torn down while this was in flight: the response belongs to a mount nothing
    // will draw again, so drop it rather than merge into a detached store (an
    // aborted fetch usually throws first; one that had already landed does not).
    if (destroyed) return;
    const conn = data.node && data.node.spans;
    const raw = ((conn && conn.edges) || []).map((e) => e && e.node).filter(Boolean);
    // Exact ids cover no time RANGE, so this merge moves no cursor — coverage is
    // a finished walk's claim alone (#109), which is exactly right here.
    ancestorMerged += mergeSpans(raw, projectId, project.name).added;
  }

  // ONE run: hop up the chain until every orphan's parent is loaded (or does not
  // exist). The chain is at LEAST two hops — a tool's parent is its turn, the
  // turn's parent is the run root — so stopping after one still loses the run
  // band; each pass re-reads the store, so the parents fetched by one pass are
  // what the next asks about.
  //
  // Depth is a property of the ID (`ancestorFrontier` stamps it, `asked`/`depths`
  // remember it), never of the pass: a frontier wider than `ANCESTOR_PASS_IDS` is
  // drained across as many passes as it takes and every carried id keeps the depth
  // it was discovered at. A per-pass hop counter would let BREADTH buy DEPTH — 400
  // orphan chains spend a whole pass per generation, each pass starts over with a
  // fresh budget, and the run walks the chain arbitrarily far, which is the
  // unbounded request run the cap exists to prevent.
  //
  // Bounded four ways: `ANCESTOR_HOPS` caps how far from a loaded span any id may
  // be, `ANCESTOR_BATCH` the ids per request, `ANCESTOR_PASS_IDS` the ids asked
  // before the frontier is re-read, and `asked` — an id is requested at most once
  // per run, so a parent CYCLE terminates instead of spinning, and a pass with
  // nothing new to ask ends the run.
  //
  // `asked` is per RUN and remembered nowhere afterwards: a table of "already
  // resolved" is exactly what leaves a later-discovered orphan orphaned.
  async function ancestorRun() {
    ancestorMerged = 0;
    try {
      const asked = new Set(); // ask keys this run has already requested
      const depths = new Map(); // ask key → the hop count it was asked at
      // The carried surplus of a pass is the NEXT pass's frontier, so a wide
      // generation is finished before the one above it is even looked at (#105
      // truncated at its first 800 ids and never came back for the rest).
      let frontier = ancestorFrontier(spans.values(), depths, ANCESTOR_HOPS);
      while (frontier.length && !destroyed) {
        const plan = ancestorRequestPlan(frontier, asked, {
          budget: ANCESTOR_PASS_IDS,
          batchSize: ANCESTOR_BATCH,
        });
        let askedThisPass = 0;
        for (const req of plan.requests) {
          if (destroyed) break;
          for (const id of req.ids) {
            const key = ancestorAskKey(req.projectId, id);
            asked.add(key);
            depths.set(key, req.depth); // what the ids it reveals are measured from
          }
          askedThisPass += req.ids.length;
          await fetchAncestors(req.projectId, req.ids);
        }
        // Nothing left to ask: every outstanding parent is either already asked
        // for in this run (a cycle, a shared ancestor) or past the hop cap.
        if (!askedThisPass) break;
        frontier = plan.carried.length
          ? plan.carried
          : ancestorFrontier(spans.values(), depths, ANCESTOR_HOPS);
      }
    } catch (_e) {
      /* transient (or aborted at teardown) — the next settle asks again */
    }
    // Ancestors landing must show the band WITHOUT a remount — and in a paused or
    // panned view nothing else would ever rebuild: `pollTick(false)` starts no
    // walk there, so this is the only repaint the band gets.
    if (ancestorMerged > 0 && !destroyed) {
      buildLayout();
      requestDraw();
    }
    return ancestorMerged;
  }

  let resolvingAncestors = false;
  let ancestorResolveQueued = false;

  // One resolve at a time. A settle landing mid-run folds into a single follow-up
  // run rather than being dropped — the view the operator stopped on is the one
  // whose ancestors have to be resolved — and rather than overlapping it, which
  // would ask for the same ids twice from two `asked` sets.
  async function resolveAncestors() {
    if (destroyed) return 0;
    if (resolvingAncestors) {
      ancestorResolveQueued = true;
      return 0;
    }
    resolvingAncestors = true;
    let merged = 0;
    try {
      do {
        ancestorResolveQueued = false;
        merged += await ancestorRun();
      } while (ancestorResolveQueued && !destroyed);
    } catch (_e) {
      /* every caller is fire-and-forget (boot, the debounce), so a failed
         repaint must not surface as an unhandled rejection */
    } finally {
      resolvingAncestors = false;
    }
    return merged;
  }

  // ONE outstanding obligation to resolve, armed by the things that expose
  // missing ancestors — the initial load, a settled viewport gesture, a reveal —
  // and never by anything periodic. Arming twice before it is spent is still one
  // resolve.
  let ancestorResolveOwed = false;

  function armAncestorResolve() {
    if (destroyed) return;
    ancestorResolveOwed = true;
    settleAncestorResolve();
  }

  // …and SPENT only on a settled ingestion — the same gate, for the same reason,
  // that #111 prunes on. An unsettled walk means "the parent has not been fetched
  // YET", not "this span is an orphan": every paged walk is capped
  // (MAX_POLL_PAGES), so a deep fill hands back a store that is only half loaded,
  // and the initial load is the sharpest case of all because almost nothing above
  // the window has arrived. Resolving there asks Phoenix for spans already on
  // their way, and asks about page six's orphans while page seven's — merged in
  // later by a tick that schedules nothing — are never asked about at all. So the
  // obligation waits for `ingestionSettled()` and is spent by whichever path
  // finishes last (#108).
  function settleAncestorResolve() {
    if (destroyed || !ancestorResolveOwed || !ingestionSettled()) return;
    ancestorResolveOwed = false;
    resolveAncestors();
  }

  // Pan/zoom arrive as a burst of events, so the obligation is armed once the
  // gesture stops. Debounced, never periodic: no timer re-arms itself here.
  let ancestorTimer = null;
  function scheduleAncestorResolve() {
    if (destroyed) return;
    if (ancestorTimer) clearTimeout(ancestorTimer);
    ancestorTimer = setTimeout(() => {
      ancestorTimer = null;
      armAncestorResolve();
    }, ANCESTOR_SETTLE_MS);
  }

  // THE viewport-gesture commit point: pull the history the new window exposes,
  // and owe a resolve for whatever that turns out to leave orphaned — spent once
  // those pages have actually landed, not while they are still being walked.
  function viewportChanged() {
    loadHistory();
    scheduleAncestorResolve();
  }

  let polling = false;
  async function pollTick(manual) {
    if (destroyed || polling || (!manual && document.hidden)) return;
    if (historyPassOwed()) loadHistory();
    // Paused, the timer starts no NEW live walk — but one already truncated
    // mid-backlog (a boot-time fill deeper than MAX_POLL_PAGES, a pause that
    // landed mid-drain) still owes the range it claimed no coverage for, and
    // only its own cursor can finish it. Those resume; nothing else does. So
    // does an unsettled reveal, which is paused BY DEFINITION (#109).
    const resumeOnly = !manual && !live;
    if (resumeOnly && !revealPending && !walkOwed(WALK_LIVE)) {
      capPausedStore(); // a paused view still ingests history — see below
      // A paused view ingests down other paths (a history walk resumed above), so
      // an owed resolve is spent here too — this early return is the only thing a
      // paused tick runs, and it is exactly the mode this bug is hit in (#108).
      settleAncestorResolve();
      return;
    }
    polling = true;
    let added = 0;
    try {
      if (revealPending) await continueReveal();
      for (const p of projects) {
        if (destroyed) break;
        // Re-read `live` per project rather than once per tick: a pause landing
        // mid-tick stops fresh walks from here on, while a pending walk keeps
        // draining.
        if (!manual && !live && !pendingWalk(WALK_LIVE, p.id)) continue;
        added += await pollProject(p.id, p.name);
      }
    } catch (_e) {
      /* transient poll errors are non-fatal — next tick retries */
    } finally {
      polling = false;
    }
    // Rebuild every tick, not only when new span IDs arrive: abandonment is
    // purely time-based (a still-open span crosses STALE_MARKER_MS with its
    // whole subtree silent), so an `added === 0` poll can still flip a span
    // that was recent when loaded to abandoned. buildLayout() re-runs
    // annotateRenderModel against a fresh nowMs, so the cap self-corrects
    // within one poll of the deadline instead of painting running-to-now
    // until a reload (#67 P1). Re-annotation is also self-healing: a fresh
    // child or a backdated twin flips an abandoned span back to live/closed.
    if (!destroyed) {
      buildLayout();
      // Cap the store only on a SETTLED ingestion: every walk reported
      // `hasNextPage: false` and none is in flight (#109), and `buildLayout()`
      // just resolved the render model, so a session's membership, summaries and
      // twins are known. Mid-walk the store is a half-loaded set — a session's
      // closing summary can be on the page still owed — and evicting off that is
      // how a unit gets broken. Rebuild once more on whatever the cap took (#111).
      if (ingestionSettled() && pruneSpans()) buildLayout();
      requestDraw();
      // If this is the tick that finished the fill — the deep backlog boot could
      // not drain, the retry after a failed page — an owed resolve is spent now,
      // on the whole store rather than on the part of it that had arrived when
      // the obligation was armed (#108).
      settleAncestorResolve();
    }
  }

  // Is the ingestion settled — nothing owed, nothing in flight?
  function ingestionSettled() {
    return walks.size === 0 && projectFetching.size === 0;
  }

  // The timer starts no live walk for a paused view, but panning back keeps
  // pulling history into the same store, so the cap still has to bind for it —
  // under exactly the same terms (settled ingestion, resolved model). Guarded on
  // the cap so a paused tick costs nothing while the store is within it (#111).
  function capPausedStore() {
    if (destroyed || spans.size <= SPAN_CAP || !ingestionSettled()) return;
    buildLayout();
    if (pruneSpans()) {
      buildLayout();
      requestDraw();
    }
  }

  // Does a FINISHED walk already reach back to what the view is showing? Only a
  // finished walk gets to make that claim, so a project still owing a truncated
  // gap is never "covered" (#109).
  function historyCovered(projectId) {
    if (pendingWalk(WALK_HISTORY, projectId)) return false;
    const floor = historyFloor.get(projectId);
    return floor != null && floor <= viewStart();
  }

  // Should the poll timer run a history pass? Yes while any gap walk is still
  // owed — truncated by the page cap or dropped by a failed page, either way it
  // has claimed no coverage and only its own cursor can finish it.
  //
  // And yes for a PAUSED view whose left edge a FINISHED walk stopped short of:
  // that is the gap a pan opened while another walk held the project (its bounds
  // were already fixed, so the `projectFetching` guard dropped that call), and a
  // paused `viewStart` never drifts off it the way a live one does — without
  // this it would wait for the operator's next gesture (#109). A project with no
  // committed floor is NOT owed here: no walk has finished for it yet, so the
  // live/initial walk still owns that range and is being drained above.
  function historyPassOwed() {
    if (walkOwed(WALK_HISTORY)) return true;
    if (live) return false;
    const target = viewStart();
    return projects.some((p) => {
      const floor = historyFloor.get(p.id);
      return floor != null && floor > target;
    });
  }

  // History paging: when the user pans left of what we've loaded, pull the gap
  // [viewStart, floor) for each project. Lazy + guarded against re-entrancy.
  //
  // A gap deeper than MAX_POLL_PAGES is finished across several passes: the
  // unfinished walk keeps the gap it was opened for and resumes on its cursor,
  // and the gap only counts as covered once that walk runs out of pages (#109).
  //
  // Each project is drained in bounded ROUNDS, and every round re-derives the
  // gap from the CURRENT `viewStart`. A walk's bounds are fixed once it starts —
  // that is what keeps its cursor meaningful — so it cannot widen to swallow a
  // pan that lands mid-flight, and such a pan is dropped by the `projectFetching`
  // guard. Re-deriving here is what asks for the newly exposed range instead of
  // leaving it for the operator's next gesture (#109). Rounds are capped so one
  // pass can't chase a continuous drag indefinitely; whatever is still uncovered
  // is picked up by the poll timer, which re-checks a paused view every tick.
  async function loadHistory() {
    for (const p of projects) {
      if (destroyed) break;
      if (historyCovered(p.id)) continue;
      if (projectFetching.has(p.id)) continue; // in flight — that walk re-checks
      projectFetching.add(p.id);
      try {
        for (let round = 0; round < MAX_HISTORY_ROUNDS; round++) {
          if (destroyed || historyCovered(p.id)) break;
          const target = viewStart();
          const pending = pendingWalk(WALK_HISTORY, p.id);
          const floor = historyFloor.get(p.id);
          const walk =
            pending || newWalk({ startMs: target, endMs: floor != null ? floor : viewEnd });
          const { added, done } = await drainWalk(walk, p.id, p.name, WALK_HISTORY);
          // Mark the requested floor as covered even if the page was empty, so we
          // don't refetch the same empty gap every frame while panned back — but
          // only for a gap whose walk actually finished.
          if (done) {
            const prev = historyFloor.get(p.id);
            if (prev == null || walk.startMs < prev) historyFloor.set(p.id, walk.startMs);
          }
          if (added) {
            buildLayout();
            requestDraw();
          }
          // Truncated: this walk owns the rest of its gap and resumes on its own
          // cursor from the next tick — starting another one here would fetch a
          // range it has already claimed.
          if (!done) break;
        }
      } catch (_e) {
        /* non-fatal — the walk keeps its cursor and the next tick resumes it */
      } finally {
        projectFetching.delete(p.id);
      }
    }
    // The pan that armed the obligation is only really settled once its pages
    // land — several ticks later for a gap deeper than the page cap. Spending it
    // here, and only when every walk is finished, is what gets the freshly paged
    // history its ancestors without a second gesture; a partial drain spends
    // nothing, because a walk still owed has not yet said what is orphaned (#108).
    settleAncestorResolve();
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
      // Non-null only for the virtual `session=scheduler` band: its aggregate
      // heartbeat/tick counts plus the real extent its envelope is drawn across,
      // in the distinctly virtual style (#87/#92).
      scheduler: schedulerBandModel(members, nowMs),
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
        scheduler: band.scheduler,
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
    // Member rollups are derived from the spans + the annotations resolved just
    // below, so both go stale here together.
    rollupCache.clear();
    // Resolve the render model first: pair "(started)" markers with their twin,
    // close turn bands at their summary/next-turn, fold summaries. rHide spans
    // (paired markers, summary bars) are then excluded from every lane (#62).
    annotateRenderModel(spans.values(), nowMs);
    // Completion is part of the render model. Index it once per rebuild while
    // the store is already being traversed; a popover click must stay O(1) at
    // the supported 60k-span cap instead of normalizing the whole inventory.
    completedTurnKeys = turnCompletionIndex(spans.values());
    // Recompute the live re-fetch floors from the just-resolved openness so the
    // next poll pulls backdated closes for still-open work (#62 P1).
    openFloors.clear();
    for (const [k, v] of openStartFloors(spans.values())) openFloors.set(k, v);
    // Bucket by project → agent (nested under its orchestrator) → worker, in
    // render order with each lane's level already assigned (#101).
    const byProject = laneGroups(spans.values());

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

      for (const lane of byProject.get(name)) {
        const band = laneBands(lane.items, nowMs);
        const h = band.tracks * TRACK_H + 2 * LANE_VPAD;
        rows.push({
          type: "lane",
          projectName: name,
          projectId: projId,
          agent: lane.agent,
          orchestrator: lane.orchestrator,
          worker: lane.worker,
          label: lane.label,
          level: lane.level,
          items: band.items,
          sessionBands: band.sessionBands,
          envelopes: band.envelopes,
          tracks: band.tracks,
          y,
          h,
        });
        y += h;
      }
    }
    layout = { rows, contentH: y };
    reconcileSelectedTurnCompletions();
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

  function isHighlighted(span) {
    return Boolean(
      highlightedSpanId &&
        span &&
        span.spanId &&
        String(span.spanId) === String(highlightedSpanId),
    );
  }

  function drawHighlightRect(x, y, w, h, span) {
    if (!isHighlighted(span)) return;
    ctx.save();
    ctx.strokeStyle = HIGHLIGHT_COLOR;
    ctx.lineWidth = 2;
    ctx.shadowColor = HIGHLIGHT_COLOR;
    ctx.shadowBlur = 5;
    ctx.strokeRect(x - 2, y - 2, Math.max(8, w + 4), h + 4);
    ctx.restore();
  }

  function drawLane(row, y) {
    // Lane label (left gutter).
    // Anything below the top level (a worker sub-lane, an orchestrator-nested
    // agent lane and its workers) is muted/smaller; the indent keeps counting.
    ctx.fillStyle = row.level >= 2 ? theme.muted : theme.text;
    ctx.font = row.level >= 2 ? "11px system-ui, sans-serif" : "12px system-ui, sans-serif";
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
      if (b.scheduler) {
        // The virtual scheduler session (#87/#92). Its extent is REAL — 71
        // heartbeats really do span 29 minutes — so the envelope is drawn across
        // it; what must never happen is it reading as a solid running task, and
        // that is the STYLE's job: a faint wash under a dashed outline, labeled
        // with the session's heartbeat count and duration. The ticks below stay
        // the per-beat geometry.
        if (b.scheduler.envelope) {
          ctx.fillStyle = IDLE_COLOR;
          ctx.globalAlpha = VIRTUAL_BAND_ALPHA;
          ctx.fillRect(r.cx, r.ry, r.w, r.rh);
          ctx.globalAlpha = VIRTUAL_BAND_EDGE_ALPHA;
          ctx.strokeStyle = IDLE_COLOR;
          ctx.lineWidth = 1;
          ctx.setLineDash(VIRTUAL_BAND_DASH);
          ctx.strokeRect(r.cx + 0.5, r.ry + 0.5, Math.max(1, r.w - 1), Math.max(1, r.rh - 1));
          ctx.setLineDash([]);
          ctx.globalAlpha = 1;
        }
        ctx.fillStyle = theme.muted;
        ctx.font = "10px system-ui, sans-serif";
        ctx.fillText(
          `⏱ ${b.scheduler.label}`,
          Math.max(GUTTER_W + 3, r.cx + 3),
          r.ry + Math.min(r.rh, TRACK_H) / 2,
        );
      } else {
        ctx.fillStyle = SESSION_BAND_COLOR;
        ctx.globalAlpha = 0.1;
        ctx.fillRect(r.cx, r.ry, r.w, r.rh);
        ctx.globalAlpha = 1;
      }
      drawn.push({ x: r.cx, y: r.ry, w: r.w, h: r.rh, band: b });
    }

    // 2. Parent (subtree) envelopes, shallow → deep so a deeper envelope wins
    //    the hit-test within its sub-region. Tinted by the parent's span kind;
    //    the exposed part of each envelope hovers/clicks as that parent span.
    const envs = (row.envelopes || []).slice().sort((a, b) => a.depth - b.depth);
    for (const e of envs) {
      // The scheduler pseudo-root's subtree envelope covers exactly the band the
      // virtual envelope just painted (every tick is its child), so drawing it
      // too would restore the solid kind-tinted task bar the virtual style exists
      // to avoid — the band envelope is that geometry now (#87/#92).
      if (e.span.rScheduler) continue;
      if (!onScreen(e.start, e.end, e.open)) continue;
      const r = rectFor(e.start, e.end, e.open, e.trackTop, e.trackCount);
      ctx.fillStyle = e.span.rAbandoned
        ? ABANDONED_FILL
        : e.span.status === "error"
          ? ERROR_COLOR
          : kindColor(e.span.kind);
      ctx.globalAlpha = 0.14 + Math.min(0.16, e.depth * 0.05);
      ctx.fillRect(r.cx, r.ry, r.w, r.rh);
      ctx.globalAlpha = 1;
      drawHighlightRect(r.cx, r.ry, r.w, r.rh, e.span);
      drawn.push({ x: r.cx, y: r.ry, w: r.w, h: r.rh, span: e.span });
    }

    // 3. Span identity bars, grouped by ABSOLUTE track (each track belongs to a
    //    single session+depth), coalescing sub-pixel runs into density strips
    //    PER track — so a wide session band never coalesces with its sub-second
    //    children (the coalescer runs per depth level for free).
    const byTrack = new Map();
    for (const it of row.items || []) {
      const s = it.span;
      // Test visibility against the DRAWN extent (rEnd folds in a turn's summary
      // and an abandoned run's latest-child bound), not the raw span end.
      if (!onScreen(s.start, s.rEnd != null ? s.rEnd : s.end, isOpen(s))) continue;
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
      // Heartbeat coalescing is resolved against the CURRENT px/ms scale (#92),
      // so beats merge only where they would genuinely overdraw — zoomed in they
      // each stand at their real time. A highlighted beat is held out so a
      // cross-view reveal can always land on the exact span.
      const beats = list.filter((s) => s.rIdle && !isHighlighted(s));
      const beatRunOf = new Map();
      for (const hr of beats.length ? heartbeatRuns(beats, pxPerMs()) : []) {
        for (const b of hr.spans) beatRunOf.set(b, hr);
      }
      let run = null; // pending sub-pixel density run {x0,x1,count,errored}
      const flush = () => {
        if (!run) return;
        const rw = Math.max(2, run.x1 - run.x0);
        ctx.fillStyle = DENSITY_COLOR;
        ctx.fillRect(run.x0, ry, rw, bh);
        if (run.errored) {
          ctx.fillStyle = ERROR_COLOR;
          ctx.fillRect(run.x0, ry, rw, 2);
        }
        drawn.push({ x: run.x0, y: ry, w: rw, h: bh, density: run.count });
        run = null;
      };
      // One heartbeat run: the beats it stands for, painted across their real
      // time extent and carrying the COUNT whenever the bar can hold it.
      const drawBeatRun = (hr) => {
        const cx = Math.max(GUTTER_W, timeToX(hr.startMs));
        const w = Math.max(HEARTBEAT_PX, timeToX(hr.endMs) + HEARTBEAT_PX - cx);
        ctx.fillStyle = IDLE_COLOR;
        ctx.fillRect(cx, ry, w, bh);
        if (hr.spans.some((s) => s.status === "error")) {
          ctx.fillStyle = ERROR_COLOR;
          ctx.fillRect(cx, ry, w, 2);
        }
        if (hr.count === 1) {
          // A lone beat keeps its own span identity (hover/click reaches the
          // tick), with a padded hit box so a 3px beat stays clickable.
          drawHighlightRect(cx - 2, ry, w + 4, bh, hr.spans[0]);
          drawn.push({ x: cx - 2, y: ry, w: w + 4, h: bh, span: hr.spans[0] });
          return;
        }
        if (w > HEARTBEAT_LABEL_PX) {
          // A coalesced run carries its COUNT on the bar — aggregated, never
          // silently collapsed (#87). The beats separate again on zoom in.
          ctx.fillStyle = "#0b1120";
          ctx.font = "10px system-ui, sans-serif";
          ctx.save();
          ctx.beginPath();
          ctx.rect(cx, ry, w, bh);
          ctx.clip();
          ctx.fillText(`${hr.count} heartbeats`, cx + 3, ry + bh / 2);
          ctx.restore();
        }
        drawn.push({ x: cx, y: ry, w, h: bh, density: hr.count, heartbeat: true });
      };
      for (const s of list) {
        if (s.rAbandoned) {
          // SIGKILL'd / never-completed run: a muted/hatched stub bounded to the
          // latest observed child end (rEnd), or a fixed ~24px stub when childless
          // — NEVER open-ended out to the live edge (#67). Not coalesced.
          flush();
          const aEnd = s.rEnd != null ? s.rEnd : s.end;
          const hasExtent = aEnd > s.start;
          const x = timeToX(s.start);
          const cx = Math.max(GUTTER_W, x);
          const rawW = hasExtent ? (aEnd - s.start) * pxPerMs() : ABANDONED_STUB_PX;
          const w = Math.max(2, x + rawW - cx);
          fillAbandoned(cx, ry, w, bh);
          drawHighlightRect(cx, ry, w, bh, s);
          if (w > 46) {
            ctx.fillStyle = theme.muted;
            ctx.font = "10px system-ui, sans-serif";
            ctx.save();
            ctx.beginPath();
            ctx.rect(cx, ry, w, bh);
            ctx.clip();
            ctx.fillText(`⚠ ${s.rLabel || s.name}`, cx + 3, ry + bh / 2);
            ctx.restore();
          }
          drawn.push({ x: cx, y: ry, w, h: bh, span: s });
          continue;
        }
        if (s.rIdle) {
          // An idle scheduler heartbeat (#87): zero-duration by construction (it
          // ran and did nothing), so it paints as a narrow teal beat — distinct
          // from work and from the wide refusal stub — at its real time. Beats
          // that would overdraw each other at this zoom share one counted run
          // (#92), painted once when its first member comes round.
          const hr = beatRunOf.get(s);
          if (hr) {
            if (hr.spans[0] !== s) continue; // already painted with its run
            flush();
            drawBeatRun(hr);
            continue;
          }
          // A highlighted beat is drawn on its own so a cross-view reveal can
          // always land on the exact span.
          flush();
          const bx = Math.max(GUTTER_W, timeToX(s.start));
          ctx.fillStyle = IDLE_COLOR;
          ctx.fillRect(bx, ry, HEARTBEAT_PX, bh);
          drawHighlightRect(bx - 2, ry, HEARTBEAT_PX + 4, bh, s);
          drawn.push({ x: bx - 2, y: ry, w: HEARTBEAT_PX + 4, h: bh, span: s });
          continue;
        }
        const open = isOpen(s);
        const closedEnd = s.rEnd != null ? s.rEnd : s.end;
        const sEnd = open ? rightT : closedEnd;
        // A denied/incomplete tool is zero-duration in the DATA (it never ran),
        // so give it a fixed visible stub at PAINT time instead of an unreadable
        // tick — a refusal is an event operators must SEE (#84). Never coalesced
        // into a density strip, and never wider than its own (zero) extent
        // implies elsewhere: the stub is paint, not a duration claim.
        const stub = !open && s.rOutcome != null;
        // A true instant (zero-width) is a tick; a turn root whose band end was
        // folded in from its summary (rEnd > start) paints as a labeled bar.
        const tick = !open && !stub && closedEnd <= s.start;
        const x = timeToX(s.start);
        const extentW = (sEnd - s.start) * pxPerMs();
        const rawW = tick ? 2 : stub ? Math.max(OUTCOME_STUB_PX, extentW) : extentW;
        const cx = Math.max(GUTTER_W, x);
        const w = Math.max(1, x + rawW - cx);
        if (!tick && !stub && w < MIN_BLOCK_PX && !isHighlighted(s)) {
          // Coalesce sub-pixel blocks into a density strip. Heartbeats never
          // enter this run — they carry their own teal, zoom-adaptive runs — so
          // a work span can never be painted teal or counted as a beat (#87).
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
          drawHighlightRect(cx - 2, ry, 6, bh, s);
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
        drawHighlightRect(cx, ry, w, bh, s);
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

  // Muted diagonal-hatch fill for an ABANDONED run/marker band (#67) — reads as
  // "died", visually distinct from a live/kind-colored bar.
  function fillAbandoned(x, y, w, h) {
    ctx.fillStyle = ABANDONED_FILL;
    ctx.globalAlpha = 0.6;
    ctx.fillRect(x, y, w, h);
    ctx.globalAlpha = 1;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, h);
    ctx.clip();
    ctx.strokeStyle = ABANDONED_HATCH;
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let hx = x - h; hx < x + w; hx += 5) {
      ctx.moveTo(hx, y + h);
      ctx.lineTo(hx + h, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.restore();
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

  // ── Shared detail model (hover tooltip + click popover) ──
  //
  // Hover and click answer the same questions, so they resolve the same
  // `normalizeSpanDetail` model — one contract with the Navigator inspector
  // (#88). Only the surface differs: the tooltip renders `spanTooltipLines`,
  // the popover the full field list.

  // `mousemove` resolves a detail for every span it crosses, and a rollup is a
  // whole-subtree walk — so memoize it. `rollupCache` (declared with the layout
  // cache above) is cleared by `buildLayout`, the one place spans and their
  // render annotations change (poll, history page, collapse, view change).
  function memberRollupCached(s) {
    if (!s.spanId) return spanMemberRollup(s, spans, childrenByParent);
    if (rollupCache.has(s.spanId)) return rollupCache.get(s.spanId);
    const rollup = spanMemberRollup(s, spans, childrenByParent);
    rollupCache.set(s.spanId, rollup);
    return rollup;
  }

  function spanDetail(s) {
    const stopContext = resolveTurnAddressContext(s);
    return normalizeSpanDetail(s, {
      sessionId: resolveSessionId(s),
      members: memberRollupCached(s),
      ...stopContext,
    });
  }

  function resolveTurnAddressContext(s) {
    const context = {};
    let current = s;
    let guard = 0;
    while (current && guard++ < 100000) {
      const attrs = current.attrs || parseAttributes(current.attributes);
      if (context.turnId == null) context.turnId = getAttr(attrs, ATTR_TURN_ID);
      if (context.agentDid == null) context.agentDid = getAttr(attrs, ATTR_AGENT_DID);
      if (
        context.agentName == null &&
        current.agent &&
        current.agent !== UNKNOWN_AGENT
      ) {
        context.agentName = current.agent;
      }
      if (context.turnId && context.agentDid && context.agentName) break;
      if (!current.parentId) break;
      const parentNodeId = spanIdToId.get(current.parentId);
      if (parentNodeId == null || parentNodeId === current.id) break;
      current = spans.get(parentNodeId);
    }
    return context;
  }

  // A leaf tool's own closed span does not mean its owning turn is complete.
  // Only the folded/actual turn summary closes the operator's Stop door.
  function knownTurnCompletion(s, detail) {
    // The layout's one-pass index can prove completion in O(1). Absence is not
    // proof of liveness; only a focused full-trace read can make that negative
    // claim authoritative.
    if (completedTurnKeys.has(turnCompletionIdentityKey(detail))) {
      return Object.freeze({ completed: true, completionKnown: true });
    }
    return (
      turnCompletionCache.get(timelineTurnCompletionKey(s)) ||
      Object.freeze({ completed: false, completionKnown: false })
    );
  }

  function loadTurnCompletion(s, detail) {
    const key = timelineTurnCompletionKey(s);
    if (
      !key ||
      turnCompletionLoads.has(key) ||
      !stopController ||
      !stopTargetFromDetail(detail).addressable ||
      completedTurnKeys.has(turnCompletionIdentityKey(detail))
    ) return;
    // Positive completion is permanent. A negative focused snapshot describes
    // only that instant: a paused view may receive no local summary afterward,
    // so reopening the popover must revalidate instead of treating "active"
    // as immutable. Remove it before the request so Stop stays disabled while
    // that revalidation is in flight.
    if (turnCompletionCache.get(key)?.completed === true) return;
    turnCompletionCache.delete(key);
    const traceWalkAbort = new AbortController();
    turnCompletionAborts.set(key, traceWalkAbort);
    const operation = (async () => {
      try {
        const inventory = await walkTraceSpans(
          s.projectId,
          s.traceId,
          { signal: traceWalkAbort.signal },
        );
        if (destroyed) return;
        const trace = inventory.trace;
        if (!trace) return;
        const evidence = turnCompletionEvidence(inventory.spans, detail, {
          truncated: inventory.complete !== true,
        });
        turnCompletionCache.set(key, evidence);
        // The selection can outlive both the popover and this Timeline tab.
        // Publish focused evidence to the shared controller immediately; the
        // active-popover repaint below is presentation only.
        observeFocusedTurnCompletion(stopController, detail, evidence);
      } catch (_error) {
        // Unknown is load-bearing: a failed focused read never enables Stop.
      } finally {
        turnCompletionLoads.delete(key);
        turnCompletionAborts.delete(key);
        if (
          !destroyed &&
          sameTimelineTurnCompletion(activePopoverSpan, s) &&
          !popEl.hidden
        ) syncPopoverStopActions();
      }
    })();
    turnCompletionLoads.set(key, operation);
  }

  function retryActivePopoverTurnCompletion() {
    if (!activePopoverSpan || popEl.hidden) return;
    // The viewport poll and this authoritative full-trace read are separate
    // queries. A transient failure in the focused read leaves completion
    // unknown by design, so the operator's explicit Refresh must retry it too
    // instead of only refreshing the visible span window.
    loadTurnCompletion(activePopoverSpan, spanDetail(activePopoverSpan));
  }

  function stopTargetForSpan(s, detail = spanDetail(s)) {
    const completion = knownTurnCompletion(s, detail);
    const target = stopTargetFromDetail(detail, completion);
    if (stopController) stopController.observe(target);
    return target;
  }

  function reconcileSelectedTurnCompletions() {
    if (!stopController || typeof stopController.knownTargets !== "function") return;
    // Selection and inspector-only results can contain many turns. Reuse the
    // one index built with this layout instead of scanning per retained target.
    for (const target of stopController.knownTargets()) {
      if (!completedTurnKeys.has(turnCompletionIdentityKey(target))) continue;
      stopController.observe(
        Object.freeze({
          ...target,
          completed: true,
          completionKnown: true,
        }),
      );
    }
  }

  // The session band is not a span, but it answers the same questions — so it
  // resolves through the same model, with its members standing in for a subtree.
  function bandDetail(b) {
    const end = b.open ? viewEnd : b.end;
    const sch = b.scheduler;
    const title = b.sessionId
      ? `session ${b.sessionId}`
      : b.traceId
        ? `trace ${b.traceId}`
        : "session";
    return normalizeSpanDetail(
      { name: title, kind: "session", start: b.start, end, rOpen: b.open === true },
      {
        sessionId: b.sessionId,
        traceId: b.traceId,
        summary: b.summary || undefined,
        durationMs: end > b.start ? end - b.start : null,
        members: {
          count: b.count,
          startMs: b.start,
          endMs: end,
          virtual: Boolean(sch),
          heartbeatCount: sch ? sch.idleCount : null,
          workCount: sch ? sch.workCount : null,
        },
      },
    );
  }

  function tipHtml(detail) {
    return (
      `<b>${escapeHtml(detail.displayName)}</b>` +
      spanTooltipLines(detail)
        .map(
          ({ text, tone }) =>
            `<div class="${tone === "warn" ? "obs-tl__tipwarn" : "obs-tl__tipdim"}">` +
            `${escapeHtml(text)}</div>`,
        )
        .join("")
    );
  }

  // ── Tooltip ──
  function showTip(d, clientX, clientY) {
    if (d.project) {
      hideTip();
      return;
    }
    let html;
    if (d.density) {
      html = d.heartbeat
        ? `<b>${d.density} heartbeats</b>` +
          `<div class="obs-tl__tipdim">idle · coalesced · zoom in to separate</div>`
        : `<b>${d.density} spans</b><div class="obs-tl__tipdim">coalesced · zoom in to expand</div>`;
    } else if (d.band) {
      html = tipHtml(bandDetail(d.band));
    } else if (d.span) {
      // The hover reads off the SAME normalized detail the click popover
      // renders — role/outcome/feature/orchestrator/run/turn, and for a
      // container what it covers — instead of the old generic
      // "<name> / AGENT / instant / ok" (#88).
      html = tipHtml(spanDetail(d.span));
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
    const detail = spanDetail(s);
    loadTurnCompletion(s, detail);
    const stopTarget = stopTargetForSpan(s, detail);
    const stopModel = stopActionModel(stopTarget, stopController);
    const canNav = Boolean(
      openNavigator &&
        detail.projectId &&
        detail.agent &&
        detail.sessionId &&
        detail.traceId &&
        detail.spanId,
    );
    const canPhx = Boolean(openTrace && s.traceId && s.projectId);
    activePopoverSpan = s;
    popEl.innerHTML = `
      <div class="obs-tl__phead">
        <span class="obs-tl__ptitle" title="${escapeHtml(detail.name)}">${escapeHtml(detail.displayName)}</span>
        <button type="button" class="obs-tl__pclose" data-pclose aria-label="Close">✕</button>
      </div>
      <div class="obs-tl__pbody">${renderSpanDetail(detail, { rawAttributes: false })}</div>
      <div class="obs-tl__pfoot">
        ${stopController ? `<button type="button" class="obs-tl__plink obs-tl__plink--stop" data-pstop title="${escapeHtml(stopModel.stopLabel)}" ${stopModel.disabled ? "disabled" : ""}>${escapeHtml(stopModel.stopLabel)}</button>` : ""}
        ${stopController ? `<button type="button" class="obs-tl__plink" data-pselect ${stopModel.addressable ? "" : "disabled"}>${stopModel.selected ? "Remove from Stop selection" : "Add to Stop selection"}</button>` : ""}
        ${canNav ? `<button type="button" class="obs-tl__plink" data-pnav>Open in Navigator</button>` : ""}
        ${canPhx ? `<button type="button" class="obs-tl__plink" data-pphx>Open in Phoenix</button>` : ""}
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
        openNavigator(buildNavigatorRevealTarget(detail));
      });
    }
    const phxBtn = popEl.querySelector("[data-pphx]");
    if (phxBtn) {
      phxBtn.addEventListener("click", () => {
        const url = openTraceUrl(s);
        if (url && openTrace) openTrace(url);
      });
    }
    const stopBtn = popEl.querySelector("[data-pstop]");
    if (stopBtn && stopController) {
      // Polling can fold a turn summary while this popover stays open. Resolve
      // lifecycle state at dispatch time rather than retaining the target from
      // when the popover was first rendered.
      stopBtn.addEventListener("click", () =>
        stopController.stopOne(stopTargetForSpan(s)),
      );
    }
    const selectBtn = popEl.querySelector("[data-pselect]");
    if (selectBtn && stopController) {
      selectBtn.addEventListener("click", () =>
        stopController.toggle(stopTargetForSpan(s)),
      );
    }
  }

  function syncPopoverStopActions() {
    if (!stopController || !activePopoverSpan || popEl.hidden) return;
    const target = stopTargetForSpan(activePopoverSpan);
    const model = stopActionModel(target, stopController);
    const stopBtn = popEl.querySelector("[data-pstop]");
    if (stopBtn) {
      stopBtn.disabled = model.disabled;
      stopBtn.textContent = model.stopLabel;
      stopBtn.title = model.stopLabel;
    }
    const selectBtn = popEl.querySelector("[data-pselect]");
    if (selectBtn) {
      selectBtn.disabled = !model.addressable;
      selectBtn.textContent = model.selected
        ? "Remove from Stop selection"
        : "Add to Stop selection";
    }
  }
  function hidePopover() {
    activePopoverSpan = null;
    popEl.hidden = true;
    popEl.innerHTML = "";
  }

  // The session band's click popover: the folded `session summary` stats (turn /
  // tool count, success ratio, duration) so the band answers "what was this
  // session" without a duplicate summary bar (#62) — rendered through the shared
  // detail contract, so a summary-LESS band (notably the virtual scheduler
  // session, which never reconciles) still identifies itself and reports what it
  // covers (#88).
  function showBandPopover(b, clientX, clientY) {
    const detail = bandDetail(b);
    const title = detail.displayName;
    popEl.innerHTML = `
      <div class="obs-tl__phead">
        <span class="obs-tl__ptitle" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
        <button type="button" class="obs-tl__pclose" data-pclose aria-label="Close">✕</button>
      </div>
      <div class="obs-tl__pbody">${renderSpanDetail(detail, { rawAttributes: false })}</div>`;
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
    viewportChanged();
    requestDraw();
  }

  // The one commit path for every time-pan gesture (wheel AND drag): run the
  // candidate right edge past the magnetic present edge, then land it.
  function panTimeTo(candidateViewEnd) {
    const snapped = magneticViewEnd(candidateViewEnd, Date.now(), LIVE_SNAP_PX / pxPerMs());
    viewEnd = snapped.viewEnd;
    if (snapped.live) setLive(true);
    else pauseLive();
    viewportChanged();
    requestDraw();
  }

  function scrollLanes(dy) {
    // `draw` projects rows at `row.y - laneScrollY`, so a positive offset lifts
    // content: scrolling down (deltaY > 0) reveals the lower lanes.
    laneScrollY += dy;
    clampScroll();
    requestDraw();
  }

  canvas.addEventListener("wheel", (e) => {
    const intent = wheelIntent(e);
    e.preventDefault();
    if (intent.zoom) {
      zoomAt(intent.zoom, e.offsetX);
      return;
    }
    if (intent.panTimeDx) panTimeTo(viewEnd + intent.panTimeDx / pxPerMs());
    if (intent.scrollLanesDy) scrollLanes(intent.scrollLanesDy);
  }, { passive: false });

  // Drag: horizontal → pan time (pauses live, or re-engages it at the magnetic
  // present edge); vertical → scroll lanes.
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
          panTimeTo(viewEnd - dx / pxPerMs());
        } else {
          scrollLanes(-dy);
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
    if (d.band) {
      // Every band opens its session popover: even without folded summary stats
      // it names the session and reports what it covers (#88).
      showBandPopover(d.band, clientX, clientY);
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
      viewportChanged();
      requestDraw();
      return;
    }
    if (d.span) showPopover(d.span, clientX, clientY);
  }

  // Toolbar.
  liveBtn.addEventListener("click", () => setLive(!live));
  container.querySelector("[data-zoomin]").addEventListener("click", () => zoomAt(1 / 1.6, null));
  container.querySelector("[data-zoomout]").addEventListener("click", () => zoomAt(1.6, null));
  container.querySelector("[data-refresh]").addEventListener("click", () => {
    pollTick(true);
    retryActivePopoverTurnCompletion();
  });

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
        const carried = getState();
        teardown();
        const replacement = mount(container, opts);
        replacement.setState(carried); // a retry is a remount — keep the view
        handleProxy.destroy = replacement.destroy;
        handleProxy.getState = replacement.getState;
        handleProxy.setState = replacement.setState;
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
    try {
      if (revealTarget) {
        await loadRevealWindow();
      } else {
        await pollTick(true); // initial fill of the visible window
      }
    } catch (_e) {
      // A failed initial page must not cost us the poll timer — that timer is
      // what resumes the walk, which kept its cursor across the throw (#109).
    }
    if (destroyed) return;
    buildLayout();
    // Only a SETTLED reveal gets to report — a walk the page cap cut short is
    // still owed, and the poll timer below finishes it and reports then (#109).
    if (revealTarget && !revealPending) finishReveal();
    // Whatever the initial load pulled, the run/turn roots above it may have
    // started before the window and be missing entirely, and there is no gesture
    // coming to trigger a resolve — opening the panel mid-run is the other way
    // this bug is hit. So arm the obligation: it is spent here if the fill above
    // actually settled, and otherwise on whichever tick finishes it (#108).
    armAncestorResolve();
    pollTimer = setInterval(() => pollTick(false), POLL_MS);
    booted = true;
    // `live` is `!revealTarget` unless setState() restored a paused window
    // first (#86): an exact reveal stays paused, a restored history window
    // stays paused, and a normal (or restored-live) entry follows the clock.
    setLive(live);
  }

  // ── Persisted view state (#86) ──
  //
  // The serializable slice of this mount that the console's panel view-state
  // provider (kestrel-sovereign #2802) round-trips across a sub-tab remount and
  // a full page reload: zoom (`windowMs`), the pan anchor (`viewEnd`),
  // live-follow, lane scroll, the collapsed projects, and the drilled/highlighted
  // span.
  //
  // Restoring must not fight live-follow: a state captured while live resumes
  // live and re-anchors on the wall clock (its stored `viewEnd` is stale by
  // definition), while one captured panned back into history restores that exact
  // window PAUSED. A cross-view reveal (`opts.revealTarget`) is an explicit
  // navigation and outranks any stored window, so it keeps its own window,
  // highlight and scroll — only the collapse set is restored alongside it.
  function getState() {
    return {
      windowMs,
      viewEnd,
      live,
      laneScrollY,
      collapsed: [...collapsed],
      highlightedSpanId,
    };
  }

  function setState(state) {
    if (!state || typeof state !== "object") return false;
    if (Array.isArray(state.collapsed)) {
      collapsed.clear();
      for (const name of state.collapsed) {
        if (typeof name === "string") collapsed.add(name);
      }
    }
    if (!revealTarget) {
      const w = Number(state.windowMs);
      if (Number.isFinite(w)) windowMs = Math.min(MAX_WINDOW_MS, Math.max(MIN_WINDOW_MS, w));
      // A missing/garbage flag falls back to the default: live-follow on.
      live = state.live !== false;
      const end = Number(state.viewEnd);
      // Only a paused state restores its right edge — and never one past "now",
      // so a stale snapshot can't park the view in the future.
      if (!live && Number.isFinite(end)) viewEnd = Math.min(end, Date.now());
      const scroll = Number(state.laneScrollY);
      if (Number.isFinite(scroll)) laneScrollY = Math.max(0, scroll);
      highlightedSpanId =
        state.highlightedSpanId != null ? String(state.highlightedSpanId) : null;
    }
    // Restored before boot (the normal path — the panel restores us right after
    // mount), the values simply ARE the boot-time view, so the initial poll
    // fills the restored window. Restored after boot, reapply them live.
    if (booted) {
      buildLayout();
      setLive(live);
      requestDraw();
      // A restored window is a viewport change like any other: it can land on
      // history whose run/turn roots start before it (#108).
      if (!live) viewportChanged();
    }
    return true;
  }

  function teardown() {
    destroyed = true;
    stopUnsubscribe();
    for (const traceWalkAbort of turnCompletionAborts.values()) {
      try {
        traceWalkAbort.abort();
      } catch (_e) {
        /* aborting is best-effort */
      }
    }
    turnCompletionAborts.clear();
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    // A debounced resolve must not fire into a dead mount, and one already in
    // flight stops at its next checkpoint (`destroyed`) while the abort drops
    // whatever is on the wire (#108).
    if (ancestorTimer) {
      clearTimeout(ancestorTimer);
      ancestorTimer = null;
    }
    if (ancestorAbort) {
      try {
        ancestorAbort.abort();
      } catch (_e) {
        /* aborting is best-effort */
      }
    }
    window.removeEventListener("resize", onResize);
  }

  boot();

  const handleProxy = { destroy: teardown, getState, setState };
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
    .obs-tl__reveal { position:absolute; z-index:4; top:6px; left:50%; transform:translateX(-50%);
                      max-width:calc(100% - 24px); padding:4px 10px; border-radius:999px;
                      background:color-mix(in srgb, #facc15 18%, var(--color-surface,#1e293b));
                      border:1px solid #facc15; color:var(--color-text,#e2e8f0);
                      font-size:11px; font-weight:600; pointer-events:none; }
    .obs-tl__reveal--fallback { border-color:#f59e0b;
                                background:color-mix(in srgb, #f59e0b 15%, var(--color-surface,#1e293b)); }
    .obs-tl__tip { position:absolute; z-index:5; pointer-events:none; max-width:320px;
                   background:var(--color-surface,#1e293b); border:1px solid var(--color-border,#334155);
                   border-radius:6px; padding:6px 9px; font-size:12px; line-height:1.35;
                   box-shadow:0 6px 20px rgba(0,0,0,.35); }
    .obs-tl__tipdim { color:var(--color-text-muted,#94a3b8); font-size:11px; }
    .obs-tl__tipwarn { color:#f59e0b; font-size:11px; font-weight:600; }
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
    .obs-tl__pfoot { display:flex; flex-wrap:wrap; gap:8px; padding:8px 10px; border-top:1px solid var(--color-border,#334155); }
    .obs-tl__plink { background:transparent; border:1px solid var(--color-border,#334155); border-radius:999px;
                     color:var(--color-accent,#818cf8); cursor:pointer; font-size:11px; font-weight:600;
                     padding:2px 10px; }
    .obs-tl__plink:hover:not(:disabled) { background:var(--color-surface,#1e293b); }
    .obs-tl__plink--stop { color:var(--color-danger,#f87171); }
    .obs-tl__plink:disabled { cursor:not-allowed; opacity:.45; }
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
