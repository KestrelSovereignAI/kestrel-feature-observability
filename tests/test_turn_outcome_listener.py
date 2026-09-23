"""Core turn outcomes end the feature-owned turn root on EVERY exit (#118).

kestrel-sovereign#3159 computes one ``kestrel.turn.outcome`` per turn and hands
it to feature-owned turn roots through ``agent.add_turn_outcome_listener`` — the
only way to end a turn whose exit skips the SDK ``Stop`` hook (the strict-audit
cancel paths). These tests drive the REAL feature lifecycle
(``ObservabilityFeature.initialize`` / ``shutdown``) against a host double that
implements core's listener contract exactly: a list registry, synchronous
delivery of a ``str`` enum, and a listener error that never reaches the turn.
"""

from __future__ import annotations

import logging
from enum import Enum
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kestrel_sdk.hooks.base import HookInput
from kestrel_feature_observability.feature import ObservabilityFeature
from kestrel_feature_observability.hook import (
    KESTREL_TOOL_OUTCOME,
    KESTREL_TURN_OUTCOME,
    ObservabilityHook,
)
from kestrel_feature_observability.tracing import KestrelTracer

logger = logging.getLogger(__name__)


class TurnOutcome(str, Enum):
    """Mirror of core's ``kestrel_sovereign.telemetry.TurnOutcome``."""

    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"
    INTERRUPTED = "interrupted"


class HostAgent:
    """Implements core's turn-outcome seam (``TurnLifecycleMixin``) verbatim."""

    def __init__(self, *, with_seam: bool = True):
        self.agent_name = "test-agent"
        self.agent_id = "did:agent:test"
        self._turn = 0
        self._current_turn_id = None
        if with_seam:
            self._turn_outcome_listeners = []

    # The canonical Stop address the lifecycle mints for each turn.
    def begin_turn(self) -> str:
        self._turn += 1
        self._current_turn_id = f"turn-{self._turn}"
        return self._current_turn_id

    def get_current_turn_id(self):
        return self._current_turn_id

    def get_current_causation_chain(self):
        return []

    def __getattr__(self, name):
        # Only a host with the seam exposes it (duck-typed, like core).
        if name in {"add_turn_outcome_listener", "remove_turn_outcome_listener"} and (
            "_turn_outcome_listeners" in self.__dict__
        ):
            return getattr(self, f"_{name}")
        raise AttributeError(name)

    def _add_turn_outcome_listener(self, listener) -> None:
        if not callable(listener):
            raise TypeError("turn outcome listener must be callable")
        if listener not in self._turn_outcome_listeners:
            self._turn_outcome_listeners.append(listener)

    def _remove_turn_outcome_listener(self, listener) -> None:
        if listener in self._turn_outcome_listeners:
            self._turn_outcome_listeners.remove(listener)

    def publish(self, turn_id, outcome) -> None:
        """Core's ``publish_turn_outcome``: synchronous, never raises into the turn."""
        for listener in tuple(self._turn_outcome_listeners):
            try:
                listener(turn_id, outcome)
            except Exception:  # noqa: BLE001 - mirrors core's contract
                logger.warning("listener raised", exc_info=True)


def _input(event, **kw):
    return HookInput(session_id="sess-1", hook_event_name=event, **kw)


async def _feature(agent):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = KestrelTracer(tracer=provider.get_tracer("test"))
    feature = ObservabilityFeature(agent)
    with patch(
        "kestrel_feature_observability.hook.configure_tracing", return_value=tracer
    ):
        await feature.initialize()
    return feature, feature.get_hooks()[0], exporter


def _named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def _start_turn(agent, hook, *, tool=None):
    turn_id = agent.begin_turn()
    await hook.execute(_input("UserPromptSubmit"))
    if tool:
        await hook.execute(_input("PreToolUse", tool_name=tool))
    return turn_id


class TestRegistrationThroughTheFeatureLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_registers_and_shutdown_unregisters(self):
        agent = HostAgent()
        feature, hook, _ = await _feature(agent)
        assert agent._turn_outcome_listeners == [hook.on_turn_outcome]
        await feature.shutdown()
        assert agent._turn_outcome_listeners == []

    @pytest.mark.asyncio
    async def test_host_teardown_order_unregisters_the_hook(self):
        """Follows core's ``_unregister_feature_runtime`` / ``_activate_feature_runtime``:
        ``shutdown()`` runs BEFORE ``get_hooks()`` is asked what to unregister,
        and a re-enable re-runs ``initialize()`` on the same instance."""
        agent = HostAgent()
        feature, hook, exporter = await _feature(agent)
        registered = list(feature.get_hooks())

        await feature.shutdown()
        for h in feature.get_hooks():
            registered.remove(h)
        assert registered == []
        assert agent._turn_outcome_listeners == []

        with patch(
            "kestrel_feature_observability.hook.configure_tracing",
            return_value=hook._tracer,
        ):
            await feature.initialize()
        registered.extend(feature.get_hooks())
        (live,) = registered
        assert live is not hook
        assert agent._turn_outcome_listeners == [live.on_turn_outcome]

        exporter.clear()
        turn_id = agent.begin_turn()
        for h in registered:
            await h.execute(_input("UserPromptSubmit"))
            await h.execute(_input("Stop"))
        agent.publish(turn_id, TurnOutcome.COMPLETED)
        (summary,) = _named(exporter, "turn 1 summary")
        assert summary.attributes[KESTREL_TURN_OUTCOME] == "completed"
        await feature.shutdown()

    @pytest.mark.asyncio
    async def test_host_without_the_seam_keeps_stop_closing_turns(self):
        agent = HostAgent(with_seam=False)
        feature, hook, exporter = await _feature(agent)
        await _start_turn(agent, hook)
        await hook.execute(_input("Stop"))
        (summary,) = _named(exporter, "turn 1 summary")
        assert KESTREL_TURN_OUTCOME not in summary.attributes
        await feature.shutdown()


class TestCoreOutcomeEndsTheTurn:
    @pytest.mark.asyncio
    async def test_completed_turn_is_closed_by_core_not_by_stop(self):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        turn_id = await _start_turn(agent, hook)
        await hook.execute(_input("Stop"))
        # "Finished responding" is not how the turn ended — core says that.
        assert _named(exporter, "turn 1 summary") == []
        agent.publish(turn_id, TurnOutcome.COMPLETED)
        (summary,) = _named(exporter, "turn 1 summary")
        (root,) = _named(exporter, "test-agent turn 1")
        assert summary.attributes[KESTREL_TURN_OUTCOME] == "completed"
        assert summary.parent.span_id == root.context.span_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", list(TurnOutcome))
    async def test_strict_audit_cancel_path_without_stop_still_ends_the_turn(self, outcome):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        turn_id = await _start_turn(agent, hook, tool="Bash")
        # No SDK Stop hook fires on this path; only core's outcome arrives.
        agent.publish(turn_id, outcome)
        (summary,) = _named(exporter, "turn 1 summary")
        assert summary.attributes[KESTREL_TURN_OUTCOME] == outcome.value
        # The pending tool is reconciled into its own turn before the summary.
        (incomplete,) = [
            s for s in _named(exporter, "Bash")
            if s.attributes.get(KESTREL_TOOL_OUTCOME) == "incomplete"
        ]
        assert incomplete.parent.span_id == _named(exporter, "test-agent turn 1")[0].context.span_id
        assert summary.attributes["kestrel.incomplete_count"] == 1

    @pytest.mark.asyncio
    async def test_outcome_is_delivered_exactly_once_even_with_a_late_stop(self):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        turn_id = await _start_turn(agent, hook)
        agent.publish(turn_id, TurnOutcome.STOPPED)
        await hook.execute(_input("Stop"))  # late, out of order
        agent.publish(turn_id, TurnOutcome.STOPPED)  # a duplicate delivery
        assert len(_named(exporter, "turn 1 summary")) == 1

    @pytest.mark.asyncio
    async def test_turn_without_a_canonical_address_is_closed_by_stop(self):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        await hook.execute(_input("UserPromptSubmit"))  # no turn address minted
        await hook.execute(_input("Stop"))
        (summary,) = _named(exporter, "turn 1 summary")
        assert KESTREL_TURN_OUTCOME not in summary.attributes

    @pytest.mark.asyncio
    async def test_outcome_for_an_unknown_turn_is_a_noop(self):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        await _start_turn(agent, hook)
        agent.publish("turn-elsewhere", TurnOutcome.FAILED)
        assert _named(exporter, "turn 1 summary") == []

    @pytest.mark.asyncio
    async def test_a_listener_failure_never_raises_into_the_turn(self):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        turn_id = await _start_turn(agent, hook)
        with patch.object(
            ObservabilityHook, "_emit_turn_summary", side_effect=RuntimeError("boom")
        ):
            hook.on_turn_outcome(turn_id, TurnOutcome.FAILED)  # must not raise
        hook.on_turn_outcome(object(), object())  # malformed delivery: ignored


class TestNoReceiptContentOnSpans:
    """R1: the outcome is the ONLY lifecycle fact a span carries."""

    @pytest.mark.asyncio
    async def test_summary_carries_exactly_the_summary_keys_plus_the_outcome(self):
        agent = HostAgent()
        _, hook, exporter = await _feature(agent)
        turn_id = await _start_turn(agent, hook)
        agent.publish(turn_id, TurnOutcome.STOPPED)
        (summary,) = _named(exporter, "turn 1 summary")
        lifecycle_keys = {
            key for key in summary.attributes
            if key.startswith("kestrel.turn.") or key.startswith("stop.") or key.startswith("hold.")
        }
        assert lifecycle_keys == {KESTREL_TURN_OUTCOME}
        # Structural: no span of the turn carries a receipt-shaped field at all.
        receipt_fields = {"reason", "actor_id", "actor", "receipt_id", "hold_receipt_id"}
        for span in exporter.get_finished_spans():
            for key in span.attributes:
                assert key.rsplit(".", 1)[-1] not in receipt_fields, (span.name, key)
