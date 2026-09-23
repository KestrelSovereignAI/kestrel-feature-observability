// Governance lifecycle read-model — Stop, Hold and Resume receipts (#118).
//
// A cooperative Stop used to read as a span that simply ended, and a held agent
// as an idle or disconnected one: the durable governance receipts were nowhere
// in the picture, so operators inferred lifecycle state from missing telemetry.
// This module is the ONE place both views (Timeline, Navigator) learn that
// state, so they cannot disagree about it (kestrel-sovereign#3159, R1–R6):
//
//   - The RECEIPTS decide what happened; spans are only matched to them. The
//     feeds are the sovereign-gated `GET /api/host/stop/receipts` and
//     `GET /api/host/hold/receipts`, paged on `feed_seq` (the `cursor` the core
//     issues — a commit-ordered key, so a later commit can never land behind a
//     page already read), plus `GET /api/host/hold` for the CURRENT latches.
//   - A turn-scope Stop joins its turn by EXACT `(trace_id, span_id)` (and, via
//     the canonical `kestrel.turn_id`, every span of that same turn). An agent-
//     or host-scope Stop joins the agent lane by DID at `occurred_at`: agent
//     scope names its DID in `target_agent_id`, a host fan-out names one per
//     outcome. A receipt with no DID renders "agent not recorded" — the agent is
//     never inferred.
//   - How a turn is shown is a PURE function of (spans, receipts, latches):
//     receipts are immutable and keyed by `receipt_id`, every list is re-sorted
//     here, so arrival order, duplicate pages, late receipts and a reload all
//     converge on the identical model.
//   - A `schema_version` this module does not know renders as an "unrecognized
//     receipt" and is never used to classify anything.
//   - Receipt reason and actor are operator-written and sovereign-gated: they
//     are rendered here through `escapeHtml` + the shared `clip`, and are never
//     copied into a span, a persisted view state, a URL or a log line.
//
// Pure and DOM-free except for `createLifecycleFeed`, whose only I/O is the
// injected host request (the console API client by default).

import { requestHost, escapeHtml, clip, getAttr, ATTR_TURN_OUTCOME } from "./phoenix.js";

// ── Wire contract (kestrel-sovereign#3323) ────────────────────
export const RECEIPT_SCHEMA_VERSION = 1;
export const STOP_RECEIPTS_PATH = "/api/host/stop/receipts";
export const HOLD_RECEIPTS_PATH = "/api/host/hold/receipts";
export const HOLD_STATE_PATH = "/api/host/hold";
export const RECEIPT_PAGE_LIMIT = 200; // the feeds' hard page cap
export const MAX_RECEIPT_PAGES = 5; // per feed per poll; the rest resumes on the next poll
// A feed with pages still past its cursor keeps draining on its own, this long
// after each poll, rather than waiting for a view tick that may never come.
export const RECEIPT_BACKLOG_MS = 250;

export const FEED_STOP = "stop";
export const FEED_HOLD = "hold";

const HOST_HOLD_TARGET = "host";
const STOP_SCOPES = new Set(["host", "agent", "turn", "tool_call"]);
const STOP_DISPOSITIONS = new Set(["stopped", "already_complete", "refused", "unreachable"]);
const HOLD_SCOPES = new Set(["host", "agent"]);
const HOLD_ACTIONS = new Set(["hold", "release"]);
const HOLD_DISPOSITIONS = new Set(["applied", "already_in_state", "refused_stale"]);

// `kestrel.turn.outcome` values core computes (R4). Anything else is shown as
// unrecognized rather than mapped onto one of these.
export const TURN_OUTCOMES = Object.freeze([
  "completed",
  "failed",
  "stopped",
  "disconnected",
  "interrupted",
]);
const KNOWN_OUTCOMES = new Set(TURN_OUTCOMES);

// Every lifecycle state a turn can render as, with its label and tone. The tone
// is the styling key both views use — only `failed` is the error tone.
export const TURN_STATES = Object.freeze({
  stopped: { label: "stopped", tone: "stopped" },
  stopped_pending: { label: "stopped (receipt pending)", tone: "pending" },
  completed: { label: "completed", tone: "ok" },
  failed: { label: "failed", tone: "error" },
  disconnected: { label: "disconnected", tone: "disconnected" },
  interrupted: { label: "interrupted", tone: "interrupted" },
  unreachable: { label: "stop unreachable", tone: "unreachable" },
  refused: { label: "stop refused", tone: "refused" },
  unrecognized_outcome: { label: "unrecognized outcome", tone: "unrecognized" },
});

export const MARKER_AFTER_COMPLETION = "Stop arrived after completion";
export const MARKER_REFUSED = "Stop refused";
export const MARKER_UNREACHABLE = "Stop unreachable";
export const AGENT_NOT_RECORDED = "agent not recorded";

// ── Small value helpers ───────────────────────────────────────

function text(value) {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

// `feed_seq` is a signed 64-bit integer — past 2^53 a JS number cannot hold it,
// so it is kept as its canonical decimal string and compared by length first.
function seqOf(value) {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value >= 1 ? String(value) : null;
  }
  if (typeof value !== "string" || !/^\d{1,19}$/.test(value)) return null;
  const canonical = value.replace(/^0+/, "");
  return canonical === "" ? null : canonical;
}

export function compareSeq(a, b) {
  if (a === b) return 0;
  if (a == null) return -1;
  if (b == null) return 1;
  return a.length - b.length || (a < b ? -1 : 1);
}

function timeOf(value) {
  if (typeof value !== "string") return null;
  const t = Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

function cmpText(a, b) {
  const x = String(a ?? "");
  const y = String(b ?? "");
  return x < y ? -1 : x > y ? 1 : 0;
}

// The one receipt order every list uses: the displayed time, then the commit-
// ordered feed position, then the id — total over immutable rows.
function cmpReceipts(a, b) {
  return (
    (a.atMs ?? 0) - (b.atMs ?? 0) ||
    compareSeq(a.feedSeq, b.feedSeq) ||
    cmpText(a.feed, b.feed) ||
    cmpText(a.receiptId, b.receiptId)
  );
}

// ── Normalizers (schema_version 1) — null on any contract violation ──

function normalizeStopOutcome(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (!Number.isInteger(raw.ordinal) || !STOP_DISPOSITIONS.has(raw.disposition)) return null;
  return {
    ordinal: raw.ordinal,
    disposition: raw.disposition,
    detail: text(raw.detail),
    agentId: text(raw.agent_id),
    resolvedTarget: text(raw.resolved_target),
  };
}

export function normalizeStopReceipt(raw) {
  if (!raw || typeof raw !== "object") return null;
  const receiptId = text(raw.receipt_id);
  const feedSeq = seqOf(raw.feed_seq);
  const atMs = timeOf(raw.occurred_at);
  if (!receiptId || !feedSeq || atMs == null || !STOP_SCOPES.has(raw.scope)) return null;
  if (!Array.isArray(raw.outcomes)) return null;
  const outcomes = [];
  for (const o of raw.outcomes) {
    const outcome = normalizeStopOutcome(o);
    if (!outcome) return null;
    outcomes.push(outcome);
  }
  outcomes.sort((a, b) => a.ordinal - b.ordinal);
  return {
    feed: FEED_STOP,
    feedSeq,
    receiptId,
    scope: raw.scope,
    actorId: text(raw.actor_id),
    targetAgentId: text(raw.target_agent_id),
    reason: text(raw.reason),
    cascade: typeof raw.cascade === "boolean" ? raw.cascade : null,
    occurredAt: raw.occurred_at,
    atMs,
    traceId: text(raw.trace_id),
    spanId: text(raw.span_id),
    outcomes,
  };
}

export function normalizeHoldReceipt(raw) {
  if (!raw || typeof raw !== "object") return null;
  const receiptId = text(raw.receipt_id);
  const feedSeq = seqOf(raw.feed_seq);
  const atMs = timeOf(raw.occurred_at);
  const targetId = text(raw.target_id);
  if (!receiptId || !feedSeq || atMs == null || !targetId) return null;
  if (!HOLD_SCOPES.has(raw.scope) || !HOLD_ACTIONS.has(raw.action)) return null;
  if (!HOLD_DISPOSITIONS.has(raw.disposition)) return null;
  if (raw.scope === "host" && targetId !== HOST_HOLD_TARGET) return null;
  return {
    feed: FEED_HOLD,
    feedSeq,
    receiptId,
    operationId: text(raw.operation_id),
    action: raw.action,
    disposition: raw.disposition,
    scope: raw.scope,
    targetId,
    reason: text(raw.reason),
    actorId: text(raw.actor_id),
    occurredAt: raw.occurred_at,
    atMs,
    expectedHoldReceiptId: text(raw.expected_hold_receipt_id),
    priorHoldReceiptId: text(raw.prior_hold_receipt_id),
    resultingHoldReceiptId: text(raw.resulting_hold_receipt_id),
  };
}

function normalizeLatch(raw, scope) {
  if (raw == null) return null;
  if (typeof raw !== "object") return undefined;
  const holdReceiptId = text(raw.hold_receipt_id);
  const targetId = text(raw.target_id);
  if (raw.scope !== scope || !holdReceiptId || !targetId) return undefined;
  return {
    scope,
    targetId,
    reason: text(raw.reason),
    actorId: text(raw.actor_id),
    setAt: typeof raw.set_at === "string" ? raw.set_at : null,
    atMs: timeOf(raw.set_at),
    holdReceiptId,
    revision: Number.isInteger(raw.revision) ? raw.revision : null,
  };
}

// `GET /api/host/hold`: the CURRENT latches — host plus one entry per hosted
// agent (held or not). Returns null when the payload breaks that shape.
export function normalizeHoldState(raw) {
  if (!raw || typeof raw !== "object" || !Array.isArray(raw.agents)) return null;
  const host = normalizeLatch(raw.host_hold, "host");
  if (host === undefined) return null;
  const agents = [];
  for (const a of raw.agents) {
    if (!a || typeof a !== "object") return null;
    const agentId = text(a.agent_id);
    const agentHold = normalizeLatch(a.agent_hold, "agent");
    if (!agentId || typeof a.held !== "boolean" || agentHold === undefined) return null;
    const sources = Array.isArray(a.sources)
      ? a.sources.filter((s) => HOLD_SCOPES.has(s)).sort()
      : [];
    agents.push({ agentId, held: a.held, sources, agentHold });
  }
  agents.sort((a, b) => cmpText(a.agentId, b.agentId));
  return { host, agents };
}

// The `kestrel.turn.outcome` a span itself reports, else null.
export function spanTurnOutcome(attrs) {
  const v = getAttr(attrs, ATTR_TURN_OUTCOME);
  return v == null || v === "" ? null : String(v);
}

// ── Store + ingestion (pure) ──────────────────────────────────

function feedState() {
  // status: "pending" (never read) | "ok" | "unavailable" | "unrecognized" | "malformed"
  // more: the newest page read said the feed has rows past `cursor`.
  return { cursor: null, status: "pending", detail: null, more: false };
}

export function createLifecycleStore() {
  return {
    stop: new Map(), // receipt_id → normalized Stop receipt
    hold: new Map(), // receipt_id → normalized Hold receipt
    unrecognized: new Map(), // stable key → {feed, receiptId, feedSeq, schemaVersion, why}
    latches: null, // normalizeHoldState() snapshot, or null while unread/unreadable
    feeds: {
      [FEED_STOP]: feedState(),
      [FEED_HOLD]: feedState(),
      latches: feedState(),
    },
  };
}

function rememberUnrecognized(store, feed, raw, index, schemaVersion, why) {
  const receiptId = raw && typeof raw === "object" ? text(raw.receipt_id) : null;
  const feedSeq = raw && typeof raw === "object" ? seqOf(raw.feed_seq) : null;
  const key = `${feed}:${receiptId || (feedSeq ? `#${feedSeq}` : `v${schemaVersion}@${index}`)}`;
  store.unrecognized.set(key, {
    key,
    feed,
    receiptId,
    feedSeq,
    schemaVersion,
    why,
    atMs: raw && typeof raw === "object" ? timeOf(raw.occurred_at) : null,
  });
}

// Ingest one receipt page. Idempotent: receipts are immutable and keyed, so a
// page read twice, out of order, or after a reload changes nothing. Returns
// whether the feed has more rows past this page.
//
// `more` (the feed's history is not fully read) is taken only from a page at
// or past the cursor already held: an older page arriving late says nothing
// about what lies beyond the newer one, so it can neither raise nor clear it.
export function ingestReceiptPage(store, feed, payload) {
  const state = store.feeds[feed];
  const target = feed === FEED_STOP ? store.stop : store.hold;
  const normalize = feed === FEED_STOP ? normalizeStopReceipt : normalizeHoldReceipt;
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.receipts)) {
    state.status = "malformed";
    state.detail = "unreadable page";
    return false;
  }
  if (payload.schema_version !== RECEIPT_SCHEMA_VERSION) {
    // Shown, never guessed at: the rows are listed as unrecognized and nothing
    // is classified from them. The cursor is NOT advanced — the envelope itself
    // is of a version this module does not read.
    state.status = "unrecognized";
    state.detail = `schema_version ${String(payload.schema_version)}`;
    payload.receipts.forEach((raw, i) =>
      rememberUnrecognized(store, feed, raw, i, payload.schema_version, "unrecognized schema_version"),
    );
    state.more = false; // nothing past here can be read by this build
    return false;
  }
  const held = state.cursor;
  let newest = held;
  let pageTop = null;
  payload.receipts.forEach((raw, i) => {
    const seq = raw && typeof raw === "object" ? seqOf(raw.feed_seq) : null;
    if (seq && compareSeq(seq, pageTop) > 0) pageTop = seq;
    if (seq && compareSeq(seq, newest) > 0) newest = seq;
    const receipt = normalize(raw);
    if (!receipt) {
      rememberUnrecognized(store, feed, raw, i, RECEIPT_SCHEMA_VERSION, "malformed receipt");
      return;
    }
    if (!target.has(receipt.receiptId)) target.set(receipt.receiptId, receipt);
  });
  const next = seqOf(payload.next_cursor);
  if (next && compareSeq(next, pageTop) > 0) pageTop = next;
  if (next && compareSeq(next, newest) > 0) newest = next;
  if (pageTop == null || compareSeq(pageTop, held) >= 0) state.more = next != null;
  state.cursor = newest;
  state.status = "ok";
  state.detail = null;
  return next != null;
}

export function ingestHoldState(store, payload) {
  const state = store.feeds.latches;
  const latches = normalizeHoldState(payload);
  if (!latches) {
    // Unreadable current state is reported, and the last snapshot is dropped:
    // showing a stale "held"/"not held" as current would be a guess.
    store.latches = null;
    state.status = "malformed";
    state.detail = "unreadable hold state";
    return;
  }
  store.latches = latches;
  state.status = "ok";
  state.detail = null;
}

export function recordFeedFailure(store, feed, error) {
  const state = store.feeds[feed];
  const status = error && Number.isInteger(error.status) ? error.status : null;
  state.status = "unavailable";
  state.detail = status != null ? `HTTP ${status}` : "request failed";
  if (feed === "latches") store.latches = null;
}

function storeSignature(store) {
  return JSON.stringify({
    stop: store.stop.size,
    hold: store.hold.size,
    unrecognized: store.unrecognized.size,
    feeds: store.feeds,
    latches: store.latches,
  });
}

// Whether a readable receipt feed still has history past its cursor.
export function lifecycleBacklog(store) {
  return [FEED_STOP, FEED_HOLD].some((feed) => {
    const state = store.feeds[feed];
    return state.status === "ok" && state.more;
  });
}

// The poller both views run on their own poll tick. `request` is the host-root
// GET (the console API client by default) — injected so tests drive it.
//
// A poll reads at most `maxPages` per feed. With `onUpdate`, the feed does not
// leave the rest for a view tick that may never come (the Navigator polls once
// unless Live is on): while a feed still has pages past its cursor it keeps
// polling on its own, `backlogMs` apart, calling `onUpdate(changed)` after
// each, until it is caught up. `destroy()` stops it.
export function createLifecycleFeed({
  request = requestHost,
  maxPages = MAX_RECEIPT_PAGES,
  backlogMs = RECEIPT_BACKLOG_MS,
  onUpdate = null,
} = {}) {
  const store = createLifecycleStore();
  let inFlight = null;
  let backlogTimer = null;
  let destroyed = false;

  async function drain(feed, path) {
    for (let page = 0; page < maxPages; page++) {
      const params = new URLSearchParams({ limit: String(RECEIPT_PAGE_LIMIT) });
      const cursor = store.feeds[feed].cursor;
      if (cursor) params.set("cursor", cursor);
      let payload;
      try {
        payload = await request(`${path}?${params}`, { cache: "no-store" });
      } catch (error) {
        // An unreadable history is reported as unreadable — an empty list here
        // would claim nothing was ever stopped or held.
        recordFeedFailure(store, feed, error);
        return;
      }
      if (!ingestReceiptPage(store, feed, payload)) return;
    }
  }

  async function readLatches() {
    let payload;
    try {
      payload = await request(HOLD_STATE_PATH, { cache: "no-store" });
    } catch (error) {
      recordFeedFailure(store, "latches", error);
      return;
    }
    ingestHoldState(store, payload);
  }

  // Resolves to whether anything the views render changed.
  function poll() {
    if (inFlight) return inFlight;
    inFlight = (async () => {
      const before = storeSignature(store);
      await drain(FEED_STOP, STOP_RECEIPTS_PATH);
      await drain(FEED_HOLD, HOLD_RECEIPTS_PATH);
      await readLatches();
      return storeSignature(store) !== before;
    })();
    return inFlight.finally(() => {
      inFlight = null;
      scheduleBacklog();
    });
  }

  function scheduleBacklog() {
    if (destroyed || !onUpdate || backlogTimer || !lifecycleBacklog(store)) return;
    backlogTimer = setTimeout(async () => {
      backlogTimer = null;
      if (destroyed) return;
      const changed = await poll();
      if (!destroyed) onUpdate(changed);
    }, backlogMs);
  }

  function destroy() {
    destroyed = true;
    if (backlogTimer) {
      clearTimeout(backlogTimer);
      backlogTimer = null;
    }
  }

  return { store, poll, destroy };
}

// ── Index: receipts arranged for joining ─────────────────────

function pushTo(map, key, value) {
  let arr = map.get(key);
  if (!arr) {
    arr = [];
    map.set(key, arr);
  }
  arr.push(value);
}

// Arrange the store for the joins. Every list is sorted, so the index — and
// everything resolved from it — is independent of ingestion order.
export function lifecycleIndex(store) {
  const stops = [...store.stop.values()].sort(cmpReceipts);
  const holds = [...store.hold.values()].sort(cmpReceipts);
  const byTraceSpan = new Map(); // `${trace} ${span}` → [entry]
  const byDid = new Map(); // agent DID → [entry]
  const unplaced = []; // entries whose agent was not recorded
  for (const r of stops) {
    const exactKey = r.traceId && r.spanId ? `${r.traceId} ${r.spanId}` : null;
    const own = [];
    const perDid = new Map();
    const orphan = [];
    for (const o of r.outcomes) {
      // Agent scope blinds the per-outcome identity; its DID is the header's.
      const did = o.agentId || (r.scope !== "host" ? r.targetAgentId : null);
      if (exactKey && (!did || !r.targetAgentId || did === r.targetAgentId)) own.push(o);
      else if (did) pushTo(perDid, did, o);
      else orphan.push(o);
    }
    if (exactKey) {
      const entry = { receipt: r, did: r.targetAgentId, outcomes: own, exact: true };
      pushTo(byTraceSpan, exactKey, entry);
      if (r.targetAgentId) pushTo(byDid, r.targetAgentId, entry);
      else unplaced.push(entry);
    } else if (!r.outcomes.length) {
      const entry = { receipt: r, did: r.targetAgentId, outcomes: [], exact: false };
      if (r.targetAgentId) pushTo(byDid, r.targetAgentId, entry);
      else unplaced.push(entry);
    }
    for (const [did, outcomes] of [...perDid.entries()].sort((a, b) => cmpText(a[0], b[0]))) {
      pushTo(byDid, did, { receipt: r, did, outcomes, exact: false });
    }
    if (orphan.length) unplaced.push({ receipt: r, did: null, outcomes: orphan, exact: false });
  }
  const unrecognized = [...store.unrecognized.values()].sort(
    (a, b) => cmpText(a.feed, b.feed) || compareSeq(a.feedSeq, b.feedSeq) || cmpText(a.key, b.key),
  );
  return { store, stops, holds, byTraceSpan, byDid, unplaced, unrecognized, latches: store.latches };
}

// ── Turns ─────────────────────────────────────────────────────

function entryDisposition(entry) {
  const set = new Set(entry.outcomes.map((o) => o.disposition));
  if (set.has("stopped")) return "stopped";
  return entry.outcomes.length ? entry.outcomes[0].disposition : null;
}

function uniqueEntries(entries) {
  const seen = new Set();
  const out = [];
  for (const e of entries) {
    const key = `${e.receipt.receiptId} ${e.did || ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(e);
  }
  return out.sort((a, b) => cmpReceipts(a.receipt, b.receipt) || cmpText(a.did, b.did));
}

function lifecycleOf(entries, spanOutcome) {
  if (!entries.length && spanOutcome == null) return null;
  const dispositions = new Set();
  for (const e of entries) for (const o of e.outcomes) dispositions.add(o.disposition);
  let state = null;
  if (dispositions.has("stopped")) state = "stopped";
  else if (spanOutcome === "stopped") state = "stopped_pending";
  else if (spanOutcome != null) {
    state = KNOWN_OUTCOMES.has(spanOutcome) ? spanOutcome : "unrecognized_outcome";
  } else if (dispositions.has("unreachable")) state = "unreachable";
  else if (dispositions.has("refused")) state = "refused";
  const markers = [];
  if (dispositions.has("already_complete")) markers.push(MARKER_AFTER_COMPLETION);
  if (dispositions.has("refused") && state !== "refused") markers.push(MARKER_REFUSED);
  if (dispositions.has("unreachable") && state !== "unreachable") markers.push(MARKER_UNREACHABLE);
  const meta = state ? TURN_STATES[state] : null;
  const label =
    state === "unrecognized_outcome" ? `${meta.label} "${spanOutcome}"` : meta ? meta.label : null;
  return {
    state,
    label,
    tone: meta ? meta.tone : null,
    spanOutcome,
    markers,
    receipts: entries,
    receiptIds: entries.map((e) => e.receipt.receiptId),
  };
}

// Resolve every turn's lifecycle at once. A turn is
// `{key, traceId, spanId, turnId, agentDid, outcome, startMs, endMs}`; the result
// maps `key` → lifecycle (or null: nothing to say — the span renders as before).
//
//   1. Exact: a receipt whose `(trace_id, span_id)` is this span's — and, through
//      the canonical `kestrel.turn_id`, every other span of that same turn.
//   2. Agent/host scope: a turn whose own outcome says `stopped` joins the DID's
//      non-exact receipts with a `stopped` outcome recorded while it ran.
//   3. State: a `stopped` disposition → stopped; a span-reported `stopped` with
//      no receipt yet → stopped (receipt pending), converging when it lands;
//      otherwise the span's own outcome. `already_complete` never changes the
//      outcome — it adds the "Stop arrived after completion" marker.
export function resolveTurnLifecycles(index, turns) {
  const exactOf = (t) =>
    t.traceId && t.spanId ? index.byTraceSpan.get(`${t.traceId} ${t.spanId}`) || [] : [];
  const byTurnId = new Map();
  for (const t of turns) {
    const exact = exactOf(t);
    if (exact.length && t.turnId) {
      byTurnId.set(t.turnId, [...(byTurnId.get(t.turnId) || []), ...exact]);
    }
  }
  const out = new Map();
  for (const t of turns) {
    const outcome = t.outcome != null && t.outcome !== "" ? String(t.outcome) : null;
    let entries = [...exactOf(t), ...((t.turnId && byTurnId.get(t.turnId)) || [])];
    if (!entries.length && outcome === "stopped" && t.agentDid) {
      entries = (index.byDid.get(t.agentDid) || []).filter(
        (e) =>
          !e.exact &&
          e.receipt.atMs >= t.startMs &&
          e.receipt.atMs <= t.endMs &&
          e.outcomes.some((o) => o.disposition === "stopped"),
      );
    }
    out.set(t.key, lifecycleOf(uniqueEntries(entries), outcome));
  }
  return out;
}

// ── Holders: the host latch and each agent (resting state + history) ──

function holdEvent(r) {
  const verb = r.action === "hold" ? "Hold" : "Resume";
  const how =
    r.disposition === "applied"
      ? ""
      : r.disposition === "already_in_state"
        ? " (already in state)"
        : " (refused: stale)";
  return {
    id: `hold:${r.receiptId}`,
    kind: r.action === "hold" ? "hold" : "resume",
    atMs: r.atMs,
    receipt: r,
    disposition: r.disposition,
    label: `${verb}${how}`,
    tone: r.action === "hold" ? "hold" : "resume",
  };
}

function stopEvent(entry) {
  const disposition = entryDisposition(entry);
  return {
    id: `stop:${entry.receipt.receiptId}:${entry.did || "-"}`,
    kind: "stop",
    atMs: entry.receipt.atMs,
    receipt: entry.receipt,
    outcomes: entry.outcomes,
    exact: entry.exact,
    did: entry.did,
    disposition,
    label: `Stop · ${disposition ? disposition.replace(/_/g, " ") : "no outcome recorded"}`,
    tone: disposition === "stopped" ? "stopped" : disposition || "unrecognized",
  };
}

function cmpEvents(a, b) {
  return cmpReceipts(a.receipt, b.receipt) || cmpText(a.id, b.id);
}

// Hold episodes for one latch: each applied hold starts one; the applied
// receipt whose `prior_hold_receipt_id` names it (a release — the Resume — or a
// replacing hold) ends it. The Resume is its own event and never edits the hold.
// The latch snapshot decides which episode is the CURRENT resting state.
function holdEpisodes(receipts, latch, latchesKnown) {
  const episodes = [];
  for (const h of receipts) {
    if (h.action !== "hold" || h.disposition !== "applied") continue;
    const end =
      receipts.find(
        (r) => r !== h && r.disposition === "applied" && r.priorHoldReceiptId === h.receiptId,
      ) || null;
    const open = end == null && latch != null && latch.holdReceiptId === h.receiptId;
    episodes.push({
      id: `episode:${h.receiptId}`,
      scope: h.scope,
      targetId: h.targetId,
      hold: h,
      end,
      latch: open ? latch : null,
      startMs: h.atMs,
      endMs: end ? end.atMs : null,
      // Without a readable latch snapshot an unended hold is the best-known
      // state, and is labeled as such rather than as "held now".
      open: open || (end == null && !latchesKnown),
      latchUnknown: !latchesKnown,
      endUnknown: end == null && latchesKnown && !open,
      reason: h.reason,
      actorId: h.actorId,
    });
  }
  if (latch && !episodes.some((e) => e.hold.receiptId === latch.holdReceiptId)) {
    // The latch is current but its receipt is not (yet) in the paged history.
    episodes.push({
      id: `episode:${latch.holdReceiptId}`,
      scope: latch.scope,
      targetId: latch.targetId,
      hold: null,
      end: null,
      latch,
      startMs: latch.atMs,
      endMs: null,
      open: true,
      latchUnknown: false,
      endUnknown: false,
      reason: latch.reason,
      actorId: latch.actorId,
    });
  }
  return episodes.sort((a, b) => (a.startMs ?? 0) - (b.startMs ?? 0) || cmpText(a.id, b.id));
}

function holderFor(index, scope, did) {
  const latches = index.latches;
  const latchesKnown = latches != null;
  const receipts = index.holds.filter((r) =>
    scope === "host" ? r.scope === "host" : r.scope === "agent" && r.targetId === did,
  );
  let latch = null;
  let held = null;
  let sources = [];
  if (latchesKnown) {
    if (scope === "host") {
      latch = latches.host;
      held = latch != null;
      sources = held ? ["host"] : [];
    } else {
      const entry = latches.agents.find((a) => a.agentId === did);
      latch = entry ? entry.agentHold : null;
      held = entry ? entry.held : latch != null;
      sources = entry ? entry.sources : [];
    }
  }
  const events = receipts.map(holdEvent);
  if (scope !== "host") {
    for (const entry of index.byDid.get(did) || []) events.push(stopEvent(entry));
  }
  events.sort(cmpEvents);
  return {
    key: scope === "host" ? "host" : `did:${did}`,
    scope,
    did: scope === "host" ? null : did,
    latchesKnown,
    held,
    sources,
    latch,
    episodes: holdEpisodes(receipts, latch, latchesKnown),
    events,
  };
}

// Every lifecycle holder with anything to show: the host latch (when it has a
// latch or any history) and each agent DID that is held now, has Hold history,
// or was the target of a Stop. A held agent always gets one — with or without
// spans — so a resting state is never invisible.
export function lifecycleHolders(index) {
  const dids = new Set();
  if (index.latches) for (const a of index.latches.agents) if (a.held) dids.add(a.agentId);
  for (const r of index.holds) if (r.scope === "agent") dids.add(r.targetId);
  for (const did of index.byDid.keys()) dids.add(did);
  const holders = [];
  if ((index.latches && index.latches.host) || index.holds.some((r) => r.scope === "host")) {
    holders.push(holderFor(index, "host", null));
  }
  for (const did of [...dids].sort(cmpText)) holders.push(holderFor(index, "agent", did));
  return holders;
}

// Receipts that name no agent, as events ("agent not recorded").
export function unplacedEvents(index) {
  return index.unplaced.map(stopEvent).sort(cmpEvents);
}

export function unrecognizedEvents(index) {
  return index.unrecognized.map((u) => ({
    id: `unrecognized:${u.key}`,
    kind: "unrecognized",
    atMs: u.atMs,
    receipt: null,
    unrecognized: u,
    disposition: null,
    label: "unrecognized receipt",
    tone: "unrecognized",
  }));
}

// Feed problems every view must surface: an unreadable, unrecognized or
// refused feed is shown, never rendered as "nothing happened" — and a feed
// whose history is still being paged says so.
export function lifecycleFeedNotices(store) {
  const names = {
    [FEED_STOP]: "Stop receipts",
    [FEED_HOLD]: "Hold receipts",
    latches: "Hold state",
  };
  const notices = [];
  for (const feed of [FEED_STOP, FEED_HOLD, "latches"]) {
    const state = store.feeds[feed];
    if (state.status === "ok" && state.more) {
      // A partly read history is said to be partial, never shown as the whole.
      notices.push({ feed, status: "loading", text: `${names[feed]} loading older history…` });
      continue;
    }
    if (state.status === "ok" || state.status === "pending") continue;
    const what =
      state.status === "unrecognized"
        ? `unrecognized ${state.detail}`
        : state.status === "malformed"
          ? `unreadable (${state.detail})`
          : `unavailable (${state.detail})`;
    notices.push({ feed, status: state.status, text: `${names[feed]} ${what}` });
  }
  return notices;
}

// ── Shared presentation (escaped + clipped; identical in both views) ──

function row(label, value) {
  if (value == null || value === "") return "";
  return (
    `<div class="obs-detail__row"><span class="obs-detail__key">${escapeHtml(label)}</span>` +
    `<span class="obs-detail__value">${escapeHtml(clip(value))}</span></div>`
  );
}

function iso(ms) {
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

function outcomeListHtml(outcomes) {
  if (!outcomes.length) {
    return `<div class="obs-lifecycle__note">no per-target outcome recorded</div>`;
  }
  return (
    `<ol class="obs-lifecycle__outcomes">` +
    outcomes
      .map(
        (o) =>
          `<li data-outcome-disposition="${escapeHtml(o.disposition)}">` +
          `<span class="obs-lifecycle__pill obs-lifecycle__pill--${escapeHtml(o.disposition)}">` +
          `${escapeHtml(o.disposition.replace(/_/g, " "))}</span> ` +
          `${escapeHtml(clip(o.agentId || AGENT_NOT_RECORDED))}` +
          (o.detail ? ` · ${escapeHtml(clip(o.detail))}` : "") +
          `</li>`,
      )
      .join("") +
    `</ol>`
  );
}

export function renderStopReceiptHtml(receipt, focusOutcomes = null) {
  const r = receipt;
  const agent =
    r.scope === "host" ? null : r.targetAgentId || AGENT_NOT_RECORDED;
  const focus =
    focusOutcomes && focusOutcomes.length !== r.outcomes.length
      ? `<div class="obs-lifecycle__sub">this target</div>${outcomeListHtml(focusOutcomes)}`
      : "";
  return (
    `<div class="obs-lifecycle__receipt" data-receipt-id="${escapeHtml(r.receiptId)}">` +
    `<div class="obs-lifecycle__title">Stop receipt</div>` +
    row("receipt", r.receiptId) +
    row("scope", r.scope) +
    row("time", r.occurredAt) +
    row("actor", r.actorId || "not recorded") +
    row("reason", r.reason || "none given") +
    row("agent", agent) +
    (r.cascade == null ? "" : row("cascade", r.cascade ? "yes" : "no")) +
    row("trace ID", r.traceId) +
    row("span ID", r.spanId) +
    focus +
    `<div class="obs-lifecycle__sub">per-target outcomes</div>` +
    outcomeListHtml(r.outcomes) +
    `</div>`
  );
}

export function renderHoldReceiptHtml(r) {
  let link = null;
  if (r.action === "release" && r.disposition === "applied") link = `ends hold ${r.priorHoldReceiptId}`;
  else if (r.action === "hold" && r.disposition === "applied" && r.priorHoldReceiptId) {
    link = `replaces hold ${r.priorHoldReceiptId}`;
  } else if (r.disposition === "refused_stale") link = `current hold ${r.priorHoldReceiptId} was not the one observed`;
  else if (r.action === "hold" && r.disposition === "already_in_state") link = `already held by ${r.priorHoldReceiptId}`;
  return (
    `<div class="obs-lifecycle__receipt" data-receipt-id="${escapeHtml(r.receiptId)}">` +
    `<div class="obs-lifecycle__title">${r.action === "hold" ? "Hold receipt" : "Resume receipt"}</div>` +
    row("receipt", r.receiptId) +
    row("action", r.action) +
    row("disposition", r.disposition.replace(/_/g, " ")) +
    row("scope", r.scope) +
    row("target", r.targetId) +
    row("time", r.occurredAt) +
    row("actor", r.actorId || "not recorded") +
    row("reason", r.reason || "none given") +
    row("links", link) +
    `</div>`
  );
}

function renderUnrecognizedHtml(u) {
  return (
    `<div class="obs-lifecycle__receipt obs-lifecycle__receipt--unrecognized">` +
    `<div class="obs-lifecycle__title">Unrecognized receipt</div>` +
    row("feed", u.feed) +
    row("receipt", u.receiptId || "unreadable") +
    row("schema", `schema_version ${String(u.schemaVersion)}`) +
    row("why", u.why) +
    `<div class="obs-lifecycle__note">Not used to classify any turn.</div>` +
    `</div>`
  );
}

export function renderLifecycleEventHtml(event) {
  if (event.kind === "unrecognized") return renderUnrecognizedHtml(event.unrecognized);
  if (event.kind === "stop") return renderStopReceiptHtml(event.receipt, event.outcomes);
  return renderHoldReceiptHtml(event.receipt);
}

export function holderStateLabel(holder) {
  if (holder.held == null) return "hold state unavailable";
  if (!holder.held) return "not held";
  return holder.sources.length ? `held (${holder.sources.join(", ")})` : "held";
}

export function renderEpisodeHtml(episode) {
  const state = episode.open
    ? episode.latchUnknown
      ? "held (current hold state unavailable)"
      : "held now"
    : episode.endUnknown
      ? "ended (release not loaded)"
      : "released";
  return (
    `<div class="obs-lifecycle__receipt" data-hold-receipt-id="${escapeHtml(episode.hold ? episode.hold.receiptId : episode.latch.holdReceiptId)}">` +
    `<div class="obs-lifecycle__title">Hold · ${escapeHtml(episode.scope)}</div>` +
    row("state", state) +
    row("since", iso(episode.startMs)) +
    row("until", iso(episode.endMs)) +
    row("actor", episode.actorId || "not recorded") +
    row("reason", episode.reason || "none given") +
    row("hold receipt", episode.hold ? episode.hold.receiptId : episode.latch.holdReceiptId) +
    row("resume receipt", episode.end ? episode.end.receiptId : null) +
    `</div>`
  );
}

export function renderHolderHtml(holder) {
  const title = holder.scope === "host" ? "Host hold" : `Agent ${holder.did}`;
  const current = holder.episodes.filter((e) => e.open);
  return (
    `<div class="obs-lifecycle" data-holder="${escapeHtml(holder.key)}">` +
    `<div class="obs-lifecycle__head"><span class="obs-lifecycle__title">${escapeHtml(title)}</span>` +
    `<span class="obs-lifecycle__pill obs-lifecycle__pill--${holder.held ? "hold" : "ok"}">${escapeHtml(holderStateLabel(holder))}</span></div>` +
    current.map(renderEpisodeHtml).join("") +
    `</div>`
  );
}

export function renderTurnLifecycleHtml(lifecycle) {
  if (!lifecycle) return "";
  const pill = lifecycle.label
    ? `<span class="obs-lifecycle__pill obs-lifecycle__pill--${escapeHtml(lifecycle.tone)}">${escapeHtml(lifecycle.label)}</span>`
    : "";
  const markers = lifecycle.markers
    .map((m) => `<span class="obs-lifecycle__marker">${escapeHtml(m)}</span>`)
    .join("");
  const pending =
    lifecycle.state === "stopped_pending"
      ? `<div class="obs-lifecycle__note">The span reports a Stop; its receipt has not arrived yet.</div>`
      : "";
  const receipts = lifecycle.receipts
    .map((e) => renderStopReceiptHtml(e.receipt, e.outcomes))
    .join("");
  return (
    `<div class="obs-lifecycle" data-lifecycle-state="${escapeHtml(lifecycle.state || "")}">` +
    `<div class="obs-lifecycle__head">${pill}${markers}</div>${pending}${receipts}</div>`
  );
}

export function renderFeedNoticesHtml(store) {
  return lifecycleFeedNotices(store)
    .map(
      (n) =>
        `<div class="obs-lifecycle__notice" data-lifecycle-notice="${escapeHtml(n.feed)}">${escapeHtml(n.text)}</div>`,
    )
    .join("");
}

// Hover lines for a turn — `{text, tone}` like `spanTooltipLines`.
export function lifecycleTooltipLines(lifecycle) {
  if (!lifecycle) return [];
  const lines = [];
  if (lifecycle.label) {
    lines.push({ text: `lifecycle: ${lifecycle.label}`, tone: lifecycle.tone === "error" ? "warn" : "dim" });
  }
  for (const m of lifecycle.markers) lines.push({ text: m, tone: "warn" });
  if (lifecycle.receiptIds.length) {
    lines.push({ text: `Stop receipt ${lifecycle.receiptIds.join(", ")}`, tone: "dim" });
  }
  return lines;
}
