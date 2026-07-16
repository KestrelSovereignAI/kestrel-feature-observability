"""
Kestrel Observability Hook — per-agent emitter (producer) of OTel spans.

Emits an OpenTelemetry trace per agent lifecycle via :class:`KestrelTracer`
(``kestrel_feature_observability.tracing``): a session/agent ``run_span`` with each
tool invocation recorded as a child ``tool_span``. Spans are exported to whatever
``OTEL_EXPORTER_OTLP_ENDPOINT`` points at (e.g. a host-supervised Phoenix). This
hook is purely observational: it never blocks, denies, or modifies anything.

Span shape (robust, real durations — no Pre/Post tool-span pairing):

- A ``run_span`` (OpenInference ``AGENT``) is opened lazily on the first lifecycle
  event of a session and held open on ``self``, keyed by ``session_id``.
- Each ``PostToolUse`` emits a child ``tool_span`` (OpenInference ``TOOL``) parented
  to the session's run span, carrying the tool name, duration, and success.
- The run span is closed on ``Stop`` / ``AgentTerminate`` — and defensively on
  hook teardown (:meth:`close`) so an in-flight run always exports.

INV-SOLO: when ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the traces-specific var) is
unset, :class:`KestrelTracer` is a no-op — no provider, no exporter, no network —
so the emit path costs nothing and the agent runs unaffected. Prometheus counters
still fire locally.

User-message content is never recorded (never stamped on any span). Exceptions are
swallowed so observability can never affect agent operation.
"""

import logging
import time
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
    configure as configure_tracing,
)

logger = logging.getLogger(__name__)

# Standard Kestrel span attribute for the agent session (no constant in tracing.py).
KESTREL_SESSION_ID = "kestrel.session_id"

# Lifecycle events that terminate a run — close the held run span so it exports.
_TERMINAL_EVENTS = frozenset({"Stop", "AgentTerminate"})

# OTel service name for the per-agent emitter's spans.
_SERVICE_NAME = "kestrel-agent"


class ObservabilityHook(Hook):
    """Per-agent emitter — emits OTel spans (session → tool) via KestrelTracer."""

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
        # Open run spans keyed by session_id → the live (non-current) run span.
        # Started via ``start_run_span`` so it is NEVER attached to the ambient
        # OTel context — it can't leak parentage onto unrelated spans, and
        # overlapping sessions stay separate traces. Ended on the terminal event
        # / teardown (an unended span never exports).
        self._runs: Dict[Optional[str], Any] = {}

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
    # Run-span lifecycle (held open across invocations)
    # ------------------------------------------------------------------

    def _ensure_run_span(
        self, session_id: Optional[str], agent_name: str, input: HookInput
    ) -> Any:
        """Return the session's run span, opening (and holding) it on first event."""
        existing = self._runs.get(session_id)
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

        # start_run_span (NOT run_span): the span is held open across events but
        # never made current, so it can't leak into the ambient OTel context.
        span = self._tracer.start_run_span(
            agent_name,
            agent_name=agent_name,
            orchestrator=orchestrator,
            attributes=attributes,
        )
        self._runs[session_id] = span
        return span

    def _emit_tool_span(
        self, run_span: Any, agent_name: str, input: HookInput
    ) -> None:
        """Emit a child tool span for a completed tool call (PostToolUse)."""
        success = (
            input.tool_response.get("success", True)
            if isinstance(input.tool_response, dict)
            else True
        )

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
        # to the held run span (it is never current) so nesting stays correct.
        end_ns = time.time_ns()
        start_ns = None
        if input.execution_time_ms is not None:
            start_ns = end_ns - int(input.execution_time_ms * 1_000_000)

        self._tracer.emit_tool_span(
            input.tool_name,
            parent=run_span,
            start_time=start_ns,
            end_time=end_ns,
            agent_name=agent_name,
            extra=extra,
            attributes=attributes,
        )

    def _close_run_span(self, session_id: Optional[str]) -> None:
        """Close and export the session's run span, if open."""
        span = self._runs.pop(session_id, None)
        if span is None:
            return
        try:
            span.end()
        except Exception as e:  # noqa: BLE001 - never fatal
            logger.debug("ObservabilityHook run-span close failed (non-fatal): %s", e)

    def close(self) -> None:
        """Close every open run span — defensive teardown so runs always export."""
        for session_id in list(self._runs):
            self._close_run_span(session_id)

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
                run_span = self._ensure_run_span(session_id, agent_name, input)
                if event_type == "PostToolUse" and input.tool_name:
                    self._emit_tool_span(run_span, agent_name, input)
                elif event_type in _TERMINAL_EVENTS:
                    self._close_run_span(session_id)
            except Exception as e:  # noqa: BLE001 - tracing must never break the agent
                logger.debug("ObservabilityHook tracing error (non-fatal): %s", e)

            # --- Prometheus counters (local operational metrics, unchanged) ---
            if PROMETHEUS_AVAILABLE:
                HOOK_EVENTS.labels(event_type=event_type).inc()

                # Tool metrics from PostToolUse events
                if event_type == "PostToolUse" and input.tool_name:
                    success = (
                        input.tool_response.get("success", True)
                        if isinstance(input.tool_response, dict)
                        else True
                    )
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
