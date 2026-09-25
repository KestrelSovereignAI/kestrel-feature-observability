// Cooperative Stop actions — the Timeline popover, the Navigator inspector and
// the selection action bar (#115, kestrel-sovereign#3158).
//
// ./lifecycle.js reads what HAPPENED; this module is the one place either view
// ASKS for a Stop, so both views send the same request, through the same door,
// for the same target:
//
//   - The target is a turn's canonical address — the `kestrel.turn_id` stamped
//     on the turn's OWN span (the feature's turn root, or core's
//     `agent.process_input[_streaming]`) — and nothing else: never a span id, a
//     `session#n`, `kestrel.orchestrator` or display ancestry. A turn without a
//     `kestrel.turn_id`, or without the agent DID its span names, gets no Stop:
//     the control is shown disabled, with the reason.
//   - One door: `POST /api/agent/stop` through the console's
//     `API.requestForAgent` (./phoenix.js `requestAgentStop`), routed by the
//     span's `kestrel.agent_name` and carrying `expected_agent_id` = the span's
//     DID. The host answers 409 `agent_identity_changed` when the routed agent
//     is not that DID, which is what makes routing by name safe. No authority is
//     decided here — the host decides, per request.
//   - A `correlation_id` is minted once per (gesture, target) and reused on
//     every retry of that target, so a retry replays the durable receipt.
//   - Stop is offered only for a live turn. An ended turn keeps its lifecycle
//     visible with Stop disabled, and a server `already_complete` is shown as
//     that, never as a failure.
//   - Selection is keyed by (agent DID, `turn_id`): it survives every redraw and
//     poll and can never drift onto another turn. A confirmed multi-Stop sends
//     exactly the selected targets, one request each with bounded concurrency,
//     and every target keeps its own outcome row. Failed and unreachable rows
//     can be retried.
//   - Nothing here paints a turn as stopped. A reply is shown as the reply to
//     the request; the turn's state still comes only from its span outcome and
//     the receipt feed, which the views re-read as soon as a Stop settles.
//
// Pure and DOM-free except for the controller's only I/O, the injected door.

import {
  requestAgentStop,
  escapeHtml,
  parseAttributes,
  getAttr,
  plural,
  spanRoleOf,
  ROLE_TURN_ROOT,
  ATTR_TURN_ID,
  ATTR_AGENT_DID,
  ATTR_CORE_AGENT_DID,
  ATTR_AGENT_NAME,
} from "./phoenix.js";

// Requests one confirmed multi-Stop keeps in flight at once.
export const STOP_CONCURRENCY = 4;

// Core's own turn spans (kestrel_agent.py / agent/streaming.py). They are
// exported only once they end, so they are always ended by the time a view
// shows them.
export const CORE_TURN_SPAN_NAMES = Object.freeze([
  "agent.process_input",
  "agent.process_input_streaming",
]);

// Every reply a Stop request can end in, with its label and tone (the styling
// key). Only a failed or unreachable reply is retryable. An identity change is
// not: the routed agent is no longer the one the turn belongs to.
export const STOP_RESULTS = Object.freeze({
  pending: { label: "requested…", tone: "pending", retryable: false },
  stopped: { label: "stopped", tone: "stopped", retryable: false },
  already_complete: { label: "already complete", tone: "complete", retryable: false },
  refused: { label: "refused", tone: "refused", retryable: true },
  unreachable: { label: "unreachable", tone: "unreachable", retryable: true },
  identity_changed: {
    label: "agent identity changed; not stopped",
    tone: "error",
    retryable: false,
  },
  error: { label: "failed", tone: "error", retryable: true },
});

function text(value) {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

// ── Target (pure) ─────────────────────────────────────────────

// Is this span a turn's OWN span, the one that carries its canonical address?
// Accepts a raw Phoenix span or the Timeline's normalized record.
export function isTurnSpan(span) {
  if (!span) return false;
  return CORE_TURN_SPAN_NAMES.includes(String(span.name ?? "")) || spanRoleOf(span) === ROLE_TURN_ROOT;
}

export function stopTargetKey(agentDid, turnId) {
  return JSON.stringify([agentDid, turnId]);
}

// The Stop target a turn span names, read from that span's own attributes only.
// `key` is null unless both halves of the identity are present.
export function stopTargetOf(span) {
  const attrs = parseAttributes(span && (span.attributes ?? span.attrs));
  const turnId = text(getAttr(attrs, ATTR_TURN_ID));
  const did = text(getAttr(attrs, ATTR_AGENT_DID)) || text(getAttr(attrs, ATTR_CORE_AGENT_DID));
  const agentDid = did && did.startsWith("did:") ? did : null;
  return {
    key: turnId && agentDid ? stopTargetKey(agentDid, turnId) : null,
    turnId,
    agentDid,
    agentName: text(getAttr(attrs, ATTR_AGENT_NAME)),
    label: text(span && span.name) || "(turn)",
  };
}

// Whether Stop can mean anything for this turn. `open` is the view's own answer
// to "has this turn ended?"; `lifecycle` is its resolved lifecycle, if any.
export function stopAvailability(target, { open, lifecycle = null } = {}) {
  const no = (reason) => ({ enabled: false, reason });
  if (!target || !target.turnId) return no("no canonical turn address (kestrel.turn_id) on this turn");
  if (!target.agentDid) return no("no agent DID recorded on this turn");
  if (!target.agentName) return no("no agent name recorded to route the Stop to");
  if (lifecycle && lifecycle.spanOutcome != null) {
    return no(`turn has ended (${lifecycle.label || lifecycle.spanOutcome})`);
  }
  if (lifecycle && lifecycle.state === "stopped") return no("turn has ended (stopped)");
  if (!open) return no("turn has ended");
  return { enabled: true, reason: null };
}

// ── The request and its reply (pure) ─────────────────────────

export function stopRequestBody(attempt) {
  return {
    turn_id: attempt.target.turnId,
    expected_agent_id: attempt.target.agentDid,
    correlation_id: attempt.correlationId,
  };
}

// Random, and available in an insecure context too (unlike `randomUUID`).
export function mintCorrelationId() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `stop-${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function receiptIdsOf(outcomes) {
  const ids = [];
  for (const o of outcomes) {
    const id = o && typeof o === "object" ? text(o.receipt_id) : null;
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

function dispositionResult(outcomes) {
  const dispositions = new Set(outcomes.map((o) => o && o.disposition));
  if (dispositions.has("stopped")) return "stopped";
  if (dispositions.has("unreachable")) return "unreachable";
  if (dispositions.has("refused")) return "refused";
  if (dispositions.size === 1 && dispositions.has("already_complete")) return "already_complete";
  return null;
}

// A 200 from the door: its `stop_outcomes` decide the result.
export function readStopReply(payload) {
  const outcomes = payload && Array.isArray(payload.stop_outcomes) ? payload.stop_outcomes : null;
  const status = outcomes && outcomes.length ? dispositionResult(outcomes) : null;
  if (!status) return { status: "error", detail: "unrecognized reply", receiptIds: [] };
  return { status, detail: null, receiptIds: receiptIdsOf(outcomes) };
}

// A rejected request (the console's `ApiError`, or a network failure).
export function readStopError(error) {
  const httpStatus = error && Number.isInteger(error.status) ? error.status : null;
  const code = error && typeof error.code === "string" ? error.code : null;
  if (httpStatus === 409 && code === "agent_identity_changed") {
    return { status: "identity_changed", detail: null, receiptIds: [] };
  }
  if (httpStatus === 503 && code === "stop_not_confirmed") {
    // The host could not confirm the Stop: its typed outcomes say why.
    const envelope = error.body && typeof error.body === "object" ? error.body.error : null;
    const outcomes = envelope && Array.isArray(envelope.details) ? envelope.details : [];
    const status = dispositionResult(outcomes);
    if (status === "unreachable" || status === "refused") {
      return { status, detail: null, receiptIds: receiptIdsOf(outcomes) };
    }
  }
  if (httpStatus != null) {
    return { status: "error", detail: `HTTP ${httpStatus}${code ? ` · ${code}` : ""}`, receiptIds: [] };
  }
  return { status: "error", detail: "request failed", receiptIds: [] };
}

// ── Controller: selection, gestures, attempts ─────────────────
//
// `onChange()` runs after every change a view renders (selection, confirm
// prompt, an attempt's state); `onSettled()` once a gesture's requests — or a
// retry — have all answered, which is when the views re-read spans and
// receipts. `request(agentName, body)` is the door, injected for tests.
export function createStopController({
  request = requestAgentStop,
  concurrency = STOP_CONCURRENCY,
  mintId = mintCorrelationId,
  onChange = null,
  onSettled = null,
} = {}) {
  const selection = new Map(); // target key → target, in selection order
  const attempts = new Map(); // target key → that target's latest attempt
  let batch = null; // the attempts of the last confirmed multi-Stop
  let confirming = false;
  let destroyed = false;

  function changed() {
    if (!destroyed && onChange) onChange();
  }

  function snapshot(target) {
    return {
      key: target.key,
      turnId: target.turnId,
      agentDid: target.agentDid,
      agentName: target.agentName,
      label: target.label,
    };
  }

  function select(target) {
    if (!target || !target.key || selection.has(target.key)) return;
    selection.set(target.key, snapshot(target));
    confirming = false;
    changed();
  }

  function deselect(key) {
    if (!selection.delete(key)) return;
    confirming = false;
    changed();
  }

  function clearSelection() {
    selection.clear();
    confirming = false;
    changed();
  }

  // One gesture's attempt for one target — and with it, the correlation id
  // every retry of that target reuses. It is reserved (`inFlight`) from the
  // moment it exists, including while it waits for a lane in a batch, so no
  // second gesture can send the same target under another correlation id.
  function newAttempt(target) {
    const attempt = {
      key: target.key,
      target: snapshot(target),
      correlationId: mintId(),
      status: "pending",
      detail: null,
      receiptIds: [],
      inFlight: true,
    };
    attempts.set(target.key, attempt);
    return attempt;
  }

  // The attempt a gesture uses for a target: the one already reserved for it
  // (queued or in flight) when there is one, else a fresh one. `fresh` says
  // whether this gesture must send it.
  function attemptFor(target) {
    const current = attempts.get(target.key);
    if (current && current.inFlight) return { attempt: current, fresh: false };
    return { attempt: newAttempt(target), fresh: true };
  }

  async function send(attempt) {
    attempt.inFlight = true;
    attempt.status = "pending";
    attempt.detail = null;
    changed();
    let result;
    try {
      result = readStopReply(await request(attempt.target.agentName, stopRequestBody(attempt)));
    } catch (error) {
      result = readStopError(error);
    }
    Object.assign(attempt, result, { inFlight: false });
    changed();
  }

  async function run(list) {
    let next = 0;
    const lane = async () => {
      while (next < list.length) await send(list[next++]);
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, list.length) }, lane));
    if (!destroyed && onSettled) onSettled();
  }

  // A single Stop from the popover or inspector: one gesture, one target, no
  // confirm (like the agent card's Stop).
  function stopOne(target) {
    if (!target || !target.key) return null;
    const current = attempts.get(target.key);
    if (current && current.inFlight) return null;
    return run([newAttempt(target)]);
  }

  function askConfirm() {
    if (!selection.size) return;
    confirming = true;
    changed();
  }

  function cancelConfirm() {
    confirming = false;
    changed();
  }

  // The confirmed multi-Stop: exactly the targets selected at confirm time.
  function confirmSelected() {
    if (!confirming || !selection.size) return null;
    // A target already queued or in flight joins the batch's results under its
    // existing attempt, and is not sent a second time.
    const chosen = [...selection.values()].map(attemptFor);
    confirming = false;
    selection.clear();
    batch = chosen.map((c) => c.attempt);
    return run(chosen.filter((c) => c.fresh).map((c) => c.attempt));
  }

  // Re-send a failed or unreachable attempt under its own correlation id.
  function retry(key) {
    const attempt = attempts.get(key);
    if (!attempt || attempt.inFlight || !STOP_RESULTS[attempt.status].retryable) return null;
    attempt.inFlight = true;
    return run([attempt]);
  }

  function dismissResults() {
    batch = null;
    changed();
  }

  return {
    select,
    deselect,
    clearSelection,
    isSelected: (key) => key != null && selection.has(key),
    selected: () => [...selection.values()],
    selectionSize: () => selection.size,
    attempt: (key) => (key != null && attempts.get(key)) || null,
    batch: () => batch,
    confirming: () => confirming,
    stopOne,
    askConfirm,
    cancelConfirm,
    confirmSelected,
    retry,
    dismissResults,
    destroy() {
      destroyed = true;
    },
  };
}

// ── Clicks (shared by both views) ─────────────────────────────

function hit(event, selector) {
  const t = event && event.target;
  return t && typeof t.closest === "function" ? t.closest(selector) : null;
}

// A click inside a turn's Stop control. `target` and `availability` come from
// the view's CURRENT model, so the click acts on the turn as it is now, not as
// it was painted. Returns whether the click was a Stop control's.
export function handleTurnStopClick(controller, event, target, availability) {
  if (!target || !target.key) return false;
  if (hit(event, "[data-stop-retry]")) {
    controller.retry(target.key);
    return true;
  }
  if (hit(event, "[data-stop-turn]")) {
    if (availability.enabled) controller.stopOne(target);
    return true;
  }
  if (hit(event, "[data-stop-select]")) {
    if (controller.isSelected(target.key)) controller.deselect(target.key);
    else if (availability.enabled) controller.select(target);
    return true;
  }
  return false;
}

// A click inside the selection action bar. Returns whether it was the bar's.
export function handleStopBarClick(controller, event) {
  let el = hit(event, "[data-stop-retry]");
  if (el) {
    controller.retry(el.dataset.stopRetry);
    return true;
  }
  el = hit(event, "[data-stop-deselect]");
  if (el) {
    controller.deselect(el.dataset.stopDeselect);
    return true;
  }
  const actions = [
    ["[data-stop-selected]", () => controller.askConfirm()],
    ["[data-stop-confirm]", () => controller.confirmSelected()],
    ["[data-stop-cancel]", () => controller.cancelConfirm()],
    ["[data-stop-clear]", () => controller.clearSelection()],
    ["[data-stop-dismiss]", () => controller.dismissResults()],
  ];
  for (const [selector, act] of actions) {
    if (hit(event, selector)) {
      act();
      return true;
    }
  }
  return false;
}

// ── Presentation (escaped; identical in both views) ───────────

function resultHtml(attempt, { withTarget }) {
  const meta = STOP_RESULTS[attempt.status];
  const who = withTarget
    ? `<span class="obs-stop__target">${escapeHtml(attempt.target.label)}</span>` +
      `<span class="obs-stop__agent">${escapeHtml(attempt.target.agentName)}</span>`
    : "";
  const detail = attempt.detail ? ` · ${escapeHtml(attempt.detail)}` : "";
  const receipts = attempt.receiptIds.length
    ? ` · receipt ${escapeHtml(attempt.receiptIds.join(", "))}`
    : "";
  const retry =
    !attempt.inFlight && meta.retryable
      ? `<button type="button" class="obs-stop__btn" data-stop-retry="${escapeHtml(attempt.key)}">Retry</button>`
      : "";
  return (
    `<div class="obs-stop__result obs-stop__result--${meta.tone}" data-stop-result="${attempt.status}">` +
    `${who}<span class="obs-stop__reply">Stop request: ${escapeHtml(meta.label)}${detail}${receipts}</span>` +
    `${retry}</div>`
  );
}

// The Stop control of one inspected turn (Timeline popover, Navigator
// inspector). Empty for a span that is not a turn's own span.
export function renderTurnStopHtml(controller, target, availability) {
  if (!target) return "";
  const attempt = controller.attempt(target.key);
  const inFlight = Boolean(attempt && attempt.inFlight);
  const selected = controller.isSelected(target.key);
  const canStop = availability.enabled && !inFlight;
  const canSelect = selected || availability.enabled;
  const off = (on) => (on ? "" : ` disabled aria-disabled="true"`);
  const reason = availability.enabled
    ? ""
    : `<div class="obs-stop__reason" data-stop-reason>Stop unavailable: ${escapeHtml(availability.reason)}</div>`;
  return (
    `<div class="obs-stop" data-stop-control>` +
    `<div class="obs-stop__actions">` +
    `<button type="button" class="obs-stop__btn obs-stop__btn--stop" data-stop-turn${off(canStop)}>Stop turn</button>` +
    `<button type="button" class="obs-stop__btn" data-stop-select aria-pressed="${selected}"${off(canSelect)}>` +
    `${selected ? "Selected for Stop ✓" : "Select for Stop"}</button>` +
    `</div>${reason}${attempt ? resultHtml(attempt, { withTarget: false }) : ""}</div>`
  );
}

// The selection action bar: the selected turns with their count, the confirm
// step, and every target's outcome from the last confirmed multi-Stop. Empty
// when there is nothing to show.
export function renderStopBarHtml(controller) {
  const selected = controller.selected();
  const batch = controller.batch();
  if (!selected.length && !batch) return "";
  let html = `<div class="obs-stopbar" role="region" aria-label="Turns selected for Stop">`;
  if (selected.length) {
    const n = plural(selected.length, "turn");
    const items = selected
      .map(
        (t) =>
          `<li class="obs-stopbar__item" data-stop-item="${escapeHtml(t.key)}">` +
          `<span class="obs-stop__target">${escapeHtml(t.label)}</span>` +
          `<span class="obs-stop__agent">${escapeHtml(t.agentName)}</span>` +
          `<button type="button" class="obs-stop__btn obs-stop__btn--icon" data-stop-deselect="${escapeHtml(t.key)}" ` +
          `aria-label="Deselect ${escapeHtml(t.label)}">✕</button></li>`,
      )
      .join("");
    const actions = controller.confirming()
      ? `<span class="obs-stopbar__confirm" role="alert">Stop ${n} selected?</span>` +
        `<button type="button" class="obs-stop__btn obs-stop__btn--stop" data-stop-confirm>Stop ${n}</button>` +
        `<button type="button" class="obs-stop__btn" data-stop-cancel>Cancel</button>`
      : `<button type="button" class="obs-stop__btn obs-stop__btn--stop" data-stop-selected>Stop ${n}…</button>` +
        `<button type="button" class="obs-stop__btn" data-stop-clear>Clear selection</button>`;
    html +=
      `<div class="obs-stopbar__row">` +
      `<span class="obs-stopbar__count" aria-live="polite" data-stop-count="${selected.length}">${n} selected</span>` +
      `${actions}</div><ul class="obs-stopbar__list">${items}</ul>`;
  }
  if (batch) {
    const counts = new Map();
    for (const a of batch) counts.set(a.status, (counts.get(a.status) || 0) + 1);
    const summary = [...counts.entries()]
      .map(([status, count]) => `${count} ${STOP_RESULTS[status].label}`)
      .join(" · ");
    html +=
      `<div class="obs-stopbar__results" aria-live="polite">` +
      `<div class="obs-stopbar__row"><span class="obs-stopbar__count">Stop results · ${escapeHtml(summary)}</span>` +
      `<button type="button" class="obs-stop__btn" data-stop-dismiss>Dismiss</button></div>` +
      batch.map((a) => resultHtml(a, { withTarget: true })).join("") +
      `</div>`;
  }
  return `${html}</div>`;
}
