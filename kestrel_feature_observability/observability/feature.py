"""
Kestrel Observability Feature - Always-on lifecycle event observability.

Auto-discovers via the feature system, registers the ObservabilityHook
to log all lifecycle events to ObservabilityStore, and exposes tool
commands for querying observability data at runtime.
"""

import logging
from typing import Any, Dict, List, Optional

from kestrel_sdk.features.base import Feature, tool
from kestrel_feature_observability.observability.hook import ObservabilityHook
from kestrel_sdk.hooks.base import Hook
from kestrel_sdk.tools.base import ToolCategory
from kestrel_sdk.tools.result import ToolResult

logger = logging.getLogger(__name__)


class ObservabilityFeature(Feature):
    """Always-on observability via hook system. Logs all lifecycle events to ObservabilityStore."""

    def __init__(self, agent):
        super().__init__(agent)
        self._hook: Optional[ObservabilityHook] = None

    @property
    def tool_description(self) -> str:
        return "Lifecycle event observability and monitoring"

    def get_router(self):
        """Return the Observability HTTP router for dynamic mounting.

        The router is defined in endpoints/observability.py and mounted by
        the server only when ObservabilityFeature is discovered and enabled.
        """
        from kestrel_feature_observability.endpoints.observability import router
        return router

    async def initialize(self):
        """Create the ObservabilityHook (auto-registered via get_hooks)."""
        self._hook = ObservabilityHook(agent=self.agent)

    def get_hooks(self) -> List[Hook]:
        """Return the observability hook for auto-registration."""
        if self._hook:
            return [self._hook]
        return []

    async def shutdown(self):
        """Clean up hook reference on shutdown."""
        self._hook = None

    @tool(
        "obs_status",
        "Show observability event counts",
        category=ToolCategory.SYSTEM,
        command_prefix="!obs",
    )
    async def obs_status(self) -> ToolResult:
        """Show observability summary: event counts by type, recent errors."""
        store = getattr(self.agent, "observability_store", None)
        if not store:
            return ToolResult.failed(
                "ObservabilityStore not available",
                data={"reason": "agent has no observability_store attribute"},
            )

        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(hours=1)
        # The serialization loop after the queries can raise on
        # schema drift (missing attributes on event records). Cover
        # BOTH the query and the iteration in the same try so any
        # AttributeError lands in the envelope (claude review #1).
        counts: Dict[str, int] = {}
        recent_errors: List[Dict[str, Any]] = []
        try:
            events = await store.query_events(
                event_type="metric", since=since, limit=1000
            )
            error_events = await store.query_events(
                event_type="error", since=since, limit=10
            )
            for e in events:
                hook_event = (
                    e.metadata.get("hook_event", "unknown")
                    if e.metadata else "unknown"
                )
                metric_name = (
                    e.metadata.get("metric_name", "") if e.metadata else ""
                )
                if metric_name.startswith("hook."):
                    counts[hook_event] = counts.get(hook_event, 0) + 1
            for e in error_events:
                recent_errors.append({
                    "timestamp": str(e.timestamp),
                    "error_message": e.error_message,
                    "metadata": e.metadata,
                })
        except Exception as e:
            return ToolResult.failed(str(e), data={"window": "last 1 hour"})

        total_hook_events = sum(counts.values())
        return ToolResult.ok(
            confirmation=(
                f"Observability summary for last 1 hour: "
                f"{total_hook_events} hook event(s), "
                f"{len(recent_errors)} recent error(s)"
            ),
            data={
                "time_window": "last 1 hour",
                "hook_event_counts": counts,
                "total_hook_events": total_hook_events,
                "recent_errors": recent_errors,
            },
        )

    @tool(
        "obs_events",
        "Query recent observability events",
        category=ToolCategory.SYSTEM,
        command_prefix="!obs-events",
    )
    async def obs_events(self, event_type: str = "", limit: int = 20) -> ToolResult:
        """Query recent events, optionally filtered by type.

        Args:
            event_type: Filter by event type (e.g., 'metric', 'error', 'tool_call')
            limit: Maximum events to return (the tail REQUEST — actual
                   count returned may be lower if fewer events exist).
        """
        store = getattr(self.agent, "observability_store", None)
        if not store:
            return ToolResult.failed(
                "ObservabilityStore not available",
                data={"reason": "agent has no observability_store attribute"},
            )

        # Cover both the query AND the serialization loop in the
        # same try — schema drift on event records would otherwise
        # raise AttributeError out of the loop and escape the
        # envelope (claude review #1).
        event_dicts: List[Dict[str, Any]] = []
        try:
            events = await store.query_events(
                event_type=event_type if event_type else None,
                limit=limit,
            )
            for e in events:
                event_dicts.append({
                    "event_id": e.event_id,
                    "timestamp": str(e.timestamp),
                    "agent_name": e.agent_name,
                    "event_type": e.event_type,
                    "tool_name": e.tool_name,
                    "duration_ms": e.duration_ms,
                    "success": e.success,
                    "error_message": e.error_message,
                    "metadata": e.metadata,
                })
        except Exception as e:
            return ToolResult.failed(
                str(e),
                data={"event_type": event_type or None, "limit_requested": limit},
            )

        # Honesty: phrase the confirmation as the REQUEST + the
        # actual count, never claim "Retrieved N" when fewer came
        # back (#1042).
        filter_clause = (
            f" of type '{event_type}'" if event_type else ""
        )
        return ToolResult.ok(
            confirmation=(
                f"Retrieved {len(event_dicts)} event(s){filter_clause} "
                f"(limit requested: {limit})"
            ),
            data={
                "events": event_dicts,
                "count": len(event_dicts),
                "limit_requested": limit,
                "event_type": event_type or None,
            },
        )
