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
from kestrel_feature_observability.hook import ObservabilityHook, KESTREL_SESSION_ID
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
    return SimpleNamespace(agent_name=agent_name, agent_id=agent_id)


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
# 3e. Scheduler no-op tick noise filter (#42 defect 1)
# ---------------------------------------------------------------------------

class TestSchedulerNoiseFilter:
    # These exercise the REAL production contract: the every-minute
    # ``restart_coordinator`` cron ACTION goes through the scheduler's tool-lookup
    # path (it is a feature @tool, not a builtin_handler), so it fires the
    # PostToolUse hook with ``session_id="scheduler"`` and a serialized
    # ``ToolResult`` envelope — outcome ``status`` at the top level, work counters
    # nested under ``data`` (verified against kestrel-sovereign
    # restart_coordinator/feature.py + the tool wrapper's ToolResult.to_dict()).

    # The exact idle envelope restart_coordinator emits every idle minute — the
    # 81%-noise no-op the issue targets.
    IDLE_RESPONSE = {
        "status": "ok",
        "confirmation": "No pending restart requests",
        "data": {"executed": False, "pending": 0},
        "tool": "restart_coordinator",
        "success": True,
    }

    @pytest.mark.asyncio
    async def test_idle_restart_coordinator_tick_emits_no_spans(self):
        # The real every-minute no-op: counters (executed) live under `data`, not
        # at the top level. A top-level-only scan would miss them and emit — this
        # asserts the nested-envelope filter actually drops the noise.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        assert exporter.get_finished_spans() == ()

    @pytest.mark.asyncio
    async def test_noop_tick_with_nested_idle_status_emits_no_spans(self):
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
        assert exporter.get_finished_spans() == ()

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
        assert (
            _by_name(exporter.get_finished_spans()).get("restart_coordinator")
            is not None
        )

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
    async def test_env_opt_in_traces_noop_ticks(self):
        with patch.dict("os.environ", {"KESTREL_OTEL_TRACE_SCHEDULER": "1"}):
            hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", session_id="scheduler",
                tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        assert (
            _by_name(exporter.get_finished_spans()).get("restart_coordinator")
            is not None
        )

    @pytest.mark.asyncio
    async def test_filter_is_scheduler_only(self):
        # A normal agent tool call always emits, even with an idle no-op response.
        hook, exporter = _memory_hook()
        await hook.execute(
            _make_input(
                "PostToolUse", tool_name="restart_coordinator",
                tool_response=self.IDLE_RESPONSE,
            )
        )
        assert (
            _by_name(exporter.get_finished_spans()).get("restart_coordinator")
            is not None
        )


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
        # error. The summary success_ratio must be 0.5, not 1.0.
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
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
        summary = _by_name(exporter.get_finished_spans())["session summary"]
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
    async def test_summary_parented_to_root_carries_totals(self):
        hook, exporter = _memory_hook()
        await hook.execute(_make_input("SessionStart"))
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
        root, summary = spans["test-agent"], spans["session summary"]
        assert summary.parent.span_id == root.context.span_id
        assert summary.context.trace_id == root.context.trace_id
        assert summary.attributes["kestrel.tool_count"] == 2
        assert summary.attributes["kestrel.success_ratio"] == 0.5
        assert summary.attributes["openinference.span.kind"] == "CHAIN"
        assert summary.end_time >= summary.start_time


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
