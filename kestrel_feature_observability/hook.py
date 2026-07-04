"""
Kestrel Observability Hook - Default observer for all lifecycle events.

Writes structured entries to ObservabilityStore for every hook event.
This hook is purely observational: it never blocks, denies, or modifies
anything. Exceptions are swallowed to avoid affecting agent operation.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from kestrel_sdk.hooks.base import Hook, HookEvent, HookInput, HookOutput
from kestrel_sdk.metrics import (
    PROMETHEUS_AVAILABLE,
    HOOK_EVENTS,
    TOOL_CALLS,
    TOOL_DURATION,
)

logger = logging.getLogger(__name__)

# Maps hook event names to ObservabilityStore event types
EVENT_TYPE_MAP = {
    "SessionStart": "lifecycle",
    "UserPromptSubmit": "lifecycle",
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_response",
    "PreSubagentCall": "subagent_call",
    "PostSubagentCall": "subagent_response",
    "Stop": "lifecycle",
}

# Hook events that open a structured "call" row (log_tool_call) whose paired
# "response" row (log_tool_response) is an UPDATE keyed on the returned
# event_id. We cache the call's event_id so the paired response can find it.
_CALL_EVENTS = {"PreToolUse": "tool_call", "PreSubagentCall": "subagent_call"}
_RESPONSE_EVENTS = {
    "PostToolUse": ("PreToolUse", "tool_response"),
    "PostSubagentCall": ("PreSubagentCall", "subagent_response"),
}


class ObservabilityHook(Hook):
    """Default observer for all lifecycle events. Writes to ObservabilityStore."""

    def __init__(self, agent):
        super().__init__(
            name="observability",
            events=list(HookEvent),  # ALL events
            priority=999,            # Run LAST — after security, audit, etc.
            timeout=5.0,
        )
        self.agent = agent
        # Correlation cache: (session_id, tool_name, tool_use_id) → event_id of
        # the PreToolUse/PreSubagentCall row, so the paired Post event can pass
        # it to log_tool_response (Option 1 from issue #10 Q2).
        self._pending_calls: Dict[Tuple[Optional[str], Optional[str], Optional[str]], Any] = {}

    def _tool_use_id(self, input: HookInput) -> Optional[str]:
        """Best-effort correlation id shared between a Pre/Post event pair."""
        for source in (input.tool_input, input.tool_response):
            if isinstance(source, dict):
                tuid = source.get("tool_use_id") or source.get("tool_call_id") or source.get("id")
                if tuid:
                    return str(tuid)
        return None

    def _lineage_metadata(self, input: HookInput) -> Dict[str, Any]:
        """Capture parent/child lineage so a swimlane can nest sublanes.

        Sourced from ``HookInput`` (spawn events carry ``parent_did`` /
        ``child_did``) and from the host agent when it exposes a driving
        parent. Only present keys are returned so events stay lean.
        """
        lineage: Dict[str, Any] = {}

        parent_agent = (
            input.parent_did
            or getattr(self.agent, "parent_agent", None)
            or getattr(self.agent, "parent_did", None)
        )
        if parent_agent:
            lineage["parent_agent"] = parent_agent

        parent_session = getattr(self.agent, "parent_session_id", None)
        if parent_session:
            lineage["parent_session_id"] = parent_session

        # For subagent events the child DID / name identify the sublane the
        # child's own events attach to.
        if input.child_did:
            lineage["subagent_id"] = input.child_did
        if input.child_name:
            lineage["child_name"] = input.child_name

        return lineage

    async def _emit_structured(
        self,
        store: Any,
        agent_name: str,
        event_type: str,
        input: HookInput,
        metadata: Dict[str, Any],
    ) -> None:
        """Emit structured tool_call/tool_response (or subagent_*) rows.

        Pairing (issue #10 Q2, Option 1): the Pre event's returned event_id is
        cached keyed by ``(session_id, tool_name, tool_use_id)`` and passed to
        ``log_tool_response`` on the matching Post event. When no cached call is
        found (out-of-order / dropped Pre) the response falls back to a
        standalone insert (``event_id=None``).
        """
        tool_name = input.tool_name or input.child_name
        tool_use_id = self._tool_use_id(input)
        key = (input.session_id, tool_name, tool_use_id)

        if event_type in _CALL_EVENTS:
            structured = dict(metadata)
            structured["event_category"] = _CALL_EVENTS[event_type]
            event_id = await store.log_tool_call(
                agent_name=agent_name,
                session_id=input.session_id,
                tool_name=tool_name,
                metadata=structured,
            )
            if event_id is not None:
                self._pending_calls[key] = event_id
            return

        if event_type in _RESPONSE_EVENTS:
            pre_event, category = _RESPONSE_EVENTS[event_type]
            event_id = self._pending_calls.pop(key, None)
            success = metadata.get("success", True)
            structured = dict(metadata)
            structured["event_category"] = category
            await store.log_tool_response(
                event_id=event_id,
                agent_name=agent_name,
                session_id=input.session_id,
                tool_name=tool_name,
                success=success,
                duration_ms=input.execution_time_ms,
                metadata=structured,
            )

    async def execute(self, input: HookInput) -> HookOutput:
        """Log the event to ObservabilityStore. Never blocks."""
        try:
            store = getattr(self.agent, "observability_store", None)
            if not store:
                logger.warning(
                    "ObservabilityHook: observability_store not initialized — "
                    "event '%s' dropped. This usually means the hook fired "
                    "before store setup completed.",
                    input.hook_event_name,
                )
                return HookOutput.allow()

            agent_name = getattr(self.agent, "agent_name", "unknown")
            event_type = input.hook_event_name
            # Classify the hook event into a broader category for querying
            event_category = EVENT_TYPE_MAP.get(event_type, "lifecycle")

            metadata: Dict[str, Any] = {
                "hook_event": event_type,
                "event_category": event_category,
                "session_id": input.session_id,
            }

            # Parent/lineage capture — enables agent → subagent → talon sublanes.
            metadata.update(self._lineage_metadata(input))

            # Enrich metadata based on event type
            if input.tool_name:
                metadata["tool_name"] = input.tool_name
            if input.feature_name:
                metadata["feature_name"] = input.feature_name
            if input.user_message:
                metadata["user_message_length"] = len(input.user_message)
                # Don't log full user message — privacy
            if input.execution_time_ms is not None:
                metadata["execution_time_ms"] = input.execution_time_ms
            if input.tool_response:
                success = (
                    input.tool_response.get("success", True)
                    if isinstance(input.tool_response, dict)
                    else True
                )
                metadata["success"] = success
                if not success and isinstance(input.tool_response, dict):
                    metadata["error"] = str(
                        input.tool_response.get("error", "")
                    )[:200]

            await store.log_metric(
                agent_name=agent_name,
                metric_name=f"hook.{event_type}",
                metric_value=1,
                metadata=metadata,
            )

            # --- Structured events (in addition to the metric above) ---
            # tool_call / subagent_call open a row; tool_response /
            # subagent_response UPDATE it via the cached event_id. Downstream
            # views group these into tool-call blocks. Kept best-effort: any
            # failure is swallowed by the outer try/except, never the metric.
            await self._emit_structured(store, agent_name, event_type, input, metadata)

            # --- Prometheus counters (fire-and-forget, same try/except) ---
            if PROMETHEUS_AVAILABLE:
                HOOK_EVENTS.labels(event_type=event_type).inc()

                # Tool metrics from PostToolUse events
                if event_type == "PostToolUse" and input.tool_name:
                    success = metadata.get("success", True)
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
