// Shared Phoenix read-model plumbing (#48).
//
// The Fleet Navigator (#46, ./navigator.js) and the realtime Timeline (#48,
// ./timeline.js) are two windows onto the SAME data: Phoenix's spans read over
// the same-origin `/phoenix/graphql` proxy. This module is the single source of
// that plumbing — "factor, don't duplicate":
//
//   - the GraphQL client + embed-cookie mint (mint before the first query,
//     re-mint once on a 401) — one module-level session shared by both views,
//   - the span-page / projects / trace GraphQL documents (schema-validated
//     against Phoenix 17.7.0 in the tests),
//   - the emitter span-attribute contract (the exact keys tracing.py / hook.py
//     stamp) and the pure read-model helpers that decode it (agent / worker /
//     session identity, the span-filter DSL builders, formatting).
//
// Pure and DOM-free — safe to import under node for the read-model tests. The
// only host coupling is the console API client (embed-session mint); everything
// else is plain fetch against the same-origin proxy.

import API from "/js/api.js";

// ── Host / proxy endpoints ────────────────────────────────────
export const PHOENIX_SESSION_PATH = "/api/host/phoenix/session";
export const PHOENIX_URL = "/phoenix/";
export const PHOENIX_GRAPHQL_URL = "/phoenix/graphql";

// The agents' default project (tracing.py DEFAULT_OTEL_PROJECT) — pinned first
// at the Fleet level / as the first timeline project group; repo projects
// (`owner/repo`, the talon claims) follow by name.
export const DEFAULT_PROJECT = "kestrel-fleet";

// ── Emitter span-attribute contract (tracing.py / hook.py) ────
export const ATTR_PROJECT_NAME = "openinference.project.name"; // Resource attr → Phoenix project
export const ATTR_SPAN_KIND = "openinference.span.kind";
export const ATTR_AGENT_NAME = "kestrel.agent_name";
export const ATTR_STAGE = "kestrel.stage";
export const ATTR_SESSION_ID = "kestrel.session_id";
export const ATTR_OI_SESSION_ID = "session.id"; // OpenInference convention (fallback)
export const ATTR_RUN_ID = "kestrel.run_id"; // final session fallback (talon runs)
export const ATTR_INPUT_VALUE = "input.value";
export const ATTR_OUTPUT_VALUE = "output.value";
export const ATTR_MODEL_NAME = "llm.model_name"; // LLM span model, shown in tooltips

// Spans missing kestrel.agent_name bucket here (should be none post-#2602).
export const UNKNOWN_AGENT = "unknown";

export const IO_CLIP = 4000; // chars of input/output shown inline

// ── GraphQL client (embed-cookie auth; mint before first query, re-mint on 401)
//
// Module-level session state: navigator.js and timeline.js import this module,
// so they share ONE minted embed session (one cookie, one 401 re-mint path).

let phoenixSessionMinted = false;

export async function mintPhoenixSession() {
  await API.requestHost(PHOENIX_SESSION_PATH, { method: "POST" });
  phoenixSessionMinted = true;
}

export async function gql(query, variables) {
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

// ── GraphQL documents (schema-validated against Phoenix 17.7.0) ──

// Fleet/project list: the Phoenix projects (spans routed by the
// `openinference.project.name` Resource attribute) with trace counts +
// last-activity.
export const PROJECTS_QUERY = `
  query NavigatorProjects {
    projects(first: 1000) {
      edges { node { id name traceCount endTime } }
    }
  }`;

// One page of recency-ordered spans, optionally windowed by `$timeRange`
// (the Timeline's live/history paging) and/or filtered with Phoenix's
// span-filter DSL (the Navigator's per-level drills — distinct-ness
// aggregated client-side). `$rootOnly` is per-caller: Navigator's
// Agents/Turns read roots; the Subagent/Session drills and the whole Timeline
// read all spans. Phoenix (17.7.0) exposes `node(id:)` as plain `ID!` — it has
// no `GlobalID` scalar; the documents here are schema-validated in the tests.
export const SPAN_PAGE_QUERY = `
  query NavigatorSpanPage($projectId: ID!, $first: Int!, $after: String, $filter: String, $rootOnly: Boolean!, $sort: SpanSort, $timeRange: TimeRange) {
    node(id: $projectId) {
      ... on Project {
        spans(first: $first, after: $after, filterCondition: $filter, rootSpansOnly: $rootOnly, sort: $sort, timeRange: $timeRange) {
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
export const TRACE_SPANS_QUERY = `
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

export function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function parseAttributes(raw) {
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
export function getAttr(attrs, key) {
  if (!attrs || typeof attrs !== "object") return undefined;
  if (key in attrs) return attrs[key];
  let cur = attrs;
  for (const part of key.split(".")) {
    if (!cur || typeof cur !== "object" || !(part in cur)) return undefined;
    cur = cur[part];
  }
  return cur;
}

// Phoenix span-filter DSL building blocks (Python-expression syntax). Phoenix
// stores dotted OTel attribute keys NESTED (see getAttr above) and the filter
// DSL matches only nested subscripts — verified live on 17.7.0, a flat
// `attributes["kestrel.agent_name"]` ref silently matches nothing (#50) — so
// each dot segment becomes its own subscript (`kestrel.agent_name` →
// `attributes["kestrel"]["agent_name"]`); dotless keys are unchanged.
export function attrRef(key) {
  const parts = String(key).split(".");
  let ref = `attributes[${JSON.stringify(parts[0])}]`;
  for (const part of parts.slice(1)) ref += `[${JSON.stringify(part)}]`;
  return ref;
}

export function dslString(value) {
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

export function ts(iso) {
  const t = Date.parse(iso || "");
  return Number.isFinite(t) ? t : null;
}

export function relTime(ms) {
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

export function fmtDuration(msLike) {
  const ms = Number(msLike);
  if (msLike == null || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function clip(text) {
  const s = String(text);
  return s.length > IO_CLIP ? `${s.slice(0, IO_CLIP)}\n… (truncated)` : s;
}

// ── Producer-shape read-model (agent / worker / session identity) ──
//
// Phoenix has no attribute group-by API, so the Agent / Subagent / Session
// levels aggregate client-side from pages of spans. The shape follows what the
// producers REALLY stamp (hook.py here; talon via tracing.py).

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
// attribute key is kept so callers can filter on that same attribute.
export function sessionKeyOf(attrs) {
  for (const attrKey of [ATTR_SESSION_ID, ATTR_OI_SESSION_ID, ATTR_RUN_ID]) {
    const value = getAttr(attrs, attrKey);
    if (value != null && value !== "") return { id: String(value), attrKey };
  }
  return null;
}

// The span-kind (`openinference.span.kind`) — TOOL / LLM / CHAIN / AGENT … —
// normalized upper-case, preferring the GraphQL `spanKind` column and falling
// back to the attribute. Drives block color on the Timeline and the kind pill
// on the Navigator.
export function spanKindOf(span) {
  const k =
    (span && span.spanKind) ||
    getAttr(parseAttributes(span && span.attributes), ATTR_SPAN_KIND);
  return String(k || "span").toUpperCase();
}
