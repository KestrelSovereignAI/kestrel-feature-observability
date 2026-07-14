"""
Kestrel Observability Hook — per-agent emitter (producer) for the fleet store.

POSTs every lifecycle event to the fleet host's observability ingest
(``POST {KESTREL_OBSERVABILITY_URL}/api/host/observability/events``) so the
fleet feature — the single tenant-aware owner of the event store, query routes,
and UI — can reconstruct the fleet. This hook is purely observational: it never
blocks, denies, or modifies anything.

Transport is lightweight and best-effort: an ``httpx.AsyncClient`` POST,
fire-and-forget with a short timeout, all failures swallowed, no buffering or
retry, and no ``entities`` dependency. When ``KESTREL_OBSERVABILITY_URL`` is
unset the emit path is a no-op — the agent still runs, and Prometheus counters
still fire locally.

User-message content is never sent (only its length). Exceptions are swallowed
so observability can never affect agent operation.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Set

from kestrel_sdk.hooks.base import Hook, HookEvent, HookInput, HookOutput
from kestrel_sdk.metrics import (
    PROMETHEUS_AVAILABLE,
    HOOK_EVENTS,
    TOOL_CALLS,
    TOOL_DURATION,
)

try:  # httpx is a lightweight runtime dep; guard so import never breaks the hook
    import httpx
except Exception:  # noqa: BLE001 - degrade to no-op emit when unavailable
    httpx = None

logger = logging.getLogger(__name__)

# Host-root path the fleet observability HostFeature serves ingest under
# (host-scoped namespace — NOT the old ``/api/observability/events``).
INGEST_PATH = "/api/host/observability/events"

# Frozen env vars talon's emitter already uses — do NOT invent new keys.
_URL_ENV = "KESTREL_OBSERVABILITY_URL"
_KEY_ENV = "KESTREL_OBSERVABILITY_KEY"

# Fire-and-forget POST timeout (seconds). Short so a slow or unreachable fleet
# host never stalls the agent.
_POST_TIMEOUT = 2.0

# Maps SDK hook event names → an ``event_type`` the fleet ingest accepts.
# The fleet store 422s any ``event_type`` outside its accepted set
# (tool_call / tool_response / agent_response / subagent_call /
# subagent_response / error / metric / gate_*). Lifecycle hooks with no
# telemetry analogue fall through to ``metric`` (see ``_DEFAULT_EVENT_TYPE``).
EVENT_TYPE_MAP = {
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_response",
    "PreSubagentCall": "subagent_call",
    "PostSubagentCall": "subagent_response",
    "PostResponse": "agent_response",
}

# Lifecycle hooks (SessionStart / UserPromptSubmit / Stop / AgentSpawn /
# AgentTerminate) carry no telemetry-specific type; the fleet accepts ``metric``
# for these operational events. The raw hook name is preserved in
# ``metadata.hook_event_type``.
_DEFAULT_EVENT_TYPE = "metric"


class ObservabilityHook(Hook):
    """Per-agent emitter — POSTs lifecycle events to the fleet observability store."""

    def __init__(self, agent):
        super().__init__(
            name="observability",
            events=list(HookEvent),  # ALL events
            priority=999,            # Run LAST — after security, audit, etc.
            timeout=5.0,
        )
        self.agent = agent
        # Frozen at construction, matching talon's emitter. When the URL is unset
        # the emit path is a no-op.
        self._ingest_url = os.environ.get(_URL_ENV)
        self._api_key = os.environ.get(_KEY_ENV)
        # Hold references to in-flight fire-and-forget POST tasks so they are not
        # garbage-collected mid-flight; discarded on completion.
        self._tasks: Set[asyncio.Task] = set()

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

    def _lineage_metadata(self, input: HookInput) -> Dict[str, Any]:
        """Capture parent/child lineage so the fleet swimlane can nest sublanes."""
        lineage: Dict[str, Any] = {}

        parent_agent = self._driving_parent(input)
        if parent_agent:
            lineage["parent_agent"] = parent_agent

        parent_session = getattr(self.agent, "parent_session_id", None)
        if parent_session:
            lineage["parent_session_id"] = parent_session

        if input.child_did:
            lineage["subagent_id"] = input.child_did
        if input.child_name:
            lineage["child_name"] = input.child_name

        return lineage

    # ------------------------------------------------------------------
    # Emit path
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        agent_name: str,
        hook_event_name: str,
        wire_event_type: str,
        input: HookInput,
    ) -> Dict[str, Any]:
        """Build the fleet-ingest event payload for one lifecycle event.

        The wire shape matches the fleet observability store's ingest contract
        (``agent_name`` / ``event_type`` / ``session_id`` top-level; hook
        details under ``metadata``). ``orchestrator`` is the agent's own display
        name when self-driven, else ``None`` — the fleet store groups by the
        stored ``orchestrator``/``agent_name`` *display* values (no DID
        resolution) and renders a null orchestrator as "Direct".
        """
        agent_did = self._agent_did()
        driven_by = self._driving_parent(input)
        # Self-driven → the agent is its own orchestrator, keyed by the SAME
        # display name the store groups ``agent_name`` on (not the DID, which
        # would never match). Driven → null (Direct).
        orchestrator = agent_name if driven_by is None else None

        # Hook-specific details ride in ``metadata`` (the only free-form field
        # the fleet store persists); the raw hook name + DID live here.
        metadata: Dict[str, Any] = {"hook_event_type": hook_event_name}
        if agent_did:
            metadata["agent_did"] = agent_did
        if input.feature_name:
            metadata["feature_name"] = input.feature_name
        if input.user_message:
            # Privacy: length only, never the content.
            metadata["user_message_length"] = len(input.user_message)
        metadata.update(self._lineage_metadata(input))

        payload: Dict[str, Any] = {
            "event_type": wire_event_type,
            "agent_name": agent_name,
            "session_id": input.session_id,
            "orchestrator": orchestrator,
            "ts": input.timestamp.isoformat() if input.timestamp else None,
            "metadata": metadata,
        }

        if input.tool_name:
            payload["tool_name"] = input.tool_name
        if input.execution_time_ms is not None:
            payload["duration_ms"] = input.execution_time_ms
        if input.tool_response is not None:
            success = (
                input.tool_response.get("success", True)
                if isinstance(input.tool_response, dict)
                else True
            )
            payload["success"] = success
            if not success and isinstance(input.tool_response, dict):
                payload["error_message"] = str(
                    input.tool_response.get("error", "")
                )[:200]

        return payload

    def _schedule_post(self, payload: Dict[str, Any]) -> None:
        """Fire-and-forget the POST without blocking the hook. No-op if unconfigured."""
        if not self._ingest_url or httpx is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop — nothing to schedule onto
            return
        task = loop.create_task(self._post(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, payload: Dict[str, Any]) -> None:
        """Best-effort POST to the fleet ingest. All failures swallowed."""
        url = self._ingest_url.rstrip("/") + INGEST_PATH
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            async with httpx.AsyncClient(timeout=_POST_TIMEOUT) as client:
                await client.post(url, json=payload, headers=headers)
        except Exception as e:  # noqa: BLE001 - no retry, no buffering, never fatal
            logger.debug("ObservabilityHook POST failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Hook entry point
    # ------------------------------------------------------------------

    async def execute(self, input: HookInput) -> HookOutput:
        """Emit the event to the fleet store and bump Prometheus counters. Never blocks."""
        try:
            agent_name = getattr(self.agent, "agent_name", "unknown")
            event_type = input.hook_event_name
            wire_event_type = EVENT_TYPE_MAP.get(event_type, _DEFAULT_EVENT_TYPE)

            # --- Fleet emit (fire-and-forget; no-op when unconfigured) ---
            payload = self._build_payload(
                agent_name, event_type, wire_event_type, input
            )
            self._schedule_post(payload)

            # --- Prometheus counters (local operational metrics, kept here) ---
            if PROMETHEUS_AVAILABLE:
                HOOK_EVENTS.labels(event_type=event_type).inc()

                # Tool metrics from PostToolUse events
                if event_type == "PostToolUse" and input.tool_name:
                    success = payload.get("success", True)
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
