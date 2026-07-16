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
    async def test_shutdown_closes_open_run_spans(self):
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
        # Run span still open → not yet exported.
        assert exporter.get_finished_spans() == ()
        await feature.shutdown()
        # Defensive close on shutdown flushes the held run span.
        assert _by_name(exporter.get_finished_spans()).get("test-agent") is not None

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
