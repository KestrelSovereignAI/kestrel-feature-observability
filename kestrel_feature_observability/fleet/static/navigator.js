// Fleet Navigator — hierarchical fleet drill-down over Phoenix GraphQL (#46).
//
// The centerpiece view of the OTel pivot (epic #32): Phoenix gives per-trace
// waterfalls; this view is the scrolling, lazily-expanding navigator ACROSS the
// whole fleet:
//
//   Tenant → Fleet → Agent → Subagent → Session → Turn → Events
//
// Pure read-model — no store, no new host routes. Every level reads Phoenix's
// GraphQL through the same-origin `/phoenix/graphql` proxy, authenticated by the
// embed cookie (minted before the first query via
// `POST /api/host/phoenix/session`, re-minted once on a 401). Spans carry every
// attribute the hierarchy needs (see tracing.py / hook.py):
//
//   - the `openinference.project.name` Resource attribute routes spans into
//     Phoenix projects → the Fleet level (`kestrel-fleet` + one per repo),
//   - `kestrel.agent_name` splits the Agent level. Root spans carry the plain
//     name (talon's run root stamps `talon`); prefixed worker variants like
//     `talon/implement` normalize to their base so workers never surface as
//     separate agents,
//   - the Subagent level is the worker split — `kestrel.stage`, or the suffix
//     of a prefixed `kestrel.agent_name` — which the producers stamp on
//     NON-root child stage spans only (talon's run root has neither), so that
//     drill reads ALL spans of the agent, not just roots,
//   - Sessions group by `kestrel.session_id` (OpenInference `session.id`
//     fallback); talon stamps neither, so its per-run `kestrel.run_id`
//     (present on every talon span) is the final fallback — each talon run is
//     one session,
//   - root spans are Turns (session markers + per-interaction/run roots; talon
//     run roots are named `owner/repo#issue`),
//   - a turn's trace tree is the Events level (`openinference.span.kind`
//     TOOL / LLM / CHAIN / AGENT …). Selecting a Turn or Event keeps the tree
//     visible and fills the persistent span inspector beside it.
//
// Levels load LAZILY: expanding a node fires exactly ONE paginated GraphQL
// query for that level. Phoenix has no attribute group-by API, so the Agent /
// Subagent / Session distinct-ness is aggregated client-side from one page of
// recency-ordered spans — root-only where the level reads roots (Agents,
// Turns), all spans where the split lives on children (Subagent / Session
// under an agent). "Load more" merges further pages; span ids are deduped so
// overlapping pages never double-count. Tenant is a single static root until
// Castle tenancy lands.
//
// The whole tree lives in ONE virtualized scroll container (windowed rows with
// per-row offsets — hundreds of sessions stay smooth); expanded levels append
// inline with indentation guides, never modal/page navigation. Live-follow
// (off by default) polls every 10s, refreshing counts and prepending new
// sessions/turns. Keyboard: ↑/↓ move, → expand/descend, ← collapse/ascend,
// Enter/Space activates/selects. Phoenix down → the same friendly notice as the embed.
// Styles are console-native (dark/light aware) — kestrel chrome, not Phoenix's.

import {
  PHOENIX_URL,
  DEFAULT_PROJECT,
  ATTR_AGENT_NAME,
  ATTR_SESSION_ID,
  mintPhoenixSession,
  gql,
  PROJECTS_QUERY,
  SPAN_PAGE_QUERY,
  TRACE_SPANS_QUERY,
  escapeHtml,
  parseAttributes,
  getAttr,
  attrRef,
  dslString,
  exactAgentFilter,
  agentFilter,
  workerFilter,
  ts,
  relTime,
  fmtDuration,
  plural,
  baseAgentName,
  createAgg,
  mergeSpansIntoAgg,
  spanKindOf,
  spanSummaryOf,
  normalizeSpanDetail,
  renderSpanDetail,
  buildTimelineRevealTarget,
} from "./phoenix.js";

// Keep the pure read-model exports available from navigator.js for callers
// that predate phoenix.js becoming the shared source of truth.
export {
  attrRef,
  exactAgentFilter,
  agentFilter,
  workerFilter,
  baseAgentName,
  createAgg,
  mergeSpansIntoAgg,
} from "./phoenix.js";

const PAGE_SIZE = 100; // root spans per lazy page (client-side aggregation window)
const TRACE_SPAN_LIMIT = 1000; // events per turn (one trace)
const POLL_MS = 10_000; // live-follow cadence

// Virtualization: fixed-height tree rows.
const ROW_H = 28;
const OVERSCAN_PX = 200;

// Pure exact-selection contract used by the mounted reveal flow and Node
// behavior tests. OTel span id is authoritative; Phoenix node id is used only
// when no span id was supplied. A miss returns the containing Turn — never a
// nearby/different Event — and marks the result inexact.
export function resolveExactSpanReveal(turn, target = {}) {
  if (!turn) return { node: null, path: [], exact: false };
  const targetSpanId = target.spanId != null ? String(target.spanId) : null;
  const targetNodeId =
    targetSpanId == null && target.nodeId != null ? String(target.nodeId) : null;
  const matches = (node) => {
    const span = node && node.data && node.data.span;
    if (!span) return false;
    if (targetSpanId != null) {
      return String((span.context && span.context.spanId) || "") === targetSpanId;
    }
    return targetNodeId != null && String(span.id || "") === targetNodeId;
  };

  if (matches(turn)) return { node: turn, path: [turn], exact: true };
  if (targetSpanId == null && targetNodeId == null) {
    return { node: turn, path: [turn], exact: false };
  }

  const visit = (node, path) => {
    for (const child of node.children || []) {
      const childPath = [...path, child];
      if (matches(child)) return { node: child, path: childPath, exact: true };
      const nested = visit(child, childPath);
      if (nested) return nested;
    }
    return null;
  };
  return visit(turn, [turn]) || { node: turn, path: [turn], exact: false };
}

// ── View / mount ──────────────────────────────────────────────

export function mount(container, opts = {}) {
  ensureStyles();

  const openTrace = typeof opts.openTrace === "function" ? opts.openTrace : null;
  const openTimeline =
    typeof opts.openTimeline === "function" ? opts.openTimeline : null;
  // A one-shot reveal target from the Timeline's "open in Navigator" (#54):
  // {projectId, projectName, agentName, worker, sessionId, traceId, spanId,
  // nodeId}. Consumed once on boot to select that exact span, best-effort.
  const revealTarget = opts.revealTarget || null;

  let destroyed = false;
  let liveFollow = false; // off by default — noise-free
  let pollTimer = null;
  let polling = false;

  // ── Tree model ──
  //
  // Node kinds mirror the hierarchy levels; `event` nodes are pre-loaded from
  // their turn's trace fetch (expanding nested events is local, no query).
  let nextNodeId = 1;
  function makeNode(kind, label, data, parent) {
    return {
      id: nextNodeId++,
      kind,
      label,
      data: data || {},
      parent: parent || null,
      depth: parent ? parent.depth + 1 : 0,
      meta: "",
      status: null, // "ok" | "error" | null
      expandable: kind !== "event",
      expanded: false,
      loaded: false,
      loading: false,
      error: null,
      children: [],
      childIndex: new Map(), // stable child identity across merges/refreshes
      hasMore: false,
      cursor: null,
      agg: null, // client-side rollup for the aggregated levels
      turns: null, // session level: span-id → root span
      passThrough: undefined, // agent level: no worker split → sessions inline
    };
  }

  // Single static tenant root until Castle tenancy lands; then roots enumerate
  // tenants.
  const tenant = makeNode("tenant", "This deployment", {});
  tenant.expanded = true;

  // ── DOM scaffold ──

  container.innerHTML = `
    <div class="obs-nav">
      <div class="obs-nav__toolbar">
        <span class="obs-nav__title">Fleet Navigator</span>
        <span class="obs-nav__grow"></span>
        <button type="button" class="obs-nav__btn" data-live
                title="Live-follow: poll every 10s for new activity">Live</button>
        <button type="button" class="obs-nav__btn" data-refresh title="Refresh now">Refresh</button>
      </div>
      <div class="obs-nav__body" data-body>
        <div class="obs-nav__treepane">
          <div class="obs-nav__scroll" data-scroll tabindex="0" role="tree" aria-label="Fleet navigator">
            <div class="obs-nav__spacer" data-spacer></div>
          </div>
        </div>
        <aside class="obs-nav__inspector" data-inspector aria-live="polite">
          <div class="obs-nav__inspector-empty">Select a Turn or Event to inspect its span.</div>
        </aside>
      </div>
    </div>`;

  const bodyEl = container.querySelector("[data-body]");
  const scroller = container.querySelector("[data-scroll]");
  const spacerEl = container.querySelector("[data-spacer]");
  const liveBtn = container.querySelector("[data-live]");
  const refreshBtn = container.querySelector("[data-refresh]");
  const inspectorEl = container.querySelector("[data-inspector]");

  // ── Level loaders — one paginated GraphQL query per expand ──

  async function loadChildren(node, mode) {
    if (destroyed || node.loading) return;
    node.loading = true;
    if (mode !== "refresh") node.error = null;
    scheduleRebuild();
    try {
      switch (node.kind) {
        case "tenant":
          await loadProjects(node);
          break;
        case "project":
          await loadAgents(node, mode);
          break;
        case "agent":
          await loadAgentDrill(node, mode);
          break;
        case "subagent":
          await loadSessions(node, mode);
          break;
        case "session":
          await loadTurns(node, mode);
          break;
        case "turn":
          await loadEvents(node);
          break;
        default:
          break; // event children are pre-loaded
      }
      node.loaded = true;
      if (mode !== "refresh") node.error = null;
    } catch (e) {
      if (mode !== "refresh") node.error = (e && e.message) || "query failed";
    } finally {
      node.loading = false;
      scheduleRebuild();
    }
  }

  // Fleet: the Phoenix projects. Re-runnable (live-follow refreshes counts and
  // picks up new projects); child identity is stable by project id.
  async function loadProjects(node) {
    const data = await gql(PROJECTS_QUERY);
    const projects = ((data.projects && data.projects.edges) || [])
      .map((e) => e && e.node)
      .filter((p) => p && p.id);
    projects.sort((a, b) => {
      const ap = a.name === DEFAULT_PROJECT ? 0 : 1;
      const bp = b.name === DEFAULT_PROJECT ? 0 : 1;
      return ap - bp || String(a.name).localeCompare(String(b.name));
    });
    node.children = projects.map((p) => {
      const key = `project:${p.id}`;
      let child = node.childIndex.get(key);
      if (!child) {
        child = makeNode("project", String(p.name), { projectId: p.id, projectName: p.name }, node);
        node.childIndex.set(key, child);
      }
      const last = ts(p.endTime);
      child.meta = `${plural(p.traceCount ?? 0, "trace")}${last ? ` · ${relTime(last)}` : ""}`;
      return child;
    });
    node.meta = plural(node.children.length, "project");
  }

  // One page of recency-ordered spans for `node` (its project), through
  // Phoenix's span-filter DSL. `rootOnly` is per-level (Agents/Turns read
  // roots; Subagent/Session drills read all spans). `mode === "more"`
  // continues from the cursor; "refresh" (live-follow) re-reads the first
  // page without touching pagination.
  async function fetchSpanPage(node, { filter = null, mode, rootOnly, dir = "desc" }) {
    const data = await gql(SPAN_PAGE_QUERY, {
      projectId: node.data.projectId,
      first: PAGE_SIZE,
      after: mode === "more" ? node.cursor : null,
      filter,
      rootOnly: Boolean(rootOnly),
      sort: { col: "startTime", dir },
    });
    const conn = data.node && data.node.spans;
    const spans = ((conn && conn.edges) || []).map((e) => e && e.node).filter(Boolean);
    if (mode !== "refresh") {
      node.hasMore = Boolean(conn && conn.pageInfo && conn.pageInfo.hasNextPage);
      node.cursor = (conn && conn.pageInfo && conn.pageInfo.endCursor) || null;
    }
    return spans;
  }

  function ensureAgg(node) {
    if (!node.agg) node.agg = createAgg();
    return node.agg;
  }

  // The DSL prefix match in `agentFilter` is substring containment (not
  // anchored), so drop the rare over-match (`xtalon/…`) before aggregating.
  function ownedByAgent(spans, agentName) {
    return spans.filter((s) => {
      const a = getAttr(parseAttributes(s && s.attributes), ATTR_AGENT_NAME);
      return a != null && a !== "" && baseAgentName(a) === agentName;
    });
  }

  // Materialize an aggregate entry as a stable child node.
  function childFor(parent, kind, key, label, data, entry, countWord) {
    const mapKey = `${kind}:${key}`;
    let child = parent.childIndex.get(mapKey);
    if (!child) {
      child = makeNode(kind, label, data, parent);
      parent.childIndex.set(mapKey, child);
    }
    child.label = label;
    // Counts come from the scanned page(s) — mark them open-ended while more
    // pages exist so a partial scan never reads as a total.
    const approx = parent.hasMore ? "+" : "";
    child.meta = `${entry.count}${approx} ${countWord}${entry.count === 1 && !approx ? "" : "s"}${
      entry.last ? ` · ${relTime(entry.last)}` : ""
    }`;
    child.status = entry.errored ? "error" : null;
    return child;
  }

  function sessionChildren(node) {
    const entries = [...ensureAgg(node).sessions.entries()].sort(
      (a, b) => (b[1].last || 0) - (a[1].last || 0),
    );
    return entries.map(([sid, entry]) =>
      childFor(
        node,
        "session",
        sid,
        sid,
        // `sessionAttr` records which attribute identified the session
        // (kestrel.session_id / session.id / kestrel.run_id) so the Turn
        // level filters on that same attribute — talon sessions are
        // run_id-keyed, which no session-attr filter would match.
        { projectId: node.data.projectId, sessionId: sid, sessionAttr: entry.attrKey },
        // Turns are the session's ROOT spans; when the scanned page held only
        // children (roots outside the window), fall back to an honest span
        // count rather than calling child spans "turns".
        entry.roots ? { ...entry, count: entry.roots } : entry,
        entry.roots ? "turn" : "span",
      ),
    );
  }

  // Agent level: distinct kestrel.agent_name among the project's ROOT spans
  // (emitter session markers and talon run roots both stamp it there);
  // prefixed worker variants normalize to their base agent.
  async function loadAgents(node, mode) {
    const spans = await fetchSpanPage(node, { mode, rootOnly: true });
    mergeSpansIntoAgg(ensureAgg(node), spans);
    const entries = [...node.agg.agents.entries()].sort(
      (a, b) => (b[1].last || 0) - (a[1].last || 0),
    );
    node.children = entries.map(([name, entry]) =>
      childFor(
        node,
        "agent",
        name,
        name,
        { projectId: node.data.projectId, agentName: name },
        entry,
        "run",
      ),
    );
  }

  // Subagent level: the worker split under an agent (e.g. talon/implement,
  // talon/review, gate). The producers stamp the split on NON-root spans only
  // — talon's `kestrel.stage` / prefixed agent names live on child stage
  // spans, never the run root — so this drill reads ALL spans of the agent,
  // matched by base name plus prefixed variants. Agents with no worker split
  // collapse (pass-through): the SAME fetched page aggregates sessions
  // directly, so the expand still costs exactly one query.
  async function loadAgentDrill(node, mode) {
    const spans = await fetchSpanPage(node, {
      mode,
      rootOnly: false,
      filter: agentFilter(node.data.agentName),
    });
    mergeSpansIntoAgg(ensureAgg(node), ownedByAgent(spans, node.data.agentName));
    if (node.passThrough === undefined) {
      node.passThrough = node.agg.workers.size === 0;
    }
    if (node.passThrough) {
      node.children = sessionChildren(node);
      return;
    }
    const entries = [...node.agg.workers.entries()].sort(
      (a, b) => (b[1].last || 0) - (a[1].last || 0),
    );
    const kids = entries.map(([worker, entry]) =>
      childFor(
        node,
        "subagent",
        `worker:${worker}`,
        `${node.data.agentName}/${worker}`,
        { projectId: node.data.projectId, agentName: node.data.agentName, worker },
        entry,
        "span",
      ),
    );
    if (node.agg.stageless && node.agg.stageless.count) {
      // Worker-less spans under an agent that HAS workers — talon's run roots
      // land here. Keyed apart from the `worker:`-prefixed entries so no
      // worker name can collide.
      kids.push(
        childFor(
          node,
          "subagent",
          "stageless",
          node.data.agentName,
          { projectId: node.data.projectId, agentName: node.data.agentName, worker: null },
          node.agg.stageless,
          "run",
        ),
      );
    }
    node.children = kids;
  }

  // Session level: distinct sessions under an agent/worker, recency-ordered.
  // Worker-keyed drills read ALL spans (the split lives on children); the
  // worker-less bucket reads the agent's run roots. Session identity falls
  // back kestrel.session_id → session.id → kestrel.run_id (see sessionKeyOf).
  async function loadSessions(node, mode) {
    const { agentName, worker } = node.data;
    let spans;
    if (worker != null) {
      spans = await fetchSpanPage(node, {
        mode,
        rootOnly: false,
        filter: workerFilter(agentName, worker),
      });
      spans = ownedByAgent(spans, agentName);
    } else {
      spans = await fetchSpanPage(node, {
        mode,
        rootOnly: true,
        filter: exactAgentFilter(agentName),
      });
    }
    mergeSpansIntoAgg(ensureAgg(node), spans);
    node.children = sessionChildren(node);
  }

  // Turn level: the root spans within a session, time-ordered (session marker +
  // per-interaction/run roots). Live-follow refreshes read the newest page
  // (desc) and merge by span id, so new turns appear without re-paging.
  async function loadTurns(node, mode) {
    const sid = node.data.sessionId;
    // Filter on the attribute that identified this session during
    // aggregation: talon sessions are `kestrel.run_id`-keyed (talon stamps no
    // session attribute), emitter sessions `kestrel.session_id`-keyed.
    const filter = `${attrRef(node.data.sessionAttr || ATTR_SESSION_ID)} == ${dslString(sid)}`;
    const spans = await fetchSpanPage(node, {
      mode,
      rootOnly: true,
      filter,
      dir: mode === "refresh" ? "desc" : "asc",
    });
    if (!node.turns) node.turns = new Map();
    for (const span of spans) {
      if (span && !node.turns.has(span.id)) node.turns.set(span.id, span);
    }
    const ordered = [...node.turns.values()].sort(
      (a, b) => (ts(a.startTime) || 0) - (ts(b.startTime) || 0),
    );
    node.children = ordered.map((span) => {
      const key = `turn:${span.id}`;
      let child = node.childIndex.get(key);
      if (!child) {
        child = makeNode(
          "turn",
          span.name || "(turn)",
          {
            projectId: node.data.projectId,
            traceId: (span.context && span.context.traceId) || null,
            span,
          },
          node,
        );
        node.childIndex.set(key, child);
      }
      child.meta = fmtDuration(span.latencyMs);
      child.status = span.statusCode === "ERROR" ? "error" : "ok";
      return child;
    });
    const first = ordered.length ? ts(ordered[0].startTime) : null;
    const last = ordered.length ? ts(ordered[ordered.length - 1].endTime) : null;
    node.meta = `${plural(ordered.length, "turn")}${last ?? first ? ` · ${relTime(last ?? first)}` : ""}`;
  }

  // Events level: the turn's whole span tree in one query, nested by parent
  // span id and time-ordered — start/stop markers, hook events, tool calls
  // (TOOL), LLM calls (LLM), gates. Nested events expand locally (no query).
  async function loadEvents(node) {
    const data = await gql(TRACE_SPANS_QUERY, {
      projectId: node.data.projectId,
      traceId: node.data.traceId,
      first: TRACE_SPAN_LIMIT,
    });
    const trace = data.node && data.node.trace;
    const spans = (((trace && trace.spans) || {}).edges || [])
      .map((e) => e && e.node)
      .filter(Boolean);

    const bySpanId = new Map();
    for (const s of spans) {
      if (s.context && s.context.spanId) bySpanId.set(s.context.spanId, s);
    }
    const rootSpanId =
      (node.data.span && node.data.span.context && node.data.span.context.spanId) || null;
    const kidsOf = new Map();
    const tops = [];
    for (const s of spans) {
      const sid = s.context && s.context.spanId;
      if (sid && sid === rootSpanId) continue; // the turn row itself
      const pid = s.parentId;
      if (pid && pid !== rootSpanId && bySpanId.has(pid)) {
        if (!kidsOf.has(pid)) kidsOf.set(pid, []);
        kidsOf.get(pid).push(s);
      } else {
        tops.push(s); // parented to the turn root (or orphan) → top level
      }
    }
    const byStart = (a, b) => (ts(a.startTime) || 0) - (ts(b.startTime) || 0);
    tops.sort(byStart);
    for (const list of kidsOf.values()) list.sort(byStart);

    // Duration bars are drawn relative to the turn's own time range.
    let turnStart = ts(node.data.span && node.data.span.startTime);
    let turnEnd = ts(node.data.span && node.data.span.endTime);
    for (const s of spans) {
      const st = ts(s.startTime);
      const en = ts(s.endTime);
      if (st != null && (turnStart == null || st < turnStart)) turnStart = st;
      if (en != null && (turnEnd == null || en > turnEnd)) turnEnd = en;
    }
    const turnSummary = spans.find(
      (span) =>
        span.parentId === rootSpanId &&
        /\bturn\b.*\bsummary\b/i.test(String(span.name || "")),
    );
    node.data.summary = turnSummary ? spanSummaryOf(turnSummary) : null;
    node.data.summaryEndMs = turnSummary ? ts(turnSummary.endTime) : null;

    function eventNode(parent, span) {
      const key = `event:${span.id}`;
      let child = parent.childIndex.get(key);
      if (!child) {
        child = makeNode("event", span.name || "(span)", {}, parent);
        parent.childIndex.set(key, child);
      }
      child.data = {
        projectId: node.data.projectId,
        traceId: node.data.traceId,
        span,
        turnStart,
        turnEnd,
      };
      child.meta = fmtDuration(span.latencyMs);
      child.status = span.statusCode === "ERROR" ? "error" : null;
      const kids = kidsOf.get((span.context && span.context.spanId) || "") || [];
      child.children = kids.map((k) => eventNode(child, k));
      child.expandable = child.children.length > 0;
      child.loaded = true;
      return child;
    }

    node.children = tops.map((s) => eventNode(node, s));
    node.meta = `${plural(spans.length ? spans.length - (rootSpanId && bySpanId.has(rootSpanId) ? 1 : 0) : 0, "event")} · ${fmtDuration(node.data.span && node.data.span.latencyMs)}`;
  }

  // ── Virtualized rows ──
  //
  // The whole tree flattens into one row list with per-row offsets; only the
  // window around the viewport renders. Sub-rows (loading / error / load-more /
  // empty) belong to their node, indented one level deeper.

  let rows = [];
  let totalH = 0;
  let focusedNode = null;
  let selectedNode = null;
  let revealFallback = null;
  let rebuildScheduled = false;

  function rebuildRows() {
    rows = [];
    totalH = 0;
    const push = (row, h) => {
      row.top = totalH;
      row.h = h;
      rows.push(row);
      totalH += h;
    };
    (function walk(node) {
      push({ t: "node", node }, ROW_H);
      if (!node.expanded) return;
      if (node.error) push({ t: "error", node }, ROW_H);
      else if (node.loading && !node.children.length) push({ t: "loading", node }, ROW_H);
      for (const child of node.children) walk(child);
      if (node.loaded && node.hasMore) push({ t: "more", node }, ROW_H);
      if (node.loaded && !node.loading && !node.error && !node.children.length && !node.hasMore) {
        push({ t: "empty", node }, ROW_H);
      }
    })(tenant);
    spacerEl.style.height = `${totalH}px`;
  }

  function scheduleRebuild() {
    if (destroyed || rebuildScheduled) return;
    rebuildScheduled = true;
    requestAnimationFrame(() => {
      rebuildScheduled = false;
      if (destroyed) return;
      rebuildRows();
      render();
    });
  }

  function kindLabel(node) {
    switch (node.kind) {
      case "tenant":
        return "tenant";
      case "project":
        return "fleet";
      case "agent":
        return "agent";
      case "subagent":
        return "worker";
      case "session":
        return "session";
      case "turn":
        return "turn";
      case "event": {
        return spanKindOf(node.data.span);
      }
      default:
        return node.kind;
    }
  }

  function barHtml(node) {
    const { turnStart, turnEnd, span } = node.data;
    const s = ts(span && span.startTime);
    const e = ts(span && span.endTime) ?? s;
    if (turnStart == null || turnEnd == null || s == null || turnEnd <= turnStart) return "";
    const range = turnEnd - turnStart;
    const left = Math.max(0, Math.min(100, ((s - turnStart) / range) * 100));
    const width = Math.max(0.5, Math.min(100 - left, ((e - s) / range) * 100));
    const errCls = node.status === "error" ? " obs-nav__barfill--error" : "";
    return `<span class="obs-nav__bartrack"><span class="obs-nav__barfill${errCls}" style="left:${left}%;width:${width}%"></span></span>`;
  }

  function subRowHtml(row, i, text, cls) {
    const node = row.node;
    return `<div class="obs-nav__row obs-nav__row--${cls}" data-i="${i}" style="top:${row.top}px;height:${row.h}px">
      <span class="obs-nav__indent" style="width:${(node.depth + 1) * 16}px"></span>
      <span class="obs-nav__caret"></span>${text}
    </div>`;
  }

  function nodeRowHtml(row, i) {
    const node = row.node;
    const focused = node === focusedNode;
    const selected = node === selectedNode;
    const caret = node.expandable
      ? `<span class="obs-nav__caret" data-caret>${node.expanded ? "▾" : "▸"}</span>`
      : `<span class="obs-nav__caret"></span>`;
    const pill = `<span class="obs-nav__kind obs-nav__kind--${node.kind}">${escapeHtml(kindLabel(node))}</span>`;
    const statusPill =
      node.status === "error"
        ? `<span class="obs-nav__pill obs-nav__pill--error">error</span>`
        : node.kind === "turn"
          ? `<span class="obs-nav__pill obs-nav__pill--ok">ok</span>`
          : "";
    const bar = node.kind === "event" ? barHtml(node) : "";
    const open =
      node.kind === "turn" && node.data.traceId && openTrace
        ? `<a href="#" class="obs-nav__open" data-open>open in Phoenix</a>`
        : "";
    return `<div class="obs-nav__row obs-nav__row--node${focused ? " obs-nav__row--focused" : ""}${selected ? " obs-nav__row--selected" : ""}" data-i="${i}" role="treeitem" aria-selected="${selected}" aria-expanded="${node.expandable ? String(node.expanded) : "false"}" style="top:${row.top}px;height:${row.h}px">
      <span class="obs-nav__indent" style="width:${node.depth * 16}px"></span>
      ${caret}${pill}
      <span class="obs-nav__label" title="${escapeHtml(node.label)}">${escapeHtml(node.label)}</span>
      ${statusPill}${bar}
      <span class="obs-nav__meta">${escapeHtml(node.meta || "")}</span>
      ${open}
    </div>`;
  }

  function rowHtml(row, i) {
    switch (row.t) {
      case "node":
        return nodeRowHtml(row, i);
      case "loading":
        return subRowHtml(row, i, `<span class="obs-nav__muted">Loading…</span>`, "status");
      case "empty":
        return subRowHtml(row, i, `<span class="obs-nav__muted">No activity</span>`, "status");
      case "more":
        return subRowHtml(row, i, `<span class="obs-nav__more">Load more…</span>`, "more");
      case "error":
        return subRowHtml(
          row,
          i,
          `<span class="obs-nav__error">${escapeHtml(row.node.error || "query failed")}</span>
           <span class="obs-nav__more">retry</span>`,
          "error",
        );
      default:
        return "";
    }
  }

  function render() {
    if (destroyed) return;
    const top = scroller.scrollTop - OVERSCAN_PX;
    const bottom = scroller.scrollTop + scroller.clientHeight + OVERSCAN_PX;
    // Binary search the first visible row (rows are offset-sorted).
    let lo = 0;
    let hi = rows.length - 1;
    let start = rows.length;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (rows[mid].top + rows[mid].h > top) {
        start = mid;
        hi = mid - 1;
      } else {
        lo = mid + 1;
      }
    }
    let html = "";
    for (let i = start; i < rows.length && rows[i].top < bottom; i++) {
      html += rowHtml(rows[i], i);
    }
    spacerEl.innerHTML = html;
  }

  // ── Interaction ──

  function toggleExpand(node) {
    if (!node.expandable) return;
    node.expanded = !node.expanded;
    if (node.expanded && !node.loaded && !node.loading) {
      loadChildren(node, "initial");
    }
    scheduleRebuild();
  }

  function detailForNode(node) {
    if (!node || (node.kind !== "turn" && node.kind !== "event")) return null;
    const context = {};
    let cur = node;
    while (cur) {
      const data = cur.data || {};
      if (context.projectId == null && data.projectId != null) context.projectId = data.projectId;
      if (context.projectName == null && data.projectName != null) {
        context.projectName = data.projectName;
      }
      if (context.agentName == null && data.agentName != null) context.agentName = data.agentName;
      if (context.sessionId == null && data.sessionId != null) context.sessionId = data.sessionId;
      if (context.traceId == null && data.traceId != null) context.traceId = data.traceId;
      cur = cur.parent;
    }
    if (node.kind === "turn" && node.data.summary) {
      context.summary = node.data.summary;
      context.endMs = node.data.summaryEndMs;
    }
    return normalizeSpanDetail(node.data.span, context);
  }

  function renderInspector() {
    if (!inspectorEl) return;
    const detail = detailForNode(selectedNode);
    if (!detail) {
      inspectorEl.innerHTML =
        `<div class="obs-nav__inspector-empty">Select a Turn or Event to inspect its span.</div>`;
      return;
    }
    const fallback = revealFallback
      ? `<div class="obs-nav__fallback">${escapeHtml(revealFallback)}</div>`
      : "";
    const canPhx = Boolean(openTrace && detail.traceId && detail.projectId);
    const canTimeline = Boolean(openTimeline && detail.spanId);
    inspectorEl.innerHTML = `
      <div class="obs-nav__inspector-head">
        <span class="obs-nav__inspector-title" title="${escapeHtml(detail.name)}">${escapeHtml(detail.displayName)}</span>
      </div>
      ${fallback}
      <div class="obs-nav__inspector-body">${renderSpanDetail(detail)}</div>
      <div class="obs-nav__inspector-actions">
        ${canPhx ? `<button type="button" class="obs-nav__action" data-inspector-phoenix>Open in Phoenix</button>` : ""}
        ${canTimeline ? `<button type="button" class="obs-nav__action" data-inspector-timeline>Show in Timeline</button>` : ""}
      </div>`;
  }

  function selectSpanNode(node, fallbackMessage = null) {
    if (!node || (node.kind !== "turn" && node.kind !== "event")) return;
    focusedNode = node;
    selectedNode = node;
    revealFallback = fallbackMessage;
    renderInspector();
    scheduleRebuild();
    if (node.kind === "turn" && !node.loaded && !node.loading) {
      ensureLoaded(node).then(() => {
        if (!destroyed && selectedNode === node) {
          renderInspector();
          scheduleRebuild();
        }
      });
    }
  }

  function activate(node) {
    focusedNode = node;
    if (node.kind === "turn" || node.kind === "event") {
      selectSpanNode(node);
    } else if (node.expandable) {
      toggleExpand(node);
      return;
    }
    scheduleRebuild();
  }

  function openTraceFor(node) {
    if (!openTrace || !node.data.traceId) return;
    // Phoenix's trace route inside the embed: /phoenix/projects/{id}/traces/{traceId}.
    const url = `${PHOENIX_URL}projects/${encodeURIComponent(node.data.projectId)}/traces/${encodeURIComponent(node.data.traceId)}`;
    openTrace(url);
  }

  if (inspectorEl) {
    inspectorEl.addEventListener("click", (e) => {
      const detail = detailForNode(selectedNode);
      if (!detail) return;
      if (e.target.closest("[data-inspector-phoenix]")) {
        openTraceFor(selectedNode);
      } else if (e.target.closest("[data-inspector-timeline]") && openTimeline) {
        openTimeline(buildTimelineRevealTarget(detail));
      }
    });
  }

  spacerEl.addEventListener("click", (e) => {
    const rowEl = e.target.closest("[data-i]");
    if (!rowEl) return;
    const row = rows[Number(rowEl.dataset.i)];
    if (!row) return;
    if (e.target.closest("[data-open]")) {
      e.preventDefault();
      openTraceFor(row.node);
      return;
    }
    if (row.t === "more") {
      loadChildren(row.node, "more");
      return;
    }
    if (row.t === "error") {
      loadChildren(row.node, row.node.loaded ? "more" : "initial");
      return;
    }
    if (row.t !== "node") return;
    focusedNode = row.node;
    // The caret owns hierarchy expansion. Activating the rest of a span-backed
    // row selects it for the persistent inspector.
    if (row.node.expandable && e.target.closest("[data-caret]")) {
      toggleExpand(row.node);
    } else {
      activate(row.node);
    }
  });

  function nodeRowIndex(node) {
    if (!node) return -1;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].t === "node" && rows[i].node === node) return i;
    }
    return -1;
  }

  function ensureRowVisible(i) {
    const row = rows[i];
    if (!row) return;
    if (row.top < scroller.scrollTop) {
      scroller.scrollTop = row.top;
    } else if (row.top + row.h > scroller.scrollTop + scroller.clientHeight) {
      scroller.scrollTop = row.top + row.h - scroller.clientHeight;
    }
  }

  function focusStep(delta) {
    const current = nodeRowIndex(focusedNode);
    let i = current === -1 ? (delta > 0 ? -1 : rows.length) : current;
    while (true) {
      i += delta;
      if (i < 0 || i >= rows.length) return;
      if (rows[i].t === "node") break;
    }
    focusedNode = rows[i].node;
    ensureRowVisible(i);
    render();
  }

  function focusNode(node) {
    focusedNode = node;
    const i = nodeRowIndex(node);
    if (i !== -1) ensureRowVisible(i);
    render();
  }

  scroller.addEventListener("keydown", (e) => {
    const n = focusedNode;
    switch (e.key) {
      case "ArrowDown":
        focusStep(1);
        break;
      case "ArrowUp":
        focusStep(-1);
        break;
      case "ArrowRight":
        if (!n) focusStep(1);
        else if (n.expandable && !n.expanded) toggleExpand(n);
        else if (n.expanded && n.children.length) focusNode(n.children[0]);
        break;
      case "ArrowLeft":
        if (!n) break;
        if (n.expanded) {
          n.expanded = false;
          scheduleRebuild();
        } else if (n.parent) {
          focusNode(n.parent);
        }
        break;
      case "Enter":
      case " ":
        if (n) activate(n);
        break;
      default:
        return;
    }
    e.preventDefault();
  });

  let renderScheduled = false;
  scroller.addEventListener("scroll", () => {
    if (renderScheduled) return;
    renderScheduled = true;
    requestAnimationFrame(() => {
      renderScheduled = false;
      render();
    });
  });
  const onResize = () => render();
  window.addEventListener("resize", onResize);

  // ── Live-follow (10s polling; off by default) ──
  //
  // Each tick refreshes the project counts and re-reads the FIRST page of every
  // expanded aggregated level (dedup by span id makes the merge idempotent), so
  // new sessions/turns appear without disturbing pagination or expansion state.

  async function pollTick(manual) {
    if (destroyed || polling || (!manual && document.hidden)) return;
    polling = true;
    try {
      const targets = [];
      (function collect(node) {
        if (node.expanded && node.loaded && node.kind !== "turn" && node.kind !== "event") {
          targets.push(node);
        }
        for (const child of node.children) collect(child);
      })(tenant);
      for (const node of targets) {
        // A timer tick stops early when live-follow is switched off mid-sweep;
        // a manual Refresh always completes.
        if (destroyed || (!manual && !liveFollow)) break;
        await loadChildren(node, "refresh");
      }
    } catch (_e) {
      /* transient poll errors are non-fatal */
    } finally {
      polling = false;
    }
  }

  function setLive(on) {
    liveFollow = on;
    liveBtn.classList.toggle("obs-nav__btn--on", on);
    liveBtn.setAttribute("aria-pressed", String(on));
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (on) {
      pollTimer = setInterval(pollTick, POLL_MS);
      pollTick();
    }
  }

  liveBtn.addEventListener("click", () => setLive(!liveFollow));
  refreshBtn.addEventListener("click", () => pollTick(true));

  // ── Timeline → Navigator reveal (#54) ──
  //
  // Drill down to a Timeline span's location, paging each lazy ancestor until
  // its identity is resolved. The exact Event is selected by OTel span id (or
  // Phoenix node id when no span id exists). A trace miss stops honestly at its
  // Turn and tells the operator that the exact span was unavailable.

  function pickChild(node, kind, pred) {
    if (!node) return null;
    for (const child of node.children) {
      if (child.kind === kind && pred(child)) return child;
    }
    return null;
  }

  // Expand a node and await its lazy load (idempotent — a node already loaded or
  // loading is left as-is). The loaders populate `node.children` synchronously
  // before their promise resolves, so callers can descend right after awaiting.
  async function ensureLoaded(node) {
    if (!node || !node.expandable) return;
    node.expanded = true;
    if (!node.loaded && !node.loading) {
      await loadChildren(node, "initial");
    }
  }

  async function findChildPaged(node, kind, pred) {
    await ensureLoaded(node);
    let found = pickChild(node, kind, pred);
    let pages = 0;
    while (!found && node.hasMore && !destroyed && pages++ < 1000) {
      await loadChildren(node, "more");
      found = pickChild(node, kind, pred);
    }
    return found;
  }

  async function reveal(target) {
    if (!target || destroyed) return;
    let deepest = tenant;
    let selected = null;
    let fallbackMessage = null;
    try {
      await ensureLoaded(tenant);
      const project = pickChild(
        tenant,
        "project",
        (n) =>
          (target.projectId != null && n.data.projectId === target.projectId) ||
          (target.projectName != null && n.data.projectName === target.projectName),
      );
      if (!project) return;
      deepest = project;

      const agent = await findChildPaged(
        project,
        "agent",
        (n) => n.data.agentName === target.agentName,
      );
      if (!agent) return;
      deepest = agent;
      await ensureLoaded(agent);
      if (target.sessionId == null) return;

      // Sessions sit directly under the agent (pass-through: no worker split) or
      // under a worker subagent. Prefer the span's own worker, then scan the
      // rest — a session lives under exactly one of them.
      let session = null;
      if (agent.passThrough) {
        session = await findChildPaged(
          agent,
          "session",
          (n) => n.data.sessionId === target.sessionId,
        );
      } else {
        // A requested worker may be outside the first aggregation page.
        if (target.worker !== undefined) {
          await findChildPaged(
            agent,
            "subagent",
            (n) => n.data.worker === (target.worker || null),
          );
        }
        const subs = agent.children.filter((c) => c.kind === "subagent");
        subs.sort(
          (a, b) =>
            Number(b.data.worker === target.worker) - Number(a.data.worker === target.worker),
        );
        for (const sub of subs) {
          if (destroyed) return;
          const found = await findChildPaged(
            sub,
            "session",
            (n) => n.data.sessionId === target.sessionId,
          );
          if (found) {
            session = found;
            break;
          }
        }
      }
      if (!session) return;
      deepest = session;

      if (target.traceId) {
        const turn = await findChildPaged(
          session,
          "turn",
          (n) => n.data.traceId === target.traceId,
        );
        if (turn) {
          deepest = turn;
          await ensureLoaded(turn);
          const resolved = resolveExactSpanReveal(turn, target);
          deepest = resolved.node || turn;
          selected = deepest;
          for (const ancestor of resolved.path.slice(0, -1)) {
            if (ancestor.expandable) ancestor.expanded = true;
          }
          if (!resolved.exact && (target.spanId || target.nodeId)) {
            fallbackMessage =
              `Exact span ${target.spanId || target.nodeId} is unavailable; ` +
              `showing its containing Turn.`;
          }
        }
      }
    } catch (_e) {
      /* reveal still focuses the deepest ancestor resolved before the failure */
    } finally {
      if (!destroyed) {
        rebuildRows();
        if (selected) {
          selectSpanNode(selected, fallbackMessage);
          focusNode(selected);
        } else {
          focusNode(deepest);
        }
      }
    }
  }

  // ── Boot ──

  function teardown() {
    destroyed = true;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    window.removeEventListener("resize", onResize);
  }

  // Phoenix down → the same friendly notice as the embed subtab, plus a retry
  // that remounts a fresh instance (this closure's scroller DOM is gone).
  function renderNotice() {
    if (destroyed || !bodyEl) return;
    bodyEl.innerHTML = `
      <div class="obs-notice">
        <div class="obs-notice__title">Phoenix is not running on this host</div>
        <div class="obs-notice__body">
          Install <code>kestrel-sovereign[phoenix]</code> and restart, or set
          <code>KESTREL_PHOENIX_ENABLED=1</code>.
        </div>
        <button type="button" class="obs-nav__btn" data-retry>Retry</button>
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
      // Mint the embed session BEFORE the first query — the cookie authenticates
      // every /phoenix/graphql call the tree makes from here on.
      await mintPhoenixSession();
    } catch (_e) {
      renderNotice();
      return;
    }
    if (destroyed) return;
    // Root is pre-expanded: Tenant → Fleet with real counts on first paint.
    await loadChildren(tenant, "initial");
    // A pending Timeline "open in Navigator" reveal drills in once the fleet
    // level is loaded (#54).
    if (!destroyed && revealTarget) reveal(revealTarget);
  }

  rebuildRows();
  render();
  boot();

  const handleProxy = { destroy: teardown };
  return handleProxy;
}

// ── Styles (scoped, theme-aware — console-native, not Phoenix chrome) ──

let stylesInjected = false;
function ensureStyles() {
  if (stylesInjected || typeof document === "undefined") return;
  const style = document.createElement("style");
  style.setAttribute("data-observability-navigator", "");
  style.textContent = `
    .obs-nav { display:flex; flex-direction:column; height:100%; min-height:0;
               color:var(--color-text,#e2e8f0); font-size:13px; }
    .obs-nav__toolbar { display:flex; align-items:center; gap:8px; padding:6px 12px;
                        border-bottom:1px solid var(--color-border,#334155); }
    .obs-nav__title { font-weight:600; }
    .obs-nav__grow { flex:1; }
    .obs-nav__btn { background:transparent; color:var(--color-text-muted,#94a3b8);
                    border:1px solid var(--color-border,#334155); border-radius:999px;
                    padding:2px 12px; cursor:pointer; font-size:12px; font-weight:600; }
    .obs-nav__btn:hover { background:var(--color-surface,#1e293b); color:var(--color-text,#e2e8f0); }
    .obs-nav__btn--on { background:var(--color-accent,#818cf8); border-color:var(--color-accent,#818cf8);
                        color:#0b1120; }
    .obs-nav__body { flex:1; min-height:0; display:flex; }
    .obs-nav__treepane { flex:1; min-width:0; min-height:0; display:flex; }
    .obs-nav__scroll { flex:1; min-height:0; overflow-y:auto; overflow-x:hidden; outline:none; }
    .obs-nav__scroll:focus-visible { box-shadow:inset 0 0 0 1px var(--color-accent,#818cf8); }
    .obs-nav__spacer { position:relative; }
    .obs-nav__row { position:absolute; left:0; right:0; display:flex; align-items:center;
                    gap:6px; padding:0 12px; box-sizing:border-box; white-space:nowrap; }
    .obs-nav__row--node { cursor:pointer; }
    .obs-nav__row--node:hover { background:var(--color-surface,#1e293b); }
    .obs-nav__row--focused { background:var(--color-surface,#1e293b);
                             box-shadow:inset 2px 0 0 var(--color-accent,#818cf8); }
    .obs-nav__row--selected { background:color-mix(in srgb, var(--color-accent,#818cf8) 14%, transparent);
                              box-shadow:inset 3px 0 0 var(--color-accent,#818cf8); }
    .obs-nav__indent { flex:none; align-self:stretch;
                       background:repeating-linear-gradient(to right,
                         transparent 0 15px, var(--color-border,#334155) 15px 16px); }
    .obs-nav__caret { flex:none; width:14px; text-align:center;
                      color:var(--color-text-muted,#94a3b8); font-size:11px; }
    .obs-nav__kind { flex:none; font-size:10px; font-weight:700; letter-spacing:.04em;
                     text-transform:uppercase; color:var(--color-text-muted,#94a3b8);
                     border:1px solid var(--color-border,#334155); border-radius:4px;
                     padding:0 5px; line-height:16px; }
    .obs-nav__kind--event { color:var(--color-accent,#818cf8); }
    .obs-nav__label { overflow:hidden; text-overflow:ellipsis; min-width:0; }
    .obs-nav__meta { flex:none; margin-left:auto; color:var(--color-text-muted,#94a3b8);
                     font-size:12px; }
    .obs-nav__pill { flex:none; font-size:10px; font-weight:700; border-radius:999px;
                     padding:0 7px; line-height:16px; }
    .obs-nav__pill--ok { color:var(--color-success,#34d399);
                         background:color-mix(in srgb, var(--color-success,#34d399) 15%, transparent); }
    .obs-nav__pill--error { color:var(--color-danger,#f87171);
                            background:color-mix(in srgb, var(--color-danger,#f87171) 15%, transparent); }
    .obs-nav__bartrack { flex:none; position:relative; width:120px; height:6px;
                         border-radius:3px; background:var(--color-surface,#1e293b); }
    .obs-nav__barfill { position:absolute; top:0; bottom:0; border-radius:3px;
                        background:var(--color-accent,#818cf8); }
    .obs-nav__barfill--error { background:var(--color-danger,#f87171); }
    .obs-nav__open { flex:none; font-size:11px; color:var(--color-accent,#818cf8);
                     text-decoration:none; }
    .obs-nav__open:hover { text-decoration:underline; }
    .obs-nav__muted { color:var(--color-text-muted,#94a3b8); }
    .obs-nav__more { color:var(--color-accent,#818cf8); cursor:pointer; font-weight:600; }
    .obs-nav__error { color:var(--color-danger,#f87171); overflow:hidden; text-overflow:ellipsis; }
    .obs-nav__inspector { flex:0 0 min(380px,42%); min-width:280px; min-height:0;
                          display:flex; flex-direction:column;
                          border-left:1px solid var(--color-border,#334155);
                          background:color-mix(in srgb, var(--color-surface,#1e293b) 38%, transparent); }
    .obs-nav__inspector-empty { margin:auto; max-width:230px; padding:24px; text-align:center;
                                color:var(--color-text-muted,#94a3b8); line-height:1.5; }
    .obs-nav__inspector-head { flex:none; padding:10px 12px;
                               border-bottom:1px solid var(--color-border,#334155); }
    .obs-nav__inspector-title { display:block; overflow:hidden; text-overflow:ellipsis;
                                white-space:nowrap; font-weight:700; }
    .obs-nav__fallback { flex:none; padding:8px 12px; color:#f59e0b; font-size:11px;
                         border-bottom:1px solid var(--color-border,#334155); }
    .obs-nav__inspector-body { flex:1; min-height:0; overflow:auto; padding:10px 12px; }
    .obs-nav__inspector-actions { flex:none; display:flex; flex-wrap:wrap; gap:8px; padding:9px 12px;
                                  border-top:1px solid var(--color-border,#334155); }
    .obs-nav__action { background:transparent; border:1px solid var(--color-border,#334155);
                       border-radius:999px; color:var(--color-accent,#818cf8); cursor:pointer;
                       font-size:11px; font-weight:600; padding:3px 10px; }
    .obs-nav__action:hover { background:var(--color-surface,#1e293b); }
    @media (max-width:760px) {
      .obs-nav__body { flex-direction:column; }
      .obs-nav__treepane { min-height:45%; }
      .obs-nav__inspector { flex:1 1 45%; min-width:0; border-left:0;
                            border-top:1px solid var(--color-border,#334155); }
    }
    .obs-nav .obs-notice { flex:1; display:flex; flex-direction:column; align-items:center;
                           justify-content:center; gap:8px; padding:24px; text-align:center;
                           color:var(--color-text-muted,#94a3b8); }
    .obs-nav .obs-notice__title { font-size:15px; font-weight:600; color:var(--color-text,#e2e8f0); }
    .obs-nav .obs-notice__body { max-width:520px; line-height:1.5; }
    .obs-nav .obs-notice code { font-family:ui-monospace,monospace; background:var(--color-surface,#1e293b);
                                border:1px solid var(--color-border,#334155); border-radius:4px;
                                padding:1px 5px; }
  `;
  document.head.appendChild(style);
  stylesInjected = true;
}
