"""
Kestrel Observability Hook — per-agent emitter (producer) of OTel spans.

Emits an OpenTelemetry trace per agent session via :class:`KestrelTracer`
(``kestrel_feature_observability.tracing``): a session root (OpenInference
``AGENT``) with each tool invocation recorded as a child ``tool_span``, and a
closing ``session summary`` span carrying totals. Spans are exported to whatever
``OTEL_EXPORTER_OTLP_ENDPOINT`` points at (e.g. a host-supervised Phoenix). This
hook is purely observational: it never blocks, denies, or modifies anything.

Span shape (robust, real durations, no held-open spans — #42):

- On the first lifecycle event of a session a short **session-marker** root span
  (OpenInference ``AGENT``) is opened AND ended immediately, so the root is
  **exported right away**. Its ``SpanContext`` is kept to parent every child —
  referencing an already-exported parent is valid OTel and renders correctly in
  Phoenix, and means no held-open root that could arrive orphaned for a
  long-lived agent.
- Each ``PostToolUse`` emits a child ``tool_span`` (OpenInference ``TOOL``)
  parented to the session root, carrying the tool name, real duration, and
  success. When no duration is available the span is a zero-duration point span
  (start == end) — never ``start > end`` (a negative duration).
- On ``Stop`` / ``AgentTerminate`` — and defensively on teardown
  (:meth:`close`) — a ``session summary`` span (parented to the root) carries
  session totals (tool count, duration, success ratio). No held-open spans.

Scheduler noise (#42): scheduler-sourced ACTION ticks (``session_id ==
"scheduler"``) that performed no work are the every-minute infra no-op that
buries real traces, so their spans are **not** emitted — only ticks that
executed / deferred / failed something get a span. Set
``KESTREL_OTEL_TRACE_SCHEDULER=1`` to re-enable full tick tracing for debugging.
A scheduler tick's ``tool_response`` is the serialized ``ToolResult`` envelope a
feature tool returns (``{"status": "ok", "confirmation": ..., "data": {...}}``,
plus ``tool``/``success`` from the tool wrapper): the outcome ``status`` sits at
the top level while the machine-readable **work counters live nested under
``data``** (e.g. ``restart_coordinator`` idles at
``data={"executed": False, "pending": 0}``). The no-op filter therefore inspects
BOTH the top level and the nested ``data`` payload — scanning only the top level
never matches the real envelope.

INV-SOLO: when ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the traces-specific var) is
unset, :class:`KestrelTracer` is a no-op — no provider, no exporter, no network —
so the emit path costs nothing and the agent runs unaffected. Prometheus counters
still fire locally.

User-message content is never recorded (never stamped on any span). Exceptions are
swallowed so observability can never affect agent operation.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from kestrel_sdk.hooks.base import Hook, HookEvent, HookInput, HookOutput
from kestrel_sdk.metrics import (
    PROMETHEUS_AVAILABLE,
    HOOK_EVENTS,
    TOOL_CALLS,
    TOOL_DURATION,
)

from kestrel_feature_observability.tracing import (
    KestrelTracer,
    KIND_AGENT,
    KIND_CHAIN,
    configure as configure_tracing,
)

logger = logging.getLogger(__name__)

# Standard Kestrel span attribute for the agent session (no constant in tracing.py).
KESTREL_SESSION_ID = "kestrel.session_id"

# Lifecycle events that terminate a session — emit the summary span.
_TERMINAL_EVENTS = frozenset({"Stop", "AgentTerminate"})

# OTel service name for the per-agent emitter's spans.
_SERVICE_NAME = "kestrel-agent"

# Session id the scheduler stamps on the PRE/POST_TOOL_USE hooks it fires on each
# tick (kestrel_sovereign SchedulerFeature._run_tool_hook_gated). Used to single
# out scheduler-sourced ticks for the no-op noise filter (#42).
_SCHEDULER_SESSION_ID = "scheduler"

# Env opt-in: re-enable full scheduler-tick tracing (including no-op ticks).
_TRACE_SCHEDULER_ENV = "KESTREL_OTEL_TRACE_SCHEDULER"

# Explicit idle / no-op status markers a scheduler ACTION tick may report
# (a tool may stamp one on the envelope's ``status``/``outcome`` or nested in
# ``data``). The serialized ``ToolResult`` top-level ``status`` is ``ok`` — never
# one of these — so matching here only trips on a genuine idle token.
_NOOP_STATUSES = frozenset(
    {"noop", "no_op", "no-op", "idle", "skipped", "unchanged",
     "no_change", "no_changes", "nothing", "none"}
)

# ``ToolResult.status`` values (and kin) that mean the tick did NOT succeed.
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})

# Response keys a scheduler ACTION tick uses to report real work done
# (executed / deferred / failed and kin). A successful tick that reports these
# and finds them all zero/empty did nothing this poll. Feature tools nest these
# under ``data`` (the serialized ``ToolResult`` payload), so the filter scans
# both the top level and ``data`` — e.g. ``restart_coordinator`` reports
# ``data={"executed": [...], "deferred": [...]}`` when it acted.
_WORK_KEYS = (
    "executed", "deferred", "failed", "actioned", "enqueued", "dispatched",
    "processed", "changed", "updated", "signals_emitted", "transitions",
)


def _env_flag(name: str) -> bool:
    """True when env var ``name`` is set to a truthy value (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _tick_success(tool_response: Any) -> bool:
    """Best-effort success for a serialized tool-response envelope.

    A feature tool's ``ToolResult`` carries the canonical outcome in ``status``
    (``ok`` / ``partial`` / ``error``). The in-tree tool wrapper also spreads a
    derived top-level ``success`` bool, but the SDK wrapper for external features
    does **not** — it exposes only ``status``. Prefer an explicit ``success``
    when present, else derive it from ``status`` (falling back to ``error``) so
    an errored tick is never mislabeled a success (#42): otherwise the tool
    span's ``tool.success`` and the summary's ``success_ratio`` would read as
    all-OK for every scheduler session.
    """
    if not isinstance(tool_response, dict):
        return True
    if "success" in tool_response:
        return bool(tool_response["success"])
    status = str(tool_response.get("status") or "").strip().lower()
    if status:
        return status not in _FAILURE_STATUSES
    return not tool_response.get("error")


def _scheduler_tick_did_work(tool_response: Any) -> bool:
    """Whether a scheduler ACTION tick actually did something this poll.

    Emit a span only for ticks that executed / deferred / failed real work; a
    successful tick that reports nothing done is the every-minute infra no-op
    that buries real traces (#42) — e.g. ``restart_coordinator`` returning
    ``{"status": "ok", "data": {"executed": False, "pending": 0}}`` on an idle
    minute. Opaque or unrecognized results default to ``True`` (emit) so a real
    trace is never dropped.

    Feature tools return the serialized ``ToolResult`` envelope: the top level
    carries ``status``/``confirmation``/``error`` (+ ``tool``/``success`` from
    the wrapper) while the **work counters live nested under ``data``**. So both
    the top level and the nested ``data`` payload are inspected for idle markers
    and work counters — scanning only the top level (as the first cut did) never
    matches the real envelope and leaves the filter inert.
    """
    if not isinstance(tool_response, dict):
        return True
    # A failed / errored tick is always worth a span (it "failed something").
    if not _tick_success(tool_response):
        return True
    # Merge the envelope top level with its nested ``data`` payload; feature
    # tools stamp work counters (and any idle marker) under ``data``.
    scopes = [tool_response]
    data = tool_response.get("data")
    if isinstance(data, dict):
        scopes.append(data)
    # Explicit idle / no-op marker in either scope (the envelope's own top-level
    # ``status`` is ``ok`` — never a no-op token — so this only trips on a real
    # idle marker a tool stamps, e.g. ``status``/``outcome`` == "idle").
    for scope in scopes:
        marker = str(
            scope.get("status") or scope.get("outcome") or ""
        ).strip().lower()
        if marker and marker in _NOOP_STATUSES:
            return False
    # Work counters across both scopes: a no-op iff every reported counter is
    # zero/empty (``executed: False``, ``deferred: []``, ...).
    reported = [scope[k] for scope in scopes for k in _WORK_KEYS if k in scope]
    if reported:
        return any(bool(v) for v in reported)
    # No idle marker and no counters → can't prove idle; emit to be safe.
    return True


@dataclass
class _SessionState:
    """Per-session bookkeeping — the exported root + running totals.

    Holds only the ended session-marker span (for its ``SpanContext``) and
    counters — NEVER a held-open span (#42).
    """

    root: Any            # the ended session-marker span (holds the root SpanContext)
    started_ns: int      # session start (marker start) — for the summary duration
    tool_count: int = 0
    success_count: int = 0


class ObservabilityHook(Hook):
    """Per-agent emitter — emits OTel spans (session → tool → summary) via KestrelTracer."""

    def __init__(self, agent):
        super().__init__(
            name="observability",
            events=list(HookEvent),  # ALL events
            priority=999,            # Run LAST — after security, audit, etc.
            timeout=5.0,
        )
        self.agent = agent
        # Frozen at construction. A no-op tracer when OTEL_EXPORTER_OTLP_ENDPOINT
        # (or the traces-specific var) is unset — never blocks, never networks.
        self._tracer: KestrelTracer = configure_tracing(service_name=_SERVICE_NAME)
        # Frozen at construction: whether to trace no-op scheduler ticks too.
        self._trace_scheduler: bool = _env_flag(_TRACE_SCHEDULER_ENV)
        # Per-session state keyed by session_id. Each holds the ALREADY-EXPORTED
        # session-marker root (for its SpanContext) plus running totals — no
        # held-open span anywhere, so nothing can arrive orphaned (#42).
        self._sessions: Dict[Optional[str], _SessionState] = {}

    # ------------------------------------------------------------------
    # Identity / lineage
    # ------------------------------------------------------------------

    def _agent_did(self) -> Optional[str]:
        """Resolve the agent's DID (falls back to None)."""
        return getattr(self.agent, "agent_id", None) or getattr(self.agent, "did", None)

    def _driving_parent(self, input: HookInput) -> Optional[str]:
        """The agent/session driving this agent, if any (else None → self-driven)."""
        return (
            input.parent_did
            or getattr(self.agent, "parent_agent", None)
            or getattr(self.agent, "parent_did", None)
        )

    # ------------------------------------------------------------------
    # Session lifecycle (root exported immediately; no held-open spans)
    # ------------------------------------------------------------------

    def _ensure_session(
        self, session_id: Optional[str], agent_name: str, input: HookInput
    ) -> _SessionState:
        """Return the session state, exporting the root marker on first event."""
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing

        # Self-driven → the agent is its own orchestrator; driven → inherit the
        # process-global orchestrator (env default), if any.
        orchestrator = agent_name if self._driving_parent(input) is None else None

        attributes: Dict[str, Any] = {}
        if session_id:
            attributes[KESTREL_SESSION_ID] = session_id
        agent_did = self._agent_did()
        if agent_did:
            attributes["kestrel.agent_did"] = agent_did

        # Export the session root IMMEDIATELY: a short session-marker span opened
        # AND ended now. We keep its SpanContext (via the returned ended span) to
        # parent every subsequent child. Referencing an already-exported parent is
        # valid OTel and renders correctly in Phoenix — and it means no held-open
        # root that could arrive orphaned for a long-lived agent (#42).
        session_marker_ns = time.time_ns()
        root = self._tracer.emit_span(
            agent_name,
            KIND_AGENT,
            start_time=session_marker_ns,
            end_time=session_marker_ns,
            agent_name=agent_name,
            orchestrator=orchestrator,
            attributes=attributes,
        )
        state = _SessionState(root=root, started_ns=session_marker_ns)
        self._sessions[session_id] = state
        return state

    def _should_emit_tool_span(self, input: HookInput) -> bool:
        """Whether this PostToolUse should be recorded as a span.

        Normal agent tool calls always emit. Scheduler-sourced ticks emit only
        when they did real work — unless ``KESTREL_OTEL_TRACE_SCHEDULER`` opts
        into full tick tracing (#42).
        """
        if input.session_id != _SCHEDULER_SESSION_ID:
            return True
        if self._trace_scheduler:
            return True
        return _scheduler_tick_did_work(input.tool_response)

    def _emit_tool_span(
        self, session: _SessionState, agent_name: str, input: HookInput
    ) -> None:
        """Emit a child tool span for a completed tool call (PostToolUse)."""
        # Derive success from the envelope: prefer top-level ``success`` but fall
        # back to ``status`` for external-feature ToolResults that omit it (#42),
        # so an errored scheduler tick isn't stamped tool.success=True.
        success = _tick_success(input.tool_response)

        extra: Dict[str, Any] = {"tool.success": success}
        if input.execution_time_ms is not None:
            extra["tool.duration_ms"] = input.execution_time_ms

        attributes: Dict[str, Any] = {}
        if input.feature_name:
            attributes["kestrel.feature_name"] = input.feature_name
        if not success and isinstance(input.tool_response, dict):
            # Privacy: truncate error text (never user-message content).
            attributes["tool.error"] = str(input.tool_response.get("error", ""))[:200]

        # PostToolUse fires after the tool completed, so backdate the span start
        # to end − duration → the exported span's duration is the real tool
        # runtime (a correct waterfall), not ~0. The span is explicitly parented
        # to the (already-exported) session root so nesting stays correct.
        end_ns = time.time_ns()
        if input.execution_time_ms is not None:
            start_time = end_ns - int(input.execution_time_ms * 1_000_000)
        else:
            # No duration stamped (e.g. the scheduler path never sets
            # execution_time_ms): make it a zero-duration point span. NEVER emit
            # start > end — the old "now" fallback landed just AFTER end_ns, so
            # every such span had a negative duration (#42).
            start_time = end_ns

        self._tracer.emit_tool_span(
            input.tool_name,
            parent=session.root,
            start_time=start_time,
            end_time=end_ns,
            agent_name=agent_name,
            extra=extra,
            attributes=attributes,
        )
        session.tool_count += 1
        if success:
            session.success_count += 1

    def _emit_session_summary(
        self, state: _SessionState, session_id: Optional[str], agent_name: str
    ) -> None:
        """Emit a ``session summary`` span (parented to the root) with totals."""
        end_ns = time.time_ns()
        tool_count = state.tool_count
        success_ratio = (state.success_count / tool_count) if tool_count else 1.0

        extra: Dict[str, Any] = {
            "kestrel.tool_count": tool_count,
            "kestrel.success_ratio": success_ratio,
            "kestrel.session_duration_ms": (end_ns - state.started_ns) / 1_000_000,
        }
        attributes: Dict[str, Any] = {}
        if session_id:
            attributes[KESTREL_SESSION_ID] = session_id

        # A `session summary` span in the SAME trace, parented to the exported
        # root — carries session totals without any held-open span (#42).
        self._tracer.emit_span(
            "session summary",
            KIND_CHAIN,
            parent=state.root,
            start_time=state.started_ns,
            end_time=end_ns,
            agent_name=agent_name,
            extra=extra,
            attributes=attributes,
        )

    def _close_session(self, session_id: Optional[str], agent_name: str) -> None:
        """Emit the summary span for the session, if one was opened."""
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        try:
            self._emit_session_summary(state, session_id, agent_name)
        except Exception as e:  # noqa: BLE001 - never fatal
            logger.debug(
                "ObservabilityHook summary emit failed (non-fatal): %s", e
            )

    def close(self) -> None:
        """Emit a summary for every open session — defensive teardown."""
        agent_name = getattr(self.agent, "agent_name", "unknown")
        for session_id in list(self._sessions):
            self._close_session(session_id, agent_name)

    def __del__(self):  # best-effort export if the hook is dropped without shutdown
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter teardown safety
            pass

    # ------------------------------------------------------------------
    # Hook entry point
    # ------------------------------------------------------------------

    async def execute(self, input: HookInput) -> HookOutput:
        """Emit OTel spans and bump Prometheus counters. Never blocks."""
        try:
            agent_name = getattr(self.agent, "agent_name", "unknown")
            event_type = input.hook_event_name
            session_id = input.session_id

            # --- OTel spans (no-op when the OTLP endpoint is unset) ---
            try:
                if event_type == "PostToolUse" and input.tool_name:
                    if self._should_emit_tool_span(input):
                        session = self._ensure_session(session_id, agent_name, input)
                        self._emit_tool_span(session, agent_name, input)
                elif event_type in _TERMINAL_EVENTS:
                    self._close_session(session_id, agent_name)
                elif session_id != _SCHEDULER_SESSION_ID:
                    # Real agent session: export the root marker early so children
                    # never arrive orphaned (#42). The scheduler pseudo-session
                    # gets a root only when a work-tick actually needs a parent.
                    self._ensure_session(session_id, agent_name, input)
            except Exception as e:  # noqa: BLE001 - tracing must never break the agent
                logger.debug("ObservabilityHook tracing error (non-fatal): %s", e)

            # --- Prometheus counters (local operational metrics, unchanged) ---
            if PROMETHEUS_AVAILABLE:
                HOOK_EVENTS.labels(event_type=event_type).inc()

                # Tool metrics from PostToolUse events
                if event_type == "PostToolUse" and input.tool_name:
                    success = _tick_success(input.tool_response)
                    TOOL_CALLS.labels(
                        tool_name=input.tool_name, success=str(success)
                    ).inc()
                    if input.execution_time_ms is not None:
                        TOOL_DURATION.labels(
                            tool_name=input.tool_name
                        ).observe(input.execution_time_ms / 1000)

        except Exception as e:
            # Never let observability failures affect agent operation
            logger.debug(f"ObservabilityHook error (non-fatal): {e}")

        return HookOutput.allow()  # NEVER block
