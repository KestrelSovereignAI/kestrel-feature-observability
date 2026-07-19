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
//     TOOL / LLM / CHAIN / AGENT …), with `input.value` / `output.value`
//     revealed inline for LLM events.
//
// Levels load LAZILY: expanding a node fires exactly ONE paginated GraphQL
// query for that level. Phoenix has no attribute group-by API, so the Agent /
// Subagent / Session distinct-ness is aggregated client-side from one page of
// recency-ordered spans — root-only where the level reads roots (Agents,
// Turns), all spans where the split lives on children (Subagent / Session
// under an agent). "Load more" merges further pages; span ids are deduped so
// overlapping pages never double-count. Tenant is a single static root until
// Castle tenancy lands (label read from host config when available).
//
// The whole tree lives in ONE virtualized scroll container (windowed rows with
// per-row offsets — hundreds of sessions stay smooth); expanded levels append
// inline with indentation guides, never modal/page navigation. Live-follow
// (off by default) polls every 10s, refreshing counts and prepending new
// sessions/turns. Keyboard: ↑/↓ move, → expand/descend, ← collapse/ascend,
// Enter/Space toggles. Phoenix down → the same friendly notice as the embed.
// Styles are console-native (dark/light aware) — kestrel chrome, not Phoenix's.

import API from "/js/api.js";

const PHOENIX_SESSION_PATH = "/api/host/phoenix/session";
const PHOENIX_URL = "/phoenix/";
const PHOENIX_GRAPHQL_URL = "/phoenix/graphql";
// Best-effort tenant label source; any failure keeps the static root label.
const HOST_CONFIG_PATH = "/api/host/config";

// The agents' project (tracing.py DEFAULT_OTEL_PROJECT) — pinned first at the
// Fleet level; repo projects (`owner/repo`, the talon claims) follow by name.
const DEFAULT_PROJECT = "kestrel-fleet";

// ── Emitter span-attribute contract (tracing.py / hook.py) ────
const ATTR_PROJECT_NAME = "openinference.project.name"; // Resource attr → Phoenix project
const ATTR_SPAN_KIND = "openinference.span.kind";
const ATTR_AGENT_NAME = "kestrel.agent_name";
const ATTR_STAGE = "kestrel.stage";
const ATTR_SESSION_ID = "kestrel.session_id";
const ATTR_OI_SESSION_ID = "session.id"; // OpenInference convention (fallback)
const ATTR_RUN_ID = "kestrel.run_id"; // final session fallback (talon runs)
const ATTR_INPUT_VALUE = "input.value";
const ATTR_OUTPUT_VALUE = "output.value";

// Spans missing kestrel.agent_name bucket here (should be none post-#2602).
const UNKNOWN_AGENT = "unknown";

const PAGE_SIZE = 100; // root spans per lazy page (client-side aggregation window)
const TRACE_SPAN_LIMIT = 1000; // events per turn (one trace)
const POLL_MS = 10_000; // live-follow cadence
const IO_CLIP = 4000; // chars of input/output shown inline

// Virtualization: fixed-height rows, taller inline I/O detail rows.
const ROW_H = 28;
const DETAIL_H = 192;
const OVERSCAN_PX = 200;

// ── GraphQL client (embed-cookie auth; mint before first query, re-mint on 401)

let phoenixSessionMinted = false;

async function mintPhoenixSession() {
  await API.requestHost(PHOENIX_SESSION_PATH, { method: "POST" });
  phoenixSessionMinted = true;
}

async function gql(query, variables) {
  if (!phoenixSessionMinted) await mintPhoenixSession();
  const post = () =>
    fetch(PHOENIX_GRAPHQL_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables: variables || {} }),
    });
  let resp = await post();
  if (resp.status === 401) {
    // Embed cookie expired → re-mint once and retry.
    phoenixSessionMinted = false;
    await mintPhoenixSession();
    resp = await post();
  }
  if (!resp.ok) throw new Error(`phoenix graphql HTTP ${resp.status}`);
  const payload = await resp.json();
  if (payload && Array.isArray(payload.errors) && payload.errors.length) {
    throw new Error(payload.errors[0].message || "phoenix graphql error");
  }
  return (payload && payload.data) || {};
}

// Fleet level: the Phoenix projects (spans routed by the
// `openinference.project.name` Resource attribute) with trace counts +
// last-activity.
const PROJECTS_QUERY = `
  query NavigatorProjects {
    projects(first: 1000) {
      edges { node { id name traceCount endTime } }
    }
  }`;

// One page of recency-ordered spans, optionally filtered with Phoenix's
// span-filter DSL — the single query shape behind the Agent / Subagent /
// Session / Turn levels (distinct-ness aggregated client-side). `$rootOnly`
// is per-level: Agents/Turns read roots; the Subagent/Session drills read all
// spans because the producers stamp the worker split on children only.
// Phoenix (17.7.0) exposes `node(id:)` as plain `ID!` — it has no `GlobalID`
// scalar; the documents here are schema-validated in the tests.
const SPAN_PAGE_QUERY = `
  query NavigatorSpanPage($projectId: ID!, $first: Int!, $after: String, $filter: String, $rootOnly: Boolean!, $sort: SpanSort) {
    node(id: $projectId) {
      ... on Project {
        spans(first: $first, after: $after, filterCondition: $filter, rootSpansOnly: $rootOnly, sort: $sort) {
          pageInfo { hasNextPage endCursor }
          edges {
            node {
              id name spanKind startTime endTime latencyMs statusCode parentId attributes
              context { spanId traceId }
            }
          }
        }
      }
    }
  }`;

// Events level: the full span tree of one turn (= one trace).
const TRACE_SPANS_QUERY = `
  query NavigatorTraceSpans($projectId: ID!, $traceId: ID!, $first: Int!) {
    node(id: $projectId) {
      ... on Project {
        trace(traceId: $traceId) {
          spans(first: $first) {
            edges {
              node {
                id name spanKind startTime endTime latencyMs statusCode parentId attributes
                context { spanId traceId }
              }
            }
          }
        }
      }
    }
  }`;

// ── Attribute / formatting helpers ────────────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function parseAttributes(raw) {
  if (raw && typeof raw === "object") return raw;
  try {
    return JSON.parse(raw) || {};
  } catch (_e) {
    return {};
  }
}

// Phoenix unflattens OTLP attribute keys into nested JSON
// (`{"kestrel": {"agent_name": …}}`); accept the flat dotted key too so either
// serialization works.
function getAttr(attrs, key) {
  if (!attrs || typeof attrs !== "object") return undefined;
  if (key in attrs) return attrs[key];
  let cur = attrs;
  for (const part of key.split(".")) {
    if (!cur || typeof cur !== "object" || !(part in cur)) return undefined;
    cur = cur[part];
  }
  return cur;
}

// Phoenix span-filter DSL building blocks (Python-expression syntax).
function attrRef(key) {
  return `attributes[${JSON.stringify(key)}]`;
}

function dslString(value) {
  return `'${String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
}

// Exact-name spans only (run roots; no prefixed worker variants). `unknown`
// covers the emitter's literal fallback name; attribute-less foreign spans
// (should be none post-#2602) stay listed but un-drillable.
export function exactAgentFilter(agentName) {
  return `${attrRef(ATTR_AGENT_NAME)} == ${dslString(agentName)}`;
}

// Spans of `agentName` INCLUDING its prefixed worker variants: talon's child
// stage spans carry `kestrel.agent_name == "talon/implement"` etc., which an
// exact match excludes — a drill filtered that way never sees the worker
// split. The DSL's `in` is substring containment (Phoenix `TextContains`),
// not anchored, so the rare over-match (`xtalon/…`) is dropped client-side
// during aggregation (see `ownedByAgent`).
export function agentFilter(agentName) {
  return `(${exactAgentFilter(agentName)} or ${dslString(`${agentName}/`)} in ${attrRef(ATTR_AGENT_NAME)})`;
}

// One worker under an agent, matched the same two ways the producers stamp
// the split: an explicit `kestrel.stage`, or the prefixed agent-name variant.
export function workerFilter(agentName, worker) {
  return (
    `${agentFilter(agentName)} and (${attrRef(ATTR_STAGE)} == ${dslString(worker)}` +
    ` or ${attrRef(ATTR_AGENT_NAME)} == ${dslString(`${agentName}/${worker}`)})`
  );
}

function ts(iso) {
  const t = Date.parse(iso || "");
  return Number.isFinite(t) ? t : null;
}

function relTime(ms) {
  if (!Number.isFinite(ms)) return "";
  const delta = Date.now() - ms;
  if (delta < 0) return "now";
  const s = Math.round(delta / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function fmtDuration(msLike) {
  const ms = Number(msLike);
  if (msLike == null || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function clip(text) {
  const s = String(text);
  return s.length > IO_CLIP ? `${s.slice(0, IO_CLIP)}\n… (truncated)` : s;
}

// {count, first, last, errored} rollup entries for the aggregated levels.
function bumpEntry(entry, start, end, errored) {
  const e = entry || { count: 0, first: null, last: null, errored: false };
  e.count += 1;
  if (start != null && (e.first == null || start < e.first)) e.first = start;
  if (end != null && (e.last == null || end > e.last)) e.last = end;
  if (errored) e.errored = true;
  return e;
}

function bump(map, key, start, end, errored) {
  map.set(key, bumpEntry(map.get(key), start, end, errored));
}

// ── Read-model aggregation (pure; exported for the node-run tests) ──
//
// Phoenix has no attribute group-by API, so the Agent / Subagent / Session
// levels aggregate client-side from pages of spans. The shape follows what
// the producers REALLY stamp (hook.py here; talon via tracing.py):
//
//   - emitter sessions: the root marker + summary carry `kestrel.agent_name`
//     + `kestrel.session_id`; tool children carry the agent name only.
//   - talon runs: the run ROOT carries `kestrel.agent_name == "talon"` +
//     `kestrel.run_id`; the worker split (`kestrel.stage`, prefixed
//     `talon/implement` agent names) lives on child stage spans only; and NO
//     talon span stamps a session attribute — the run id is the session.

// `talon/implement` → `talon`: prefixed worker variants group under their
// base agent so workers never surface as separate Agent-level entries.
export function baseAgentName(name) {
  const s = String(name);
  const i = s.indexOf("/");
  return i > 0 ? s.slice(0, i) : s;
}

// The worker (Subagent-level key) a span belongs to: an explicit
// `kestrel.stage`, else the suffix of a prefixed `kestrel.agent_name`
// (`talon/review` → `review`); null → no worker split (plain run spans).
export function workerOf(attrs) {
  const stage = getAttr(attrs, ATTR_STAGE);
  if (stage != null && stage !== "") return String(stage);
  const agent = getAttr(attrs, ATTR_AGENT_NAME);
  if (agent == null) return null;
  const s = String(agent);
  const i = s.indexOf("/");
  return i > 0 && i < s.length - 1 ? s.slice(i + 1) : null;
}

// Session identity, in producer priority order. Talon stamps neither session
// attribute, so its per-run `kestrel.run_id` (on every talon span) is the
// final fallback — each talon run groups as one session. The winning
// attribute key is kept so the Turn level filters on that same attribute.
export function sessionKeyOf(attrs) {
  for (const attrKey of [ATTR_SESSION_ID, ATTR_OI_SESSION_ID, ATTR_RUN_ID]) {
    const value = getAttr(attrs, attrKey);
    if (value != null && value !== "") return { id: String(value), attrKey };
  }
  return null;
}

export function createAgg() {
  return {
    seen: new Set(), // span ids — dedupes overlapping refresh/more pages
    agents: new Map(), // base agent name → rollup
    workers: new Map(), // worker (stage / prefixed-name suffix) → rollup
    stageless: null, // rollup of spans with no worker split (e.g. run roots)
    sessions: new Map(), // session id → rollup + {attrKey, roots}
  };
}

export function mergeSpansIntoAgg(agg, spans) {
  for (const span of spans) {
    if (!span || agg.seen.has(span.id)) continue;
    agg.seen.add(span.id);
    const attrs = parseAttributes(span.attributes);
    const start = ts(span.startTime);
    const end = ts(span.endTime) ?? start;
    const errored = span.statusCode === "ERROR";

    const agent = getAttr(attrs, ATTR_AGENT_NAME);
    bump(
      agg.agents,
      agent != null && agent !== "" ? baseAgentName(agent) : UNKNOWN_AGENT,
      start,
      end,
      errored,
    );

    const worker = workerOf(attrs);
    if (worker) bump(agg.workers, worker, start, end, errored);
    else agg.stageless = bumpEntry(agg.stageless, start, end, errored);

    const sess = sessionKeyOf(attrs);
    if (sess) {
      bump(agg.sessions, sess.id, start, end, errored);
      const entry = agg.sessions.get(sess.id);
      if (!entry.attrKey) entry.attrKey = sess.attrKey;
      // Root spans are the session's turns; children only pad the span count.
      if (span.parentId == null) entry.roots = (entry.roots || 0) + 1;
    }
  }
}

// ── View / mount ──────────────────────────────────────────────

export function mount(container, opts = {}) {
  ensureStyles();

  const openTrace = typeof opts.openTrace === "function" ? opts.openTrace : null;

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
      detailOpen: false, // event level: inline input/output reveal
      io: null,
    };
  }

  // Single static tenant root until Castle tenancy lands; then roots enumerate
  // tenants. Label upgraded from host config below when available.
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
        <div class="obs-nav__scroll" data-scroll tabindex="0" role="tree" aria-label="Fleet navigator">
          <div class="obs-nav__spacer" data-spacer></div>
        </div>
      </div>
    </div>`;

  const bodyEl = container.querySelector("[data-body]");
  const scroller = container.querySelector("[data-scroll]");
  const spacerEl = container.querySelector("[data-spacer]");
  const liveBtn = container.querySelector("[data-live]");
  const refreshBtn = container.querySelector("[data-refresh]");

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
      const attrs = parseAttributes(span.attributes);
      const input = getAttr(attrs, ATTR_INPUT_VALUE);
      const output = getAttr(attrs, ATTR_OUTPUT_VALUE);
      child.io = input != null || output != null ? { input, output } : null;
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
  // detail) belong to their node, indented one level deeper.

  let rows = [];
  let totalH = 0;
  let focusedNode = null;
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
      if (node.detailOpen && node.io) push({ t: "detail", node }, DETAIL_H);
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
        const span = node.data.span;
        const k =
          (span && span.spanKind) ||
          getAttr(parseAttributes(span && span.attributes), ATTR_SPAN_KIND);
        return String(k || "span").toUpperCase();
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
    const ioHint =
      node.kind === "event" && node.io
        ? `<span class="obs-nav__iohint">${node.detailOpen ? "hide i/o" : "i/o"}</span>`
        : "";
    const bar = node.kind === "event" ? barHtml(node) : "";
    const open =
      node.kind === "turn" && node.data.traceId && openTrace
        ? `<a href="#" class="obs-nav__open" data-open>open in Phoenix</a>`
        : "";
    return `<div class="obs-nav__row obs-nav__row--node${focused ? " obs-nav__row--focused" : ""}" data-i="${i}" role="treeitem" aria-expanded="${node.expandable ? String(node.expanded) : "false"}" style="top:${row.top}px;height:${row.h}px">
      <span class="obs-nav__indent" style="width:${node.depth * 16}px"></span>
      ${caret}${pill}
      <span class="obs-nav__label" title="${escapeHtml(node.label)}">${escapeHtml(node.label)}</span>
      ${statusPill}${ioHint}${bar}
      <span class="obs-nav__meta">${escapeHtml(node.meta || "")}</span>
      ${open}
    </div>`;
  }

  function detailRowHtml(row, i) {
    const node = row.node;
    const io = node.io || {};
    const block = (label, value) =>
      value == null
        ? ""
        : `<div class="obs-nav__io">
            <div class="obs-nav__io-label">${escapeHtml(label)}</div>
            <pre class="obs-nav__io-pre">${escapeHtml(clip(value))}</pre>
          </div>`;
    return `<div class="obs-nav__row obs-nav__row--detail" data-i="${i}" style="top:${row.top}px;height:${row.h}px">
      <span class="obs-nav__indent" style="width:${(node.depth + 1) * 16}px"></span>
      <div class="obs-nav__iowrap">
        ${block(ATTR_INPUT_VALUE, io.input)}
        ${block(ATTR_OUTPUT_VALUE, io.output)}
      </div>
    </div>`;
  }

  function rowHtml(row, i) {
    switch (row.t) {
      case "node":
        return nodeRowHtml(row, i);
      case "detail":
        return detailRowHtml(row, i);
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

  function activate(node) {
    focusedNode = node;
    if (node.kind === "event" && node.io) {
      node.detailOpen = !node.detailOpen;
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
    // An event row with BOTH children and I/O: the caret expands, the row
    // toggles the inline prompt/response reveal.
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
        else if (n.kind === "event" && n.io && !n.detailOpen) {
          n.detailOpen = true;
          scheduleRebuild();
        }
        break;
      case "ArrowLeft":
        if (!n) break;
        if (n.detailOpen) {
          n.detailOpen = false;
          scheduleRebuild();
        } else if (n.expanded) {
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
    loadChildren(tenant, "initial");
    // Best-effort tenant label from host config; static root label otherwise.
    try {
      const cfg = await API.requestHost(HOST_CONFIG_PATH);
      const label = cfg && (cfg.deployment_name || cfg.host_name || cfg.name);
      if (label && !destroyed) {
        tenant.label = String(label);
        scheduleRebuild();
      }
    } catch (_e) {
      /* no host label — keep the static root */
    }
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
    .obs-nav__body { flex:1; min-height:0; display:flex; flex-direction:column; }
    .obs-nav__scroll { flex:1; min-height:0; overflow-y:auto; overflow-x:hidden; outline:none; }
    .obs-nav__scroll:focus-visible { box-shadow:inset 0 0 0 1px var(--color-accent,#818cf8); }
    .obs-nav__spacer { position:relative; }
    .obs-nav__row { position:absolute; left:0; right:0; display:flex; align-items:center;
                    gap:6px; padding:0 12px; box-sizing:border-box; white-space:nowrap; }
    .obs-nav__row--node { cursor:pointer; }
    .obs-nav__row--node:hover { background:var(--color-surface,#1e293b); }
    .obs-nav__row--focused { background:var(--color-surface,#1e293b);
                             box-shadow:inset 2px 0 0 var(--color-accent,#818cf8); }
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
    .obs-nav__iohint { flex:none; font-size:11px; color:var(--color-accent,#818cf8); }
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
    .obs-nav__row--detail { align-items:stretch; padding-top:4px; padding-bottom:4px; }
    .obs-nav__iowrap { flex:1; min-width:0; display:flex; gap:8px; }
    .obs-nav__io { flex:1; min-width:0; display:flex; flex-direction:column; gap:2px; }
    .obs-nav__io-label { font-size:10px; font-weight:700; letter-spacing:.04em;
                         text-transform:uppercase; color:var(--color-text-muted,#94a3b8); }
    .obs-nav__io-pre { flex:1; min-height:0; margin:0; overflow:auto; white-space:pre-wrap;
                       word-break:break-word; font-family:ui-monospace,monospace; font-size:11px;
                       background:var(--color-surface,#1e293b);
                       border:1px solid var(--color-border,#334155); border-radius:6px; padding:6px 8px;
                       color:var(--color-text,#e2e8f0); }
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
