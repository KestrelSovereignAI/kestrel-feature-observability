// Cooperative Stop actions shared by the Timeline and Navigator (#115).
//
// A trace is evidence about work, not authority over it.  The only mutation in
// this module therefore goes through Sovereign's canonical, authenticated
// per-agent Stop door.  Selection identity is the trace-stamped pair
// (agent DID, turn ID); display ancestry, orchestrator attribution, Phoenix row
// objects, and redraw order never participate in addressing.

import {
  stopActionModel,
  stopTargetFromDetail,
  stopTargetKey,
} from "./phoenix.js";

export { stopActionModel, stopTargetFromDetail, stopTargetKey } from "./phoenix.js";

const STOP_PATH = "/api/agent/stop";
const TERMINAL_DISPOSITIONS = new Set([
  "stopped",
  "already_complete",
  "refused",
  "unreachable",
]);
const CONFIRMED_STOP_DISPOSITIONS = new Set(["stopped", "already_complete"]);

function presentString(value) {
  if (typeof value !== "string") return null;
  return value.trim() ? value : null;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function canonicalTarget(value) {
  if (!value || value.addressable !== true) return null;
  const key = stopTargetKey(value.agentDid, value.turnId);
  const agentName = presentString(value.agentName);
  if (!key || key !== value.key || !agentName) return null;
  return value;
}

function defaultCorrelationId() {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return `observability:${cryptoApi.randomUUID()}`;
  }
  return `observability:${Date.now().toString(36)}:${Math.random().toString(36).slice(2)}`;
}

function structuredOutcomes(error) {
  const body = error && error.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) return [];
  const candidates = [body.error?.details, body.details, body.detail];
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) {
      return candidate.filter((item) => item && typeof item === "object");
    }
    if (candidate && typeof candidate === "object") return [candidate];
  }
  return [];
}

function outcomeMatches(outcome, target, correlationId) {
  return (
    outcome &&
    outcome.scope === "turn" &&
    outcome.requested_target === target.turnId &&
    outcome.agent_id === target.agentDid &&
    outcome.correlation_id === correlationId &&
    TERMINAL_DISPOSITIONS.has(outcome.disposition)
  );
}

function resultFromOutcome(target, outcome, { message = null, status = null } = {}) {
  return Object.freeze({
    key: target.key,
    target,
    state: outcome.disposition,
    disposition: outcome.disposition,
    detail: presentString(outcome.detail),
    receiptId: presentString(outcome.receipt_id),
    resolvedTarget: presentString(outcome.resolved_target),
    correlationId: presentString(outcome.correlation_id),
    message: presentString(outcome.detail) || presentString(message) || outcome.disposition,
    status,
    local: false,
  });
}

function indeterminateResult(target, message, extra = {}) {
  return Object.freeze({
    key: target.key,
    target,
    state: "indeterminate",
    disposition: null,
    detail: null,
    receiptId: null,
    resolvedTarget: null,
    correlationId: extra.correlationId || null,
    message: presentString(message) || "Stop outcome could not be verified.",
    status: Number.isInteger(extra.status) ? extra.status : null,
    code: presentString(extra.code),
    local: false,
  });
}

function completedResult(target) {
  return Object.freeze({
    key: target.key,
    target,
    state: "already_complete",
    disposition: "already_complete",
    detail: "The trace already records this turn as complete.",
    receiptId: null,
    resolvedTarget: target.turnId,
    correlationId: null,
    message: "Already complete — no Stop request was sent.",
    status: null,
    local: true,
  });
}

function pendingResult(target, correlationId) {
  return Object.freeze({
    key: target.key,
    target,
    state: "submitting",
    disposition: null,
    detail: null,
    receiptId: null,
    resolvedTarget: null,
    correlationId,
    message: "Requesting cooperative Stop…",
    status: null,
    local: false,
  });
}

/** Shared selection/result controller retained across Timeline/Navigator mounts. */
export function createStopController({
  api = null,
  correlationIdFactory = defaultCorrelationId,
} = {}) {
  if (!api || typeof api.requestForAgent !== "function") {
    throw new TypeError("Stop controller requires requestForAgent");
  }
  if (typeof correlationIdFactory !== "function") {
    throw new TypeError("correlationIdFactory must be callable");
  }

  const selected = new Map();
  const results = new Map();
  const pendingOperations = new Map();
  const listeners = new Set();

  function emit() {
    for (const listener of [...listeners]) {
      try {
        listener();
      } catch (_error) {
        // A dead view listener must not break Stop or the persistent action bar.
      }
    }
  }

  function observe(candidate) {
    const target = canonicalTarget(candidate);
    if (!target) return false;
    const existing = selected.get(target.key);
    if (!existing) return false;
    const completionKnown =
      existing.completionKnown === true || target.completionKnown === true;
    const completed = existing.completed === true || target.completed === true;
    if (
      completionKnown === existing.completionKnown &&
      completed === existing.completed
    ) {
      return false;
    }
    // Lifecycle may advance on a later poll, but routing and identity remain
    // the exact values selected originally.
    selected.set(
      target.key,
      Object.freeze({ ...existing, completionKnown, completed }),
    );
    emit();
    return true;
  }

  function select(candidate) {
    const target = canonicalTarget(candidate);
    if (!target) return false;
    const existing = selected.get(target.key);
    if (existing) {
      observe(target);
      return true;
    }
    selected.set(target.key, target);
    emit();
    return true;
  }

  function deselect(candidateOrKey) {
    const key = typeof candidateOrKey === "string" ? candidateOrKey : candidateOrKey?.key;
    const changed = Boolean(key && selected.delete(key));
    if (changed) emit();
    return changed;
  }

  function toggle(candidate) {
    const target = canonicalTarget(candidate);
    if (!target) return false;
    if (selected.has(target.key)) {
      deselect(target.key);
      return false;
    }
    select(target);
    return true;
  }

  function selectedTargets() {
    return [...selected.values()];
  }

  function resultValues() {
    return [...results.values()];
  }

  function targetForKey(key) {
    return selected.get(key) || results.get(key)?.target || null;
  }

  async function stopOne(candidate) {
    const original = canonicalTarget(candidate);
    if (!original) return null;
    const pendingOperation = pendingOperations.get(original.key);
    if (pendingOperation) return pendingOperation;
    if (selected.has(original.key)) observe(original);
    const target = selected.get(original.key) || original;
    if (target.completionKnown !== true) return null;
    const priorResult = results.get(target.key);
    if (CONFIRMED_STOP_DISPOSITIONS.has(priorResult?.state)) {
      return priorResult;
    }
    if (target.completed) {
      const complete = completedResult(target);
      results.set(target.key, complete);
      emit();
      return complete;
    }

    const correlationId = presentString(correlationIdFactory());
    if (!correlationId) {
      const invalid = indeterminateResult(target, "Stop correlation ID could not be created.");
      results.set(target.key, invalid);
      emit();
      return invalid;
    }
    results.set(target.key, pendingResult(target, correlationId));
    emit();

    const operation = (async () => {
      try {
        const response = await api.requestForAgent(
          STOP_PATH,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ turn_id: target.turnId, correlation_id: correlationId }),
          },
          target.agentName,
        );
        const outcomes = Array.isArray(response?.stop_outcomes) ? response.stop_outcomes : [];
        const responseTurn = response?.turn_id == null ? target.turnId : response.turn_id;
        let result;
        if (
          responseTurn !== target.turnId ||
          outcomes.length !== 1 ||
          !outcomeMatches(outcomes[0], target, correlationId)
        ) {
          result = indeterminateResult(
            target,
            "Stop response did not match the selected agent DID and turn ID.",
            { correlationId },
          );
        } else {
          result = resultFromOutcome(target, outcomes[0], { message: response?.message });
        }
        results.set(target.key, result);
        emit();
        return result;
      } catch (error) {
        const outcomes = structuredOutcomes(error);
        let result;
        if (outcomes.length === 1 && outcomeMatches(outcomes[0], target, correlationId)) {
          result = resultFromOutcome(target, outcomes[0], {
            message: error?.message,
            status: Number.isInteger(error?.status) ? error.status : null,
          });
        } else {
          const identityMismatch = outcomes.length > 0;
          result = indeterminateResult(
            target,
            identityMismatch
              ? "Stop error did not match the selected agent DID and turn ID."
              : error?.message || "Stop request failed before an outcome was confirmed.",
            {
              correlationId,
              status: error?.status,
              code: error?.code,
            },
          );
        }
        results.set(target.key, result);
        emit();
        return result;
      }
    })();
    pendingOperations.set(target.key, operation);
    const forgetOperation = () => {
      if (pendingOperations.get(target.key) === operation) {
        pendingOperations.delete(target.key);
      }
    };
    operation.then(forgetOperation, forgetOperation);
    return operation;
  }

  async function stopSelected() {
    // Snapshot exact targets before the first await.  Redraws, deselection, and
    // sub-tab switches cannot retarget an in-flight multi-Stop operation.
    const targets = selectedTargets().filter(
      (target) => {
        const state = results.get(target.key)?.state;
        return (
          target.completionKnown === true &&
          state !== "submitting" &&
          !CONFIRMED_STOP_DISPOSITIONS.has(state)
        );
      },
    );
    return Promise.all(targets.map((target) => stopOne(target)));
  }

  return Object.freeze({
    subscribe(listener) {
      if (typeof listener !== "function") throw new TypeError("listener must be callable");
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    observe,
    select,
    deselect,
    toggle,
    isSelected(candidateOrKey) {
      const key = typeof candidateOrKey === "string" ? candidateOrKey : candidateOrKey?.key;
      return Boolean(key && selected.has(key));
    },
    selected: selectedTargets,
    results: resultValues,
    targetForKey,
    getResult(candidateOrKey) {
      const key = typeof candidateOrKey === "string" ? candidateOrKey : candidateOrKey?.key;
      return key ? results.get(key) || null : null;
    },
    stopOne,
    stopSelected,
    clearSelection() {
      if (!selected.size) return;
      selected.clear();
      emit();
    },
    clearResults() {
      if (!results.size) return;
      results.clear();
      emit();
    },
  });
}

function resultLabel(result) {
  switch (result?.state) {
    case "submitting":
      return "Stopping…";
    case "stopped":
      return "Stopped";
    case "already_complete":
      return "Already complete";
    case "refused":
      return "Refused";
    case "unreachable":
      return "Unreachable";
    default:
      return "Indeterminate";
  }
}

/** Mount the persistent, accessible multi-selection action bar. */
export function mountStopActionBar(element, controller) {
  if (!element || !controller) return { destroy() {} };

  function render() {
    const selected = controller.selected();
    const resultByKey = new Map(controller.results().map((result) => [result.key, result]));
    const dispatchable = selected.filter(
      (target) => {
        const state = resultByKey.get(target.key)?.state;
        return (
          target.completionKnown === true &&
          target.completed !== true &&
          state !== "submitting" &&
          !CONFIRMED_STOP_DISPOSITIONS.has(state)
        );
      },
    );
    const rows = new Map(selected.map((target) => [target.key, { target, result: null }]));
    for (const result of resultByKey.values()) {
      rows.set(result.key, { target: result.target, result });
    }
    element.innerHTML = `
      <section class="obs-stopbar" aria-label="Cooperative Stop selection">
        <div class="obs-stopbar__head">
          <strong>Cooperative Stop</strong>
          <span class="obs-stopbar__count">${selected.length} turn${selected.length === 1 ? "" : "s"} selected</span>
          <span class="obs-stopbar__grow"></span>
          <button type="button" class="obs-stopbar__button obs-stopbar__button--danger" data-stop-selected ${dispatchable.length ? "" : "disabled"}>Stop selected</button>
          <button type="button" class="obs-stopbar__button" data-clear-selection ${selected.length ? "" : "disabled"}>Clear selection</button>
          <button type="button" class="obs-stopbar__button" data-clear-results ${resultByKey.size ? "" : "disabled"}>Dismiss outcomes</button>
        </div>
        ${rows.size ? `
          <ul class="obs-stopbar__list">
            ${[...rows.values()].map(({ target, result }) => `
              <li class="obs-stopbar__item">
                <span class="obs-stopbar__target">
                  <b>${escapeHtml(target.agentName)}</b>
                  <code>${escapeHtml(target.agentDid)}</code>
                  <code>${escapeHtml(target.turnId)}</code>
                </span>
                ${result ? `<span class="obs-stopbar__outcome obs-stopbar__outcome--${escapeHtml(result.state)}" title="${escapeHtml(result.message)}">${escapeHtml(resultLabel(result))}</span>` : `<span class="obs-stopbar__outcome">Selected</span>`}
                ${result && !target.completed && result.state !== "submitting" && result.state !== "stopped" && result.state !== "already_complete" ? `<button type="button" class="obs-stopbar__button" data-retry-stop="${escapeHtml(target.key)}">Retry</button>` : ""}
              </li>`).join("")}
          </ul>` : `<div class="obs-stopbar__empty">Select an addressable turn in Timeline or Navigator to build an exact multi-turn Stop set.</div>`}
      </section>`;
  }

  function onClick(event) {
    if (event.target.closest("[data-stop-selected]")) {
      controller.stopSelected();
      return;
    }
    if (event.target.closest("[data-clear-selection]")) {
      controller.clearSelection();
      return;
    }
    if (event.target.closest("[data-clear-results]")) {
      controller.clearResults();
      return;
    }
    const retry = event.target.closest("[data-retry-stop]");
    if (retry) {
      const target = controller.targetForKey(retry.dataset.retryStop);
      if (target) controller.stopOne(target);
    }
  }

  element.addEventListener("click", onClick);
  const unsubscribe = controller.subscribe(render);
  render();
  return {
    destroy() {
      unsubscribe();
      element.removeEventListener("click", onClick);
      element.innerHTML = "";
    },
  };
}
