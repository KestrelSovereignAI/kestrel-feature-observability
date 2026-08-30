"""
Tests for the ObservabilityHook (per-agent OTel emitter) and ObservabilityFeature.

Covers:
1. Hook registers on all events
2. Hook always returns ALLOW (never blocks)
3. Hook emits an OTel trace (session run span → tool spans) via KestrelTracer
4. Hook is a no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset
5. Hook swallows tracing failures
6. Feature registers hook during initialize() / closes spans on shutdown
7. Privacy: user_message content is NOT stamped on any span
8. Privacy: tool error truncated to 200 chars
9. orchestrator = agent when self-driven, else inherited (driven)
10. Prometheus metrics still emitted
"""

from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kestrel_sdk.hooks.base import (
    HookEvent,
    HookInput,
    PermissionDecision,
)
from kestrel_feature_observability.hook import (
    ObservabilityHook,
    KESTREL_SESSION_ID,
    OPENINFERENCE_SESSION_ID,
    KESTREL_TURN_ID,
    KESTREL_TURN_INDEX,
    KESTREL_MARKER,
    KESTREL_TOOL_OUTCOME,
    KESTREL_DENIED_COUNT,
    KESTREL_INCOMPLETE_COUNT,
    KESTREL_IDLE_COUNT,
    TOOL_OUTCOME_COMPLETED,
    TOOL_OUTCOME_IDLE,
    TOOL_OUTCOME_INCOMPLETE,
    _MAX_PENDING_PER_TOOL,
)
from kestrel_feature_observability.feature import ObservabilityFeature
from kestrel_feature_observability.tracing import (
    KESTREL_AGENT_NAME,
    KESTREL_ORCHESTRATOR,
    KestrelTracer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(agent_name="test-agent", agent_id="did:agent:test"):
    """Create a stand-in agent with an identity (no auto-created attrs)."""
    turn_number = 0

    def get_current_turn_id():
        nonlocal turn_number
        turn_number += 1
        return f"turn-test-{turn_number}"

    return SimpleNamespace(
        agent_name=agent_name,
        agent_id=agent_id,
        get_current_turn_id=get_current_turn_id,
    )


def _make_input(event_name="PreToolUse", **overrides):
    """Create a HookInput for testing."""
    defaults = {
        "session_id": "sess-1",
        "hook_event_name": event_name,
    }
    defaults.update(overrides)
    return HookInput(**defaults)


def _memory_hook(agent=None, defaults=None):
    """Build a hook whose KestrelTracer exports to an in-memory span exporter.

    Returns ``(hook, exporter)``. Patches ``configure`` so construction wires the
    memory-backed tracer instead of a real OTLP exporter.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = KestrelTracer(
        tracer=provider.get_tracer("test"), defaults=defaults or {}
    )
    agent = agent or _make_agent()
    with patch(
        "kestrel_feature_observability.hook.configure_tracing", return_value=tracer
    ):
        hook = ObservabilityHook(agent=agent)
    return hook, exporter


def _by_name(spans):
    return {s.name: s for s in spans}


# ---------------------------------------------------------------------------
# 1. Hook registers on all events
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_registers_on_all_hook_events(self):
        hook, _ = _memory_hook()
        assert set(hook.events) == set(HookEvent)

    def test_priority_is_999(self):
        hook, _ = _memory_hook()
        assert hook.priority == 999

    def test_name_is_observability(self):
        hook, _ = _memory_hook()
        assert hook.name == "observability"

    def test_timeout_is_5_seconds(self):
        hook, _ = _memory_hook()
        assert hook.timeout == 5.0


# ---------------------------------------------------------------------------
# 2. Hook always returns ALLOW
# ---------------------------------------------------------------------------

class TestHookAlwaysAllows:
    @pytest.mark.asyncio
    async def test_returns_allow_on_pre_tool_use(self):
        hook, _ = _memory_hook()
        result = await hook.execute(_make_input("PreToolUse", tool_name="some_tool"))
        assert result.continue_execution is True
        assert result.permission_decision == PermissionDecision.ALLOW

    @pytest.mark.asyncio
    async def test_returns_allow_on_stop(self):
        hook, _ = _memory_hook()
        result = await hook.execute(_make_input("Stop"))
        assert result.continue_execution is True

    @pytest.mark.asyncio
    async def test_returns_allow_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            hook = ObservabilityHook(agent=_make_agent())
        result = await hook.execute(_make_input("PreToolUse"))
        assert result.continue_execution is True


# ---------------------------------------------------------------------------
# 3. Hook emits an OTel trace (run span → tool spans)
# ---------------------------------------------------------------------------

class TestHookEmitsSpans:
    @pytest.mark.asyncio
    async def test_post_tool_use_emits_child_tool_span(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse",
                tool_name="Bash",
                execution_time_ms=42,
                tool_response={"success": True, "result": "ok"},
            )
        )
        tool = _by_name(exporter.get_finished_spans()).get("Bash")
        assert tool is not None
        assert tool.attributes[KESTREL_AGENT_NAME] == "test-agent"
        assert tool.attributes["tool.duration_ms"] == 42
        assert tool.attributes["tool.success"] is True
        assert tool.attributes["openinference.span.kind"] == "TOOL"

    @pytest.mark.asyncio
    async def test_run_span_exports_on_stop_with_session(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("Stop"))
        run = _by_name(exporter.get_finished_spans()).get("test-agent")
        assert run is not None
        assert run.attributes[KESTREL_SESSION_ID] == "sess-1"
        assert run.attributes[KESTREL_AGENT_NAME] == "test-agent"
        assert run.attributes["openinference.span.kind"] == "AGENT"

    @pytest.mark.asyncio
    async def test_tool_span_is_child_of_run_span(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse",
                tool_name="Bash",
                execution_time_ms=1,
                tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop"))
        spans = _by_name(exporter.get_finished_spans())
        run, tool = spans["test-agent"], spans["Bash"]
        assert tool.context.trace_id == run.context.trace_id
        assert tool.parent.span_id == run.context.span_id

    @pytest.mark.asyncio
    async def test_run_span_opened_once_per_session(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("PreToolUse", tool_name="t"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="t",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop"))
        runs = [s for s in exporter.get_finished_spans() if s.name == "test-agent"]
        assert len(runs) == 1

    @pytest.mark.asyncio
    async def test_agent_terminate_also_closes_run_span(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("AgentTerminate"))
        assert _by_name(exporter.get_finished_spans()).get("test-agent") is not None

    @pytest.mark.asyncio
    async def test_tool_span_carries_feature_name(self):
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="t", feature_name="SecurityFeature",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        tool = _by_name(exporter.get_finished_spans())["t"]
        assert tool.attributes["kestrel.feature_name"] == "SecurityFeature"


# ---------------------------------------------------------------------------
# 3b. Held run span must NOT leak into the ambient OTel context
# ---------------------------------------------------------------------------

class TestNoAmbientContextLeak:
    @pytest.mark.asyncio
    async def test_interleaved_sessions_are_separate_traces(self):
        """Two overlapping sessions must not nest under one another."""
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart", session_id="s1"))
        await hook.execute(_make_input("SessionStart", session_id="s2"))
        await hook.execute(_make_input("Stop", session_id="s2"))
        await hook.execute(_make_input("Stop", session_id="s1"))

        runs = [s for s in exporter.get_finished_spans() if s.name == "test-agent"]
        assert len(runs) == 2
        # Distinct traces, and neither run span is the parent of the other.
        assert runs[0].context.trace_id != runs[1].context.trace_id
        assert runs[0].parent is None
        assert runs[1].parent is None

    @pytest.mark.asyncio
    async def test_unrelated_span_after_session_start_is_not_parented(self):
        """A span created after SessionStart must not inherit the run span."""
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = KestrelTracer(tracer=provider.get_tracer("test"))
        with patch(
            "kestrel_feature_observability.hook.configure_tracing", return_value=tracer
        ):
            hook = ObservabilityHook(agent=_make_agent())

        await hook.execute(_make_input("SessionStart"))
        # An unrelated span opened while the run span is held must be a root.
        with provider.get_tracer("other").start_as_current_span("unrelated"):
            pass
        await hook.execute(_make_input("Stop"))

        unrelated = _by_name(exporter.get_finished_spans())["unrelated"]
        assert unrelated.parent is None

    @pytest.mark.asyncio
    async def test_markers_are_roots_even_inside_ambient_span(self):
        """Session/turn markers must be fresh trace roots even when the hook runs
        inside an instrumented (ambient) span — never swallowed into a host trace.

        Regression for #55 P1: ``emit_span`` used ``context=None``, so OTel parented
        the marker to whatever span was current. The session marker AND the turn
        root must each start a distinct trace with no parent.
        """
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = KestrelTracer(tracer=provider.get_tracer("test"))
        with patch(
            "kestrel_feature_observability.hook.configure_tracing", return_value=tracer
        ):
            hook = ObservabilityHook(agent=_make_agent())

        # Drive the lifecycle while an unrelated host span is current, as would
        # happen if the host request handler is itself OTel-instrumented.
        with provider.get_tracer("host").start_as_current_span("host-request") as host:
            host_trace_id = host.get_span_context().trace_id
            await hook.execute(_make_input("SessionStart"))
            await hook.execute(_make_input("UserPromptSubmit"))

        spans = _by_name(exporter.get_finished_spans())
        session_root = spans["test-agent"]
        turn_root = spans["test-agent turn 1"]
        # Neither marker inherits the ambient host span…
        assert session_root.parent is None
        assert turn_root.parent is None
        # …and each is its own trace, distinct from the host and from each other.
        assert session_root.context.trace_id != host_trace_id
        assert turn_root.context.trace_id != host_trace_id
        assert turn_root.context.trace_id != session_root.context.trace_id


# ---------------------------------------------------------------------------
# 3c. Tool span duration reflects the real tool runtime (backdated start)
# ---------------------------------------------------------------------------

class TestToolSpanDuration:
    @pytest.mark.asyncio
    async def test_tool_span_duration_matches_execution_time(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse",
                tool_name="Bash",
                execution_time_ms=42,
                tool_response={"success": True},
            )
        )
        tool = _by_name(exporter.get_finished_spans())["Bash"]
        # start_time/end_time are epoch-ns; duration must be the real 42ms, not ~0.
        assert tool.end_time - tool.start_time == 42 * 1_000_000


# ---------------------------------------------------------------------------
# 3d. Negative durations — never emit start > end (#42 defect 2)
# ---------------------------------------------------------------------------

class TestNoNegativeDurations:
    @pytest.mark.asyncio
    async def test_duration_present_is_non_negative(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=42, tool_response={"success": True},
            )
        )
        tool = _by_name(exporter.get_finished_spans())["Bash"]
        assert tool.end_time >= tool.start_time
        assert tool.end_time - tool.start_time == 42 * 1_000_000

    @pytest.mark.asyncio
    async def test_missing_duration_is_zero_duration_not_negative(self):
        # The scheduler path never stamps execution_time_ms; the fallback must be
        # a zero-duration point span (start == end), NEVER start > end (#42).
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=None, tool_response={"success": True},
            )
        )
        tool = _by_name(exporter.get_finished_spans())["Bash"]
        assert tool.end_time == tool.start_time
        assert tool.end_time >= tool.start_time

    @pytest.mark.asyncio
    async def test_scheduler_work_tick_without_duration_is_non_negative(self):
        # Real scheduler cron span: session_id="scheduler", no execution_time_ms,
        # and the real serialized ToolResult envelope (counters under `data`).
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response={
                    "status": "ok",
                    "confirmation": "restart_coordinator: pending=1 executed=1",
                    "data": {"pending": 1, "executed": [{"request_id": "r1"}]},
                    "tool": "restart_coordinator",
                    "success": True,
                },
            )
        )
        tool = _by_name(exporter.get_finished_spans())["restart_coordinator"]
        assert tool.end_time >= tool.start_time


# ---------------------------------------------------------------------------
# 3e. Scheduler heartbeats: idle ticks EMIT, labeled `idle` (#87, retiring #42)
# ---------------------------------------------------------------------------

class TestSchedulerHeartbeats:
    # These exercise the REAL production contract: the every-minute
    # ``restart_coordinator`` cron ACTION goes through the scheduler's tool-lookup
    # path (it is a feature @tool, not a builtin_handler), so it fires the
    # PostToolUse hook with ``session_id="scheduler"`` and a serialized
    # ``ToolResult`` envelope — outcome ``status`` at the top level, work counters
    # nested under ``data`` (verified against kestrel-sovereign
    # restart_coordinator/feature.py + the tool wrapper's ToolResult.to_dict()).
    #
    # #42 DROPPED these idle ticks as noise, which made an idle-but-alive
    # scheduler indistinguishable from a dead one. They now always emit, labeled
    # ``kestrel.tool_outcome=idle`` so idle vs work is legible at render time.

    # The exact idle envelope restart_coordinator emits every idle minute.
    IDLE_RESPONSE = {
        "status": "ok",
        "confirmation": "No pending restart requests",
        "data": {"executed": False, "pending": 0},
        "tool": "restart_coordinator",
        "success": True,
    }

    @pytest.mark.asyncio
    async def test_idle_restart_coordinator_tick_emits_idle_span(self):
        # The real every-minute heartbeat: counters (executed) live under `data`,
        # not at the top level. It used to produce NO span at all (#42); it now
        # produces one, stamped `idle` — visibly alive, distinct from work.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        tick = _by_name(exporter.get_finished_spans())["restart_coordinator"]
        assert tick.attributes[KESTREL_TOOL_OUTCOME] == TOOL_OUTCOME_IDLE
        # It RAN — it just did nothing. Never mislabeled a failure.
        assert tick.attributes["tool.success"] is True

    @pytest.mark.asyncio
    async def test_noop_tick_with_nested_idle_status_emits_idle_span(self):
        # A tool that stamps an explicit idle marker inside its `data` payload.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="signal_dispatch",
                tool_response={
                    "status": "ok",
                    "confirmation": "Nothing to dispatch",
                    "data": {"outcome": "idle", "dispatched": 0},
                    "tool": "signal_dispatch",
                    "success": True,
                },
            )
        )
        tick = _by_name(exporter.get_finished_spans())["signal_dispatch"]
        assert tick.attributes[KESTREL_TOOL_OUTCOME] == TOOL_OUTCOME_IDLE

    @pytest.mark.asyncio
    async def test_idle_tick_is_a_zero_duration_point_span(self):
        # The scheduler never stamps execution_time_ms, so a heartbeat is an
        # instant point event — never a bar claiming runtime it didn't have.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        tick = _by_name(exporter.get_finished_spans())["restart_coordinator"]
        assert tick.end_time == tick.start_time

    @pytest.mark.asyncio
    async def test_tick_that_executed_work_emits_span(self):
        # restart_coordinator that actually executed a restart: `data.executed`
        # is a non-empty list.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response={
                    "status": "ok",
                    "confirmation": "restart_coordinator: pending=1 executed=1 deferred=0",
                    "data": {
                        "pending": 1,
                        "executed": [{"request_id": "r1"}],
                        "deferred": [],
                    },
                    "tool": "restart_coordinator",
                    "success": True,
                },
            )
        )
        tick = _by_name(exporter.get_finished_spans())["restart_coordinator"]
        # A tick that DID something is `completed`, never a heartbeat.
        assert tick.attributes[KESTREL_TOOL_OUTCOME] == TOOL_OUTCOME_COMPLETED

    @pytest.mark.asyncio
    async def test_tick_that_only_deferred_emits_span(self):
        # A tick that deferred (but executed nothing) still did work.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response={
                    "status": "ok",
                    "confirmation": "restart_coordinator: pending=1 executed=0 deferred=1",
                    "data": {
                        "pending": 1,
                        "executed": [],
                        "deferred": [{"request_id": "r1", "reason": "unsafe"}],
                    },
                    "tool": "restart_coordinator",
                    "success": True,
                },
            )
        )
        assert (
            _by_name(exporter.get_finished_spans()).get("restart_coordinator")
            is not None
        )

    @pytest.mark.asyncio
    async def test_failed_tick_emits_span(self):
        # A tick that "failed something" is always worth a span. The ERROR
        # envelope carries status="error" (+ success=False from the in-tree
        # wrapper); its data counters are zero but the failure still emits.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response={
                    "status": "error",
                    "error": "Restart coordinator storage unavailable",
                    "data": {"executed": False, "pending": 0},
                    "tool": "restart_coordinator",
                    "success": False,
                },
            )
        )
        assert (
            _by_name(exporter.get_finished_spans()).get("restart_coordinator")
            is not None
        )

    @pytest.mark.asyncio
    async def test_external_error_without_top_level_success_emits_span(self):
        # External features use the SDK tool wrapper, which spreads to_dict() but
        # does NOT add a top-level `success` — only `status`. An errored tick must
        # still emit (success derived from status="error"), and never be dropped
        # as a zero-counter no-op.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="ext_action",
                tool_response={
                    "status": "error",
                    "error": "boom",
                    "data": {"executed": False},
                    "tool": "ext_action",
                },
            )
        )
        assert _by_name(exporter.get_finished_spans()).get("ext_action") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["1", "0", "", "off"])
    async def test_legacy_trace_scheduler_env_is_an_accepted_no_op(self, flag):
        # The #42 opt-in is DEPRECATED (#87): parsed without error, but it can no
        # longer change anything — there is no suppression path left to enable or
        # disable, so an idle tick emits identically at every setting.
        with patch.dict("os.environ", {"KESTREL_OTEL_TRACE_SCHEDULER": flag}):
            hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        tick = _by_name(exporter.get_finished_spans())["restart_coordinator"]
        assert tick.attributes[KESTREL_TOOL_OUTCOME] == TOOL_OUTCOME_IDLE

    @pytest.mark.asyncio
    async def test_idle_label_is_scheduler_only(self):
        # A normal agent tool call is `completed` even with an idle-looking
        # response: `idle` means "scheduler heartbeat", not "returned zeroes".
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        tick = _by_name(exporter.get_finished_spans())["restart_coordinator"]
        assert tick.attributes[KESTREL_TOOL_OUTCOME] == TOOL_OUTCOME_COMPLETED

    @pytest.mark.asyncio
    async def test_idle_ticks_ride_on_idle_count_not_the_success_ratio(self):
        # The #84 precedent for denied/incomplete: additive key, excluded from
        # tool_count / error_count / success_ratio, so the every-minute heartbeat
        # can't drown the ratio of the ticks that actually worked.
        hook, exporter = _memory_hook()
        for _ in range(3):
            await hook.execute(
                _make_input(
                    "PostToolUse", session_id="scheduler",
                    tool_name="restart_coordinator",
                    tool_response=self.IDLE_RESPONSE,
                )
            )
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response={
                    "status": "ok",
                    "data": {"executed": [{"request_id": "r1"}]},
                    "success": True,
                },
            )
        )
        await hook.execute(
            _make_input("AgentTerminate", session_id="scheduler")
        )
        summary = _by_name(exporter.get_finished_spans())["session summary"]
        assert summary.attributes[KESTREL_IDLE_COUNT] == 3
        assert summary.attributes["kestrel.tool_count"] == 1
        assert summary.attributes["kestrel.error_count"] == 0
        assert summary.attributes["kestrel.success_ratio"] == 1.0


# ---------------------------------------------------------------------------
# 3e-bis. tool.success derived from ToolResult status (#42 P3)
# ---------------------------------------------------------------------------

class TestToolSuccessDerivation:
    @pytest.mark.asyncio
    async def test_error_envelope_without_success_key_stamps_false(self):
        # External-feature ToolResult (SDK wrapper) has no top-level `success` —
        # only `status`. tool.success must be derived from status="error", not
        # default to True.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="ext_action",
                tool_response={
                    "status": "error",
                    "error": "boom",
                    "data": {},
                    "tool": "ext_action",
                },
            )
        )
        tool = _by_name(exporter.get_finished_spans())["ext_action"]
        assert tool.attributes["tool.success"] is False
        assert tool.attributes["tool.error"] == "boom"

    @pytest.mark.asyncio
    async def test_partial_and_ok_status_are_success(self):
        # PARTIAL succeeded enough to produce a confirmation → success=True.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="ext_action",
                tool_response={
                    "status": "partial",
                    "confirmation": "Saved with degraded indexing",
                    "error": "index lag",
                    "tool": "ext_action",
                },
            )
        )
        tool = _by_name(exporter.get_finished_spans())["ext_action"]
        assert tool.attributes["tool.success"] is True

    @pytest.mark.asyncio
    async def test_summary_success_ratio_reflects_status_only_envelopes(self):
        # Two external ToolResult ticks (no top-level `success`): one ok, one
        # error. The per-turn summary success_ratio must be 0.5, not 1.0.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="a",
                tool_response={"status": "ok", "confirmation": "done", "tool": "a"},
            )
        )
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="b",
                tool_response={"status": "error", "error": "nope", "tool": "b"},
            )
        )
        await hook.execute(_make_input("Stop"))
        summary = _by_name(exporter.get_finished_spans())["turn 1 summary"]
        assert summary.attributes["kestrel.success_ratio"] == 0.5


# ---------------------------------------------------------------------------
# 3f. Session root exported immediately + summary span (#42 defect 3)
# ---------------------------------------------------------------------------

class TestSessionRootAndSummary:
    @pytest.mark.asyncio
    async def test_root_exported_immediately_before_children(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        # Root (session-marker) is exported right away — no held-open span.
        assert [s.name for s in exporter.get_finished_spans()] == ["test-agent"]
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert names.index("test-agent") < names.index("Bash")
        root, child = _by_name(spans)["test-agent"], _by_name(spans)["Bash"]
        assert child.parent.span_id == root.context.span_id
        assert child.context.trace_id == root.context.trace_id

    @pytest.mark.asyncio
    async def test_child_exports_without_terminal_event(self):
        # No held-open span: root + child are exported even with no Stop.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        spans = _by_name(exporter.get_finished_spans())
        assert spans.get("test-agent") is not None
        assert spans.get("Bash") is not None

    @pytest.mark.asyncio
    async def test_summary_parented_to_turn_root_carries_totals(self):
        # On Stop the per-cycle summary is a `turn <n> summary` parented to the
        # turn root (NOT the misnamed "session summary" of old), carrying the
        # per-turn totals.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="a",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="b",
                execution_time_ms=1, tool_response={"success": False},
            )
        )
        await hook.execute(_make_input("Stop"))
        spans = _by_name(exporter.get_finished_spans())
        turn_root, summary = spans["test-agent turn 1"], spans["turn 1 summary"]
        assert summary.parent.span_id == turn_root.context.span_id
        assert summary.context.trace_id == turn_root.context.trace_id
        assert summary.attributes["kestrel.tool_count"] == 2
        assert summary.attributes["kestrel.success_ratio"] == 0.5
        assert summary.attributes["openinference.span.kind"] == "CHAIN"
        assert summary.end_time >= summary.start_time


# ---------------------------------------------------------------------------
# 3g. First-class turn spans: session ⊃ turn ⊃ tool ⊃ markers (#55)
# ---------------------------------------------------------------------------

class TestTurnSpans:
    async def _drive_turn(self, hook, *, prompt_session="sess-1"):
        """SessionStart → UserPromptSubmit → PreToolUse → PostToolUse → Stop."""
        await hook.execute(_make_input("SessionStart", session_id=prompt_session))
        await hook.execute(_make_input("UserPromptSubmit", session_id=prompt_session))
        await hook.execute(
            _make_input("PreToolUse", session_id=prompt_session, tool_name="Bash")
        )
        await hook.execute(
            _make_input(
                "PostToolUse", session_id=prompt_session, tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop", session_id=prompt_session))

    @pytest.mark.asyncio
    async def test_user_prompt_submit_emits_labeled_turn_root(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        turn = _by_name(exporter.get_finished_spans()).get("test-agent turn 1")
        assert turn is not None
        assert turn.attributes["openinference.span.kind"] == "AGENT"
        assert turn.attributes[KESTREL_MARKER] == "start"
        assert turn.attributes[KESTREL_SESSION_ID] == "sess-1"
        assert turn.attributes[KESTREL_TURN_ID] == "turn-test-1"
        assert turn.attributes[KESTREL_TURN_INDEX] == 1

    @pytest.mark.asyncio
    async def test_turn_root_is_a_new_trace_root(self):
        # One-trace-per-turn: the turn root has no parent and a distinct trace
        # from the session-marker root.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        spans = _by_name(exporter.get_finished_spans())
        session_root, turn_root = spans["test-agent"], spans["test-agent turn 1"]
        assert turn_root.parent is None
        assert turn_root.context.trace_id != session_root.context.trace_id

    @pytest.mark.asyncio
    async def test_tool_span_parents_to_current_turn(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        spans = _by_name(exporter.get_finished_spans())
        turn_root, tool = spans["test-agent turn 1"], spans["Bash"]
        assert tool.parent.span_id == turn_root.context.span_id
        assert tool.context.trace_id == turn_root.context.trace_id

    @pytest.mark.asyncio
    async def test_pre_tool_use_emits_start_marker(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        spans = _by_name(exporter.get_finished_spans())
        marker, turn_root = spans["Bash (started)"], spans["test-agent turn 1"]
        assert marker.attributes["openinference.span.kind"] == "TOOL"
        assert marker.attributes[KESTREL_MARKER] == "start"
        assert marker.attributes["tool.name"] == "Bash"
        assert marker.attributes[KESTREL_TURN_ID] == "turn-test-1"
        # Point span (instant), parented to the current turn.
        assert marker.end_time == marker.start_time
        assert marker.parent.span_id == turn_root.context.span_id

    @pytest.mark.asyncio
    async def test_start_marker_is_attribute_light(self):
        # Keep it lean: no duration/feature/error, just name + session/turn ids.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(
            _make_input("PreToolUse", tool_name="Bash", feature_name="Sec")
        )
        marker = _by_name(exporter.get_finished_spans())["Bash (started)"]
        assert "tool.duration_ms" not in marker.attributes
        assert "kestrel.feature_name" not in marker.attributes

    @pytest.mark.asyncio
    async def test_executed_tool_is_exactly_one_terminal_span(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        tools = [
            s for s in exporter.get_finished_spans()
            if s.attributes["openinference.span.kind"] == "TOOL"
            and KESTREL_MARKER not in s.attributes
        ]
        assert len(tools) == 1
        assert tools[0].name == "Bash"
        assert tools[0].attributes["tool.name"] == "Bash"
        assert tools[0].attributes[KESTREL_TOOL_OUTCOME] == "completed"
        assert tools[0].end_time - tools[0].start_time == 5_000_000

    @pytest.mark.asyncio
    async def test_pre_tool_use_still_exports_the_session_root_early(self):
        # The session root is still exported on the first event so neither the
        # marker nor a later tool span ever arrives orphaned (#42).
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        assert "test-agent" in _by_name(exporter.get_finished_spans())

    @pytest.mark.asyncio
    async def test_session_ids_on_every_span_shape(self):
        hook, exporter = _memory_hook()
        await self._drive_turn(hook)
        await hook.execute(_make_input("AgentTerminate"))
        spans = _by_name(exporter.get_finished_spans())
        assert OPENINFERENCE_SESSION_ID == "session.id"
        # Cover every lifecycle-emitter shape, not only the session marker.
        expected = {
            "test-agent",
            "test-agent turn 1",
            "Bash (started)",
            "Bash",
            "turn 1 summary",
            "session summary",
        }
        assert expected <= spans.keys()
        for name in expected:
            attrs = spans[name].attributes
            assert attrs[KESTREL_SESSION_ID] == "sess-1"
            assert attrs["session.id"] == attrs[KESTREL_SESSION_ID]

    @pytest.mark.asyncio
    async def test_missing_session_id_omits_both_session_attributes(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart", session_id=None))
        await hook.execute(_make_input("UserPromptSubmit", session_id=None))
        await hook.execute(
            _make_input(
                "PostToolUse",
                session_id=None,
                tool_name="Bash",
                execution_time_ms=1,
                tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop", session_id=None))
        await hook.execute(_make_input("AgentTerminate", session_id=None))
        assert exporter.get_finished_spans()
        for span in exporter.get_finished_spans():
            assert KESTREL_SESSION_ID not in span.attributes
            assert "session.id" not in span.attributes

    @pytest.mark.asyncio
    async def test_turn_ids_on_every_span_of_a_turn(self):
        hook, exporter = _memory_hook()
        await self._drive_turn(hook)
        spans = _by_name(exporter.get_finished_spans())
        # Every span EXCEPT the pre-turn session-marker root carries turn ids.
        for name in ("test-agent turn 1", "Bash (started)", "Bash", "turn 1 summary"):
            attrs = spans[name].attributes
            assert attrs[KESTREL_TURN_ID] == "turn-test-1"
            assert attrs[KESTREL_TURN_INDEX] == 1
            assert attrs["kestrel.agent_did"] == "did:agent:test"

    @pytest.mark.asyncio
    async def test_turn_root_binds_its_trace_to_the_host_stop_address(self):
        bound = []
        agent = _make_agent()
        agent.bind_current_turn_trace_identity = (
            lambda trace_id, span_id: bound.append((trace_id, span_id))
        )
        hook, exporter = _memory_hook(agent=agent)

        await hook.execute(_make_input("UserPromptSubmit"))

        root = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert root.attributes[KESTREL_TURN_ID] == "turn-test-1"
        assert bound == [
            (
                f"{root.context.trace_id:032x}",
                f"{root.context.span_id:016x}",
            )
        ]

    @pytest.mark.asyncio
    async def test_missing_host_turn_identity_omits_authority_address_only(self):
        hook, exporter = _memory_hook(
            agent=SimpleNamespace(
                agent_name="test-agent",
                agent_id="did:agent:test",
            )
        )

        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("Stop"))

        spans = _by_name(exporter.get_finished_spans())
        assert KESTREL_TURN_ID not in spans["test-agent turn 1"].attributes
        assert spans["test-agent turn 1"].attributes[KESTREL_TURN_INDEX] == 1
        assert "turn 1 summary" in spans

    @pytest.mark.asyncio
    async def test_stop_emits_turn_summary_not_session_summary(self):
        hook, exporter = _memory_hook()
        await self._drive_turn(hook)
        spans = _by_name(exporter.get_finished_spans())
        assert "turn 1 summary" in spans
        # The session is NOT popped on Stop → no session summary yet.
        assert "session summary" not in spans

    @pytest.mark.asyncio
    async def test_monotonic_turn_counter_across_turns(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("Stop"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("Stop"))
        spans = _by_name(exporter.get_finished_spans())
        assert spans["test-agent turn 2"].attributes[KESTREL_TURN_ID] == (
            "turn-test-2"
        )
        assert spans["test-agent turn 2"].attributes[KESTREL_TURN_INDEX] == 2
        # Two distinct per-turn traces.
        assert (
            spans["test-agent turn 1"].context.trace_id
            != spans["test-agent turn 2"].context.trace_id
        )

    @pytest.mark.asyncio
    async def test_agent_terminate_emits_session_summary_aggregating_turns(self):
        hook, exporter = _memory_hook()
        # Two turns, one tool each, then terminate.
        for _ in range(2):
            await hook.execute(_make_input("UserPromptSubmit"))
            await hook.execute(
                _make_input(
                    "PostToolUse", tool_name="Bash",
                    execution_time_ms=1, tool_response={"success": True},
                )
            )
            await hook.execute(_make_input("Stop"))
        await hook.execute(_make_input("AgentTerminate"))
        spans = _by_name(exporter.get_finished_spans())
        summary = spans["session summary"]
        session_root = spans["test-agent"]
        assert summary.parent.span_id == session_root.context.span_id
        assert summary.attributes["openinference.span.kind"] == "CHAIN"
        assert summary.attributes["kestrel.turn_count"] == 2
        assert summary.attributes["kestrel.tool_count"] == 2
        assert summary.attributes[KESTREL_SESSION_ID] == "sess-1"
        assert summary.attributes["session.id"] == "sess-1"
        # Session summary is session-scoped, not turn-scoped.
        assert KESTREL_TURN_ID not in summary.attributes

    @pytest.mark.asyncio
    async def test_stop_does_not_pop_session(self):
        # After Stop the session stays live: a following turn reuses the same
        # session-marker root (one root, stable session id across turns).
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("Stop"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("Stop"))
        roots = [s for s in exporter.get_finished_spans() if s.name == "test-agent"]
        assert len(roots) == 1

    @pytest.mark.asyncio
    async def test_tool_before_prompt_falls_back_to_session_root(self):
        # Events arriving before any prompt parent to the session-marker root.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        spans = _by_name(exporter.get_finished_spans())
        session_root, tool = spans["test-agent"], spans["Bash"]
        assert tool.parent.span_id == session_root.context.span_id
        assert KESTREL_TURN_ID not in tool.attributes

    @pytest.mark.asyncio
    async def test_scheduler_pre_tool_use_emits_a_start_marker(self):
        # The scheduler pseudo-session is no longer special-cased (#87): a tick
        # gets the same start marker as any other tool call, so the Timeline can
        # pair it with the tick's terminal (idle or completed) span.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input("PreToolUse", session_id="scheduler", tool_name="restart_coordinator")
        )
        assert (
            _by_name(exporter.get_finished_spans()).get("restart_coordinator (started)")
            is not None
        )


# ---------------------------------------------------------------------------
# 3g-bis. Terminal tool outcomes: turn-end reconciliation (#84)
# ---------------------------------------------------------------------------

def _terminal_tools(exporter):
    """The terminal (non-marker) TOOL spans — a tool's completed/incomplete twin."""
    return [
        s for s in exporter.get_finished_spans()
        if s.attributes["openinference.span.kind"] == "TOOL"
        and KESTREL_MARKER not in s.attributes
    ]


class TestToolOutcomes:
    """The SDK HookInput has no per-call id and no deny event, so the in-process
    emitter pairs pending starts by tool NAME and produces `completed` /
    `incomplete` only — never `denied`. Reconciliation is the whole backbone."""

    @pytest.mark.asyncio
    async def test_unresolved_tool_reconciles_as_incomplete_at_stop(self):
        # A guard/permission layer refusing a call fires no event the hook can
        # see: the tool is simply still pending when its turn ends (#84).
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("Stop"))
        spans = _by_name(exporter.get_finished_spans())
        incomplete, marker = spans["Bash"], spans["Bash (started)"]
        assert incomplete.attributes[KESTREL_TOOL_OUTCOME] == "incomplete"
        assert incomplete.attributes["tool.success"] is False
        # Zero-duration at the recorded start, NOT start→Stop: it never ran, so
        # it must never draw a bar claiming runtime it did not have.
        assert incomplete.start_time == incomplete.end_time == marker.start_time
        assert incomplete.parent.span_id == spans["test-agent turn 1"].context.span_id

    @pytest.mark.asyncio
    async def test_refusals_counted_apart_from_executed_tools(self):
        # A refusal is not an agent error: tool_count/error_count/success_ratio
        # stay over EXECUTED tools, with incomplete_count as its own dimension.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Read"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Read",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("Stop"))
        await hook.execute(_make_input("AgentTerminate"))
        spans = _by_name(exporter.get_finished_spans())
        for summary in (spans["turn 1 summary"], spans["session summary"]):
            assert summary.attributes["kestrel.tool_count"] == 1
            assert summary.attributes["kestrel.error_count"] == 0
            assert summary.attributes["kestrel.success_ratio"] == 1.0
            assert summary.attributes[KESTREL_INCOMPLETE_COUNT] == 1
            # No deny signal exists in the SDK contract — always zero here.
            assert summary.attributes[KESTREL_DENIED_COUNT] == 0

    @pytest.mark.asyncio
    async def test_completed_tool_leaves_nothing_to_reconcile(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop"))
        terminal = _terminal_tools(exporter)
        assert [s.attributes[KESTREL_TOOL_OUTCOME] for s in terminal] == ["completed"]
        assert _by_name(exporter.get_finished_spans())[
            "turn 1 summary"
        ].attributes[KESTREL_INCOMPLETE_COUNT] == 0

    @pytest.mark.asyncio
    async def test_interrupted_turn_reconciles_into_its_own_turn(self):
        # An interrupt fires no Stop, so the leftover is reconciled when the next
        # prompt closes the stale turn — into the ORIGINAL turn, never the new
        # one (the phantom-attribution class this fix exists to kill).
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("UserPromptSubmit"))  # interrupted
        spans = _by_name(exporter.get_finished_spans())
        incomplete = spans["Bash"]
        turn1, turn2 = spans["test-agent turn 1"], spans["test-agent turn 2"]
        assert incomplete.attributes[KESTREL_TOOL_OUTCOME] == "incomplete"
        assert incomplete.attributes[KESTREL_TURN_ID] == "turn-test-1"
        assert incomplete.parent.span_id == turn1.context.span_id
        assert incomplete.context.trace_id == turn1.context.trace_id
        assert incomplete.context.trace_id != turn2.context.trace_id
        # ...and the interrupted turn is still summarized exactly once.
        names = [s.name for s in exporter.get_finished_spans()]
        assert names.count("turn 1 summary") == 1

    @pytest.mark.asyncio
    async def test_session_close_mid_turn_reconciles_before_totals(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("AgentTerminate"))
        spans = _by_name(exporter.get_finished_spans())
        assert spans["Bash"].attributes[KESTREL_TOOL_OUTCOME] == "incomplete"
        assert spans["turn 1 summary"].attributes[KESTREL_INCOMPLETE_COUNT] == 1
        assert spans["session summary"].attributes[KESTREL_INCOMPLETE_COUNT] == 1

    @pytest.mark.asyncio
    async def test_concurrent_same_name_tools_pair_lifo(self):
        # Name-keyed pairing is all the SDK contract allows: the completion
        # claims the most recent start, the other reconciles as its own span.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop"))
        outcomes = sorted(s.attributes[KESTREL_TOOL_OUTCOME] for s in _terminal_tools(exporter))
        assert outcomes == ["completed", "incomplete"]

    @pytest.mark.asyncio
    async def test_late_completion_never_adds_a_second_terminal_span(self):
        # A PostToolUse landing AFTER reconciliation already terminalized the
        # call must not emit a second terminal span: one call, one terminal span.
        # An exported OTel span cannot be re-labeled incomplete → completed, so
        # the span already exported wins and the late duplicate is dropped.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("Stop"))          # reconciles → incomplete
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        terminal = _terminal_tools(exporter)
        assert [s.attributes[KESTREL_TOOL_OUTCOME] for s in terminal] == ["incomplete"]
        # ...and it is not double-counted into the session totals either.
        await hook.execute(_make_input("AgentTerminate"))
        session_summary = _by_name(exporter.get_finished_spans())["session summary"]
        assert session_summary.attributes["kestrel.tool_count"] == 0
        assert session_summary.attributes[KESTREL_INCOMPLETE_COUNT] == 1

    @pytest.mark.asyncio
    async def test_tombstone_absorbs_only_one_late_completion(self):
        # The tombstone is consumed on claim, so a genuinely NEW same-name call
        # with its own start still emits normally afterwards.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(_make_input("Stop"))          # reconciles → incomplete
        await hook.execute(_make_input("PostToolUse", tool_name="Bash",
                                       tool_response={"success": True}))  # dropped
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        outcomes = sorted(s.attributes[KESTREL_TOOL_OUTCOME] for s in _terminal_tools(exporter))
        assert outcomes == ["completed", "incomplete"]
        assert hook._sessions["sess-1"].terminalized == deque()

    @pytest.mark.asyncio
    async def test_completion_is_parented_by_its_start_not_the_live_turn(self):
        # A claimed start carries the turn the tool STARTED in, and the terminal
        # span must follow it — parenting/counting against whatever turn is live
        # when the completion lands is the phantom attribution this span shape
        # exists to kill. Driven directly: the dispatch paths all reconcile first,
        # so this guards the wiring itself against a future barrier change.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(_make_input("PreToolUse", tool_name="Bash"))
        session = hook._sessions["sess-1"]
        record = session.pending_tools["Bash"][-1]  # stamped with turn 1
        turn_one = record.turn
        await hook.execute(_make_input("UserPromptSubmit"))  # turn 2 is now live
        hook._emit_tool_span(
            session, "sess-1", "test-agent",
            _make_input(
                "PostToolUse", tool_name="Bash",
                execution_time_ms=5, tool_response={"success": True},
            ),
            record,
        )
        spans = _by_name(exporter.get_finished_spans())
        completed = [
            s for s in _terminal_tools(exporter)
            if s.attributes[KESTREL_TOOL_OUTCOME] == "completed"
        ][0]
        turn1, turn2 = spans["test-agent turn 1"], spans["test-agent turn 2"]
        assert completed.parent.span_id == turn1.context.span_id
        assert completed.context.trace_id == turn1.context.trace_id
        assert completed.context.trace_id != turn2.context.trace_id
        assert completed.attributes[KESTREL_TURN_ID] == "turn-test-1"
        # Counted against its own turn, never the live one.
        assert turn_one.tool_count == 1
        assert session.current_turn.tool_count == 0

    @pytest.mark.asyncio
    async def test_idle_scheduler_tick_terminalizes_and_leaves_nothing_pending(self):
        # An idle tick now emits its OWN terminal `idle` span (#87), which claims
        # the pending start — so the entry can't leak (the scheduler pseudo-session
        # never sees a Stop to reconcile it) and reconciliation can never later
        # mislabel a SUCCESSFUL heartbeat as refused.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input("PreToolUse", session_id="scheduler", tool_name="tick")
        )
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler", tool_name="tick",
                tool_response={"status": "ok", "data": {"executed": False, "pending": 0}},
            )
        )
        session = hook._sessions["scheduler"]
        assert session.pending_tools == {}
        terminals = _terminal_tools(exporter)
        assert [s.attributes[KESTREL_TOOL_OUTCOME] for s in terminals] == [
            TOOL_OUTCOME_IDLE
        ]

    @pytest.mark.asyncio
    async def test_never_completing_scheduler_ticks_cannot_grow_unbounded(self):
        # Every tick now records a pending start (#87 retired the #42
        # suppression), and the `scheduler` pseudo-session is IMMORTAL — it never
        # sees a Stop/AgentTerminate, so `_reconcile_pending` never runs for it.
        # A cron tool that fires PreToolUse and never completes (a guard, an
        # abort, an error before PostToolUse) would therefore grow the stack once
        # a minute for the life of the process. The per-tool-name cap bounds it.
        hook, exporter = _memory_hook()
        for _ in range(_MAX_PENDING_PER_TOOL * 4):
            await hook.execute(
                _make_input("PreToolUse", session_id="scheduler", tool_name="tick")
            )
        session = hook._sessions["scheduler"]
        assert len(session.pending_tools["tick"]) == _MAX_PENDING_PER_TOOL

        # Eviction is NOT a silent drop: each evicted leftover gets the same
        # terminal `incomplete` span reconciliation would have given it...
        evicted = _MAX_PENDING_PER_TOOL * 3
        terminals = _terminal_tools(exporter)
        assert [s.attributes[KESTREL_TOOL_OUTCOME] for s in terminals] == [
            TOOL_OUTCOME_INCOMPLETE
        ] * evicted
        assert session.incomplete_count == evicted
        # ...anchored zero-duration at its OWN recorded start, so a late-exported
        # leftover never claims runtime it did not have.
        assert all(s.start_time == s.end_time for s in terminals)
        # The oldest (LIFO-unreachable) entries are the ones evicted — the
        # survivors are the newest, which a completion can still pair with.
        assert [r.tool_name for r in session.pending_tools["tick"]] == [
            "tick"
        ] * _MAX_PENDING_PER_TOOL

    @pytest.mark.asyncio
    async def test_pending_cap_does_not_disturb_normal_tick_pairing(self):
        # The cap must bound ONLY the never-completed leftovers: the Timeline
        # pairs a tick's "(started)" marker with its terminal span (#87), so a
        # normal Pre→Post tick must still record and claim its pending start.
        hook, exporter = _memory_hook()
        for _ in range(_MAX_PENDING_PER_TOOL * 3):
            await hook.execute(
                _make_input("PreToolUse", session_id="scheduler", tool_name="tick")
            )
            await hook.execute(
                _make_input(
                    "PostToolUse", session_id="scheduler", tool_name="tick",
                    tool_response={
                        "status": "ok", "data": {"executed": False, "pending": 0}
                    },
                )
            )
        session = hook._sessions["scheduler"]
        # Each tick claimed its own start, so nothing ever reached the cap.
        assert session.pending_tools == {}
        assert session.incomplete_count == 0
        outcomes = {s.attributes[KESTREL_TOOL_OUTCOME] for s in _terminal_tools(exporter)}
        assert outcomes == {TOOL_OUTCOME_IDLE}


# ---------------------------------------------------------------------------
# 3h. Turn-root prompt capture (opt-in) + complete summary stats (#63)
# ---------------------------------------------------------------------------

class TestTurnPromptCapture:
    @pytest.mark.asyncio
    async def test_prompt_not_captured_by_default(self):
        # Default OFF: the turn root carries no prompt text (privacy invariant).
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input("UserPromptSubmit", user_message="hello world")
        )
        turn = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert "input.value" not in turn.attributes

    @pytest.mark.asyncio
    async def test_prompt_captured_when_opted_in(self):
        with patch.dict("os.environ", {"KESTREL_OTEL_CAPTURE_PROMPTS": "1"}):
            hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input("UserPromptSubmit", user_message="hello world")
        )
        turn = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert turn.attributes["input.value"] == "hello world"

    @pytest.mark.asyncio
    async def test_prompt_truncated_to_env_cap(self):
        with patch.dict(
            "os.environ",
            {"KESTREL_OTEL_CAPTURE_PROMPTS": "1", "KESTREL_OTEL_MAX_IO_CHARS": "10"},
        ):
            hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit", user_message="x" * 50))
        turn = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert turn.attributes["input.value"] == "x" * 10

    @pytest.mark.asyncio
    async def test_prompt_default_cap_is_20000(self):
        with patch.dict("os.environ", {"KESTREL_OTEL_CAPTURE_PROMPTS": "1"}):
            hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit", user_message="x" * 25_000))
        turn = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert len(turn.attributes["input.value"]) == 20_000

    @pytest.mark.asyncio
    async def test_prompt_capture_prefers_rewritten_prompt(self):
        # An earlier UserPromptSubmit hook can rewrite/redact the prompt via
        # HookOutput.modify(updated_input={"user_message": ...}); the host merges
        # that into HookInput.tool_input before this (last-priority) emitter runs.
        # Capture must export the rewritten prompt the model actually saw — not the
        # stale original in ``user_message``.
        with patch.dict("os.environ", {"KESTREL_OTEL_CAPTURE_PROMPTS": "1"}):
            hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "UserPromptSubmit",
                user_message="my password is hunter2",
                tool_input={"user_message": "my password is [REDACTED]"},
            )
        )
        turn = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert turn.attributes["input.value"] == "my password is [REDACTED]"

    @pytest.mark.asyncio
    async def test_prompt_capture_falls_back_to_user_message(self):
        # No upstream rewrite (tool_input carries no user_message) → the original
        # prompt is captured unchanged.
        with patch.dict("os.environ", {"KESTREL_OTEL_CAPTURE_PROMPTS": "1"}):
            hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(
            _make_input(
                "UserPromptSubmit",
                user_message="hello world",
                tool_input={"unrelated": "value"},
            )
        )
        turn = _by_name(exporter.get_finished_spans())["test-agent turn 1"]
        assert turn.attributes["input.value"] == "hello world"


class TestSummaryStats:
    @pytest.mark.asyncio
    async def test_turn_summary_carries_error_count_and_duration(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="a",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="b",
                execution_time_ms=1, tool_response={"success": False},
            )
        )
        await hook.execute(_make_input("Stop"))
        summary = _by_name(exporter.get_finished_spans())["turn 1 summary"]
        assert summary.attributes["kestrel.tool_count"] == 2
        assert summary.attributes["kestrel.error_count"] == 1
        assert summary.attributes["kestrel.success_ratio"] == 0.5
        # Unified go-forward key mirrors the legacy per-scope key.
        assert (
            summary.attributes["kestrel.duration_ms"]
            == summary.attributes["kestrel.turn_duration_ms"]
        )

    @pytest.mark.asyncio
    async def test_session_summary_carries_error_count_and_duration(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("UserPromptSubmit"))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="a",
                execution_time_ms=1, tool_response={"success": False},
            )
        )
        await hook.execute(_make_input("Stop"))
        await hook.execute(_make_input("AgentTerminate"))
        summary = _by_name(exporter.get_finished_spans())["session summary"]
        assert summary.attributes["kestrel.tool_count"] == 1
        assert summary.attributes["kestrel.error_count"] == 1
        assert (
            summary.attributes["kestrel.duration_ms"]
            == summary.attributes["kestrel.session_duration_ms"]
        )


# ---------------------------------------------------------------------------
# 4. No-op when unconfigured
# ---------------------------------------------------------------------------

class TestUnconfigured:
    @pytest.mark.asyncio
    async def test_tracer_disabled_when_endpoint_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            hook = ObservabilityHook(agent=_make_agent())
        assert hook._tracer.enabled is False
        result = await hook.execute(
            _make_input(
                "PostToolUse", tool_name="t",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        assert result.continue_execution is True

    @pytest.mark.asyncio
    async def test_no_exporter_constructed_when_unset(self):
        with patch(
            "kestrel_feature_observability.tracing.OTLPSpanExporter"
        ) as exporter:
            with patch.dict("os.environ", {}, clear=True):
                hook = ObservabilityHook(agent=_make_agent())
                await hook.execute(_make_input("PreToolUse", tool_name="t"))
        exporter.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Failures are swallowed
# ---------------------------------------------------------------------------

class TestHookExceptionHandling:
    @pytest.mark.asyncio
    async def test_tracer_raising_is_swallowed(self):
        hook, _ = _memory_hook()

        class _BoomTracer:
            def run_span(self, *a, **k):
                raise RuntimeError("tracer down")

        hook._tracer = _BoomTracer()
        result = await hook.execute(_make_input("PreToolUse", tool_name="t"))
        assert result.continue_execution is True

    @pytest.mark.asyncio
    async def test_agent_name_missing(self):
        agent = _make_agent()
        del agent.agent_name
        hook, exporter = _memory_hook(agent=agent)
        result = await hook.execute(_make_input("PreToolUse"))
        assert result.continue_execution is True


# ---------------------------------------------------------------------------
# 6. Feature registers hook during initialize() / closes on shutdown
# ---------------------------------------------------------------------------

class TestFeatureInitialization:
    @pytest.mark.asyncio
    async def test_feature_provides_hook_via_get_hooks(self):
        feature = ObservabilityFeature(_make_agent())
        await feature.initialize()

        hooks = feature.get_hooks()
        assert len(hooks) == 1
        assert isinstance(hooks[0], ObservabilityHook)
        assert hooks[0].name == "observability"
        assert hooks[0].priority == 999

    @pytest.mark.asyncio
    async def test_feature_clears_hook_on_shutdown(self):
        feature = ObservabilityFeature(_make_agent())
        await feature.initialize()
        await feature.shutdown()
        assert feature.get_hooks() == []

    @pytest.mark.asyncio
    async def test_shutdown_emits_summary_for_open_sessions(self):
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = KestrelTracer(tracer=provider.get_tracer("test"))

        feature = ObservabilityFeature(_make_agent())
        with patch(
            "kestrel_feature_observability.hook.configure_tracing",
            return_value=tracer,
        ):
            await feature.initialize()
        hook = feature.get_hooks()[0]
        await hook.execute(_make_input("SessionStart"))
        # The session root (marker) is exported IMMEDIATELY — never held open.
        assert _by_name(exporter.get_finished_spans()).get("test-agent") is not None
        # No summary yet (session still live).
        assert _by_name(exporter.get_finished_spans()).get("session summary") is None
        await feature.shutdown()
        # Defensive close on shutdown flushes the session summary span.
        assert (
            _by_name(exporter.get_finished_spans()).get("session summary") is not None
        )

    def test_feature_tool_description(self):
        feature = ObservabilityFeature(_make_agent())
        assert "observability" in feature.tool_description.lower()

    def test_feature_has_no_query_tools(self):
        """Producer-only: no obs_status/obs_events @tool surface remains."""
        feature = ObservabilityFeature(_make_agent())
        tool_names = [t.name for t in feature.get_tools()]
        assert "obs_status" not in tool_names
        assert "obs_events" not in tool_names

    def test_feature_has_no_router_or_ui(self):
        """Producer-only: router + UI panels belong to the fleet host."""
        feature = ObservabilityFeature(_make_agent())
        assert feature.get_router() is None
        assert feature.get_ui_contributions() is None


# ---------------------------------------------------------------------------
# 7. Privacy: user_message content NOT stamped on any span
# ---------------------------------------------------------------------------

class TestPrivacy:
    @pytest.mark.asyncio
    async def test_user_message_content_not_in_spans(self):
        hook, exporter = _memory_hook()
        secret = "my password is hunter2"
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("UserPromptSubmit", user_message=secret))
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="t",
                execution_time_ms=1, tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop"))
        for span in exporter.get_finished_spans():
            for value in span.attributes.values():
                if isinstance(value, str):
                    assert secret not in value

    @pytest.mark.asyncio
    async def test_tool_input_not_stamped(self):
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse",
                tool_name="web_search",
                tool_input={"query": "sensitive query", "api_key": "secret123"},
                execution_time_ms=1,
                tool_response={"success": True},
            )
        )
        await hook.execute(_make_input("Stop"))
        for span in exporter.get_finished_spans():
            for value in span.attributes.values():
                if isinstance(value, str):
                    assert "sensitive query" not in value
                    assert "secret123" not in value


# ---------------------------------------------------------------------------
# 8. Error truncation
# ---------------------------------------------------------------------------

class TestErrorTruncation:
    @pytest.mark.asyncio
    async def test_long_error_truncated_to_200_chars(self):
        hook, exporter = _memory_hook()
        long_error = "x" * 500
        await hook.execute(
            _make_input(
                "PostToolUse",
                tool_name="t",
                execution_time_ms=1,
                tool_response={"success": False, "error": long_error},
            )
        )
        tool = _by_name(exporter.get_finished_spans())["t"]
        assert tool.attributes["tool.success"] is False
        assert len(tool.attributes["tool.error"]) == 200


# ---------------------------------------------------------------------------
# 9. orchestrator semantics (self-driven vs driven)
# ---------------------------------------------------------------------------

class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_self_driven_sets_orchestrator_to_agent(self):
        hook, exporter = _memory_hook(agent=_make_agent(agent_id="did:agent:me"))
        await hook.execute(_make_input("SessionStart"))
        await hook.execute(_make_input("Stop"))
        run = _by_name(exporter.get_finished_spans())["test-agent"]
        assert run.attributes[KESTREL_ORCHESTRATOR] == "test-agent"
        assert run.attributes["kestrel.agent_did"] == "did:agent:me"

    @pytest.mark.asyncio
    async def test_driven_agent_does_not_self_orchestrate(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart", parent_did="did:agent:driver"))
        await hook.execute(_make_input("Stop"))
        run = _by_name(exporter.get_finished_spans())["test-agent"]
        # Driven → orchestrator not set to this agent's own name (no env default here).
        assert run.attributes.get(KESTREL_ORCHESTRATOR) != "test-agent"


# ---------------------------------------------------------------------------
# 10. Prometheus metrics still emitted
# ---------------------------------------------------------------------------

class TestPrometheusUnchanged:
    @pytest.mark.asyncio
    async def test_hook_event_counter_increments(self):
        from kestrel_feature_observability.hook import PROMETHEUS_AVAILABLE, HOOK_EVENTS

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus-client not installed")

        before = HOOK_EVENTS.labels(event_type="PreToolUse")._value.get()
        hook, _ = _memory_hook()
        await hook.execute(_make_input("PreToolUse", tool_name="t"))
        after = HOOK_EVENTS.labels(event_type="PreToolUse")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_tool_call_counter_increments(self):
        from kestrel_feature_observability.hook import PROMETHEUS_AVAILABLE, TOOL_CALLS

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus-client not installed")

        before = TOOL_CALLS.labels(tool_name="t", success="True")._value.get()
        hook, _ = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="t",
                execution_time_ms=5, tool_response={"success": True},
            )
        )
        after = TOOL_CALLS.labels(tool_name="t", success="True")._value.get()
        assert after == before + 1
