"""
Kestrel Observability Hook — per-agent emitter (producer) of OTel spans.

Emits an OpenTelemetry trace per agent session via :class:`KestrelTracer`
(``kestrel_feature_observability.tracing``): a session root (OpenInference
``AGENT``) with each tool invocation recorded as a child ``tool_span``, and a
closing ``session summary`` span carrying totals. Spans are exported to whatever
``OTEL_EXPORTER_OTLP_ENDPOINT`` points at (e.g. a host-supervised Phoenix). This
hook is purely observational: it never blocks, denies, or modifies anything.

Span shape — session ⊃ turn ⊃ tool ⊃ tool-start markers (robust, real durations,
no held-open spans; every span exports immediately — #42, #55):

- On the first lifecycle event of a session a short **session-marker** root span
  (OpenInference ``AGENT``) is opened AND ended immediately, so it is **exported
  right away**. Its ``SpanContext`` is kept as a fallback parent;
  ``kestrel.session_id`` and OpenInference ``session.id`` are stamped on it —
  and on EVERY span the hook emits — so Navigator and Phoenix Sessions can group
  a session by attribute: the session band is an attribute grouping, not a trace.
- Trace granularity is **one trace per turn**. On ``UserPromptSubmit`` a turn
  starts: a monotonic turn counter is bumped and an immediately-ended ``AGENT``
  turn-root span (``<agent> turn <n>``, ``kestrel.marker=start``) is emitted as a
  **new trace root**; its ``SpanContext`` is kept in session state. Every span of
  the turn also carries ``kestrel.turn_id`` (``<session_id>#<n>``) and
  ``kestrel.turn_index`` (n).
- On ``PreToolUse`` an instant tool-start marker (``<tool> (started)``,
  ``kestrel.marker=start``) is emitted, parented to the current turn — the
  innermost doll, paired with the ``PostToolUse`` tool span by the Timeline — and
  the start is recorded as a *pending* tool so turn-end reconciliation can
  represent a tool that never completed (#84).
- Each ``PostToolUse`` emits a child ``tool_span`` (OpenInference ``TOOL``)
  parented to the turn the tool STARTED in — the one stamped on the pending start
  it pops, so a completion never lands in whatever turn happens to be live when it
  arrives (fallback: the current turn, then the session root, e.g. events arriving
  before any prompt) — carrying the tool name, real duration, success, and
  ``kestrel.tool_outcome=completed``. When no duration is available the span is a
  zero-duration point span (start == end) — never ``start > end`` (a negative
  duration).
- Every tool gets exactly ONE terminal span. A tool the security/permission layer
  refuses never fires ``PostToolUse``, so its pending start is still open when the
  turn ends: ``Stop`` **reconciles** it into a terminal ``TOOL`` span carrying
  ``kestrel.tool_outcome=incomplete`` and ``tool.success=False``, emitted as a
  **zero-duration point span at the recorded start** — it never ran, so it must
  never draw a bar claiming runtime. That pairs with its ``(started)`` marker, so
  a refused tool renders as a visible terminal stub instead of an orphaned
  open-ended band (#82) or nothing at all (#84). Reconciling leaves a bounded
  **tombstone** for the call, so a ``PostToolUse`` that still arrives afterwards
  claims it and emits NOTHING: an exported OTel span cannot be re-labeled
  ``incomplete`` → ``completed``, so the span already exported stands rather than
  the call getting a second, double-counted, mis-parented terminal span.
- On ``Stop`` — AFTER reconciliation, so the counts are complete — a
  ``turn <n> summary`` (OpenInference ``CHAIN``, parented to the turn root)
  carries the per-turn totals: ``kestrel.tool_count`` / ``kestrel.error_count`` /
  ``kestrel.success_ratio`` over **executed** tools only, plus the distinct
  ``kestrel.denied_count`` / ``kestrel.incomplete_count`` dimensions (a refused
  tool is not an agent error, so it never moves the success ratio). The session is
  NOT popped — it stays stable across turns.
- A turn interrupted before its ``Stop`` is reconciled at the next
  ``UserPromptSubmit`` (or session close): its still-pending tools become
  ``incomplete`` spans in **their own** (stamped) turn context and that turn gets
  its retroactive ``turn <n> summary``, so every turn is summarized exactly once
  and nothing is ever attributed to the wrong turn.
- The session closes on ``AgentTerminate`` — and defensively on teardown
  (:meth:`close`) — emitting the true ``session summary`` (parented to the session
  root) aggregating turns (turn count + totals). No held-open spans.

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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

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
    KIND_TOOL,
    configure as configure_tracing,
)

logger = logging.getLogger(__name__)

# Phoenix Sessions recognizes the OpenInference ``session.id`` span attribute.
# Older compatible semantic-conventions releases may not expose ``SESSION_ID``,
# so retain the canonical literal as a dependency-safe fallback.
try:
    from openinference.semconv.trace import SpanAttributes

    OPENINFERENCE_SESSION_ID = getattr(SpanAttributes, "SESSION_ID", "session.id")
except Exception:  # noqa: BLE001 - semantic-conventions fallback must not break the hook
    OPENINFERENCE_SESSION_ID = "session.id"

# Standard session / turn grouping attributes (no constants in tracing.py).
# Both ``kestrel.session_id`` (Navigator/backward compatibility) and OpenInference
# ``session.id`` (Phoenix Sessions) are stamped on EVERY span when a session id
# exists; ``kestrel.turn_id`` / ``kestrel.turn_index`` label the one-trace-per-turn
# spans; ``kestrel.marker`` flags the instant start spans (turn root + tool-start),
# which the Timeline pairs with their close event.
KESTREL_SESSION_ID = "kestrel.session_id"
KESTREL_TURN_ID = "kestrel.turn_id"
KESTREL_TURN_INDEX = "kestrel.turn_index"
KESTREL_MARKER = "kestrel.marker"
KESTREL_TOOL_NAME = "tool.name"

# ``kestrel.marker`` value stamped on the instant turn-root and tool-start spans.
_MARKER_START = "start"

# How a tool call ended — stamped on EVERY terminal tool span so the three
# outcomes are a first-class, queryable dimension rather than an inference from
# absence (#84). ``completed`` = it ran (the ``PostToolUse`` span); ``denied`` =
# an explicit refusal signal; ``incomplete`` = it was still pending when its turn
# ended (a guard/permission-rule denial or an abort — no event ever fires for
# those, which is why turn-end reconciliation, not the deny signal, is the
# backbone). ``denied``/``incomplete`` spans are zero-duration point spans: the
# tool never ran, so it must never draw a bar claiming runtime.
KESTREL_TOOL_OUTCOME = "kestrel.tool_outcome"
TOOL_OUTCOME_COMPLETED = "completed"
TOOL_OUTCOME_DENIED = "denied"
TOOL_OUTCOME_INCOMPLETE = "incomplete"

# Distinct summary dimensions for the refused/aborted tools. Deliberately NOT
# folded into ``kestrel.tool_count`` / ``kestrel.error_count`` /
# ``kestrel.success_ratio``, which stay over EXECUTED tools only ("of the tools
# that ran, how many succeeded"): a user/guard blocking a tool is not the agent
# erroring, and conflating them would make every denial read as a fault in
# existing fleet dashboards. Additive keys → existing dashboards keep their exact
# semantics while denials become their own visible dimension (#84).
KESTREL_DENIED_COUNT = "kestrel.denied_count"
KESTREL_INCOMPLETE_COUNT = "kestrel.incomplete_count"

# Bound on the per-session tombstones for calls reconciliation already
# terminalized (#84). Each reconciled tool leaves one entry so a LATE
# ``PostToolUse`` for it is recognized and dropped instead of emitting a second
# terminal span; a late completion follows its reconciliation closely, so the
# ``deque`` evicts the OLDEST entry once the cap is reached.
_MAX_TERMINALIZED = 256

# OpenInference INPUT_VALUE attribute key — the user prompt stamped on the turn
# root when opt-in prompt capture is enabled (see ``_CAPTURE_PROMPTS_ENV``).
_INPUT_VALUE_KEY = "input.value"

# Turn-root prompt capture (opt-in; default OFF). When
# ``KESTREL_OTEL_CAPTURE_PROMPTS`` is truthy the turn's user prompt is stamped on
# the turn-root span as OpenInference ``input.value``, truncated to
# ``KESTREL_OTEL_MAX_IO_CHARS`` chars. Default OFF preserves the package's
# default-safe posture: user-message content is not recorded unless an operator
# explicitly opts in at their own wiring point.
_CAPTURE_PROMPTS_ENV = "KESTREL_OTEL_CAPTURE_PROMPTS"
_MAX_IO_CHARS_ENV = "KESTREL_OTEL_MAX_IO_CHARS"
# Default prompt truncation cap when ``KESTREL_OTEL_MAX_IO_CHARS`` is unset.
# Source of truth: kestrel-talon's ``DEFAULT_MAX_IO_CHARS = 20000`` in
# ``kestreltalon/observability.py`` (the #71 prompt/response capture convention
# this env var comes from) — kept in sync by reference.
_DEFAULT_MAX_IO_CHARS = 20000

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


def _max_io_chars() -> int:
    """The prompt truncation cap — ``KESTREL_OTEL_MAX_IO_CHARS`` else the default.

    Mirrors kestrel-talon's ``DEFAULT_MAX_IO_CHARS`` (20000); a non-numeric or
    negative value falls back to :data:`_DEFAULT_MAX_IO_CHARS`.
    """
    raw = os.environ.get(_MAX_IO_CHARS_ENV)
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return _DEFAULT_MAX_IO_CHARS
        if val >= 0:
            return val
    return _DEFAULT_MAX_IO_CHARS


def _effective_prompt(input: HookInput) -> Optional[str]:
    """The user prompt as it will actually reach the LLM — honoring earlier rewrites.

    A ``UserPromptSubmit`` hook can rewrite/redact the prompt via
    ``HookOutput.modify(updated_input={"user_message": ...})``; the host hook
    manager merges that ``updated_input`` into ``HookInput.tool_input`` before the
    next hook runs. This emitter runs last (priority 999), so a rewritten prompt
    lives in ``tool_input["user_message"]`` while ``input.user_message`` still holds
    the ORIGINAL text. Prefer the rewritten value so opt-in capture never exports a
    prompt the model never saw (e.g. a redacted secret); fall back to
    ``user_message`` when no upstream hook rewrote it.
    """
    tool_input = input.tool_input
    if isinstance(tool_input, dict):
        rewritten = tool_input.get("user_message")
        if rewritten is not None:
            return rewritten
    return input.user_message


def _session_attrs(session_id: Optional[str]) -> Dict[str, Any]:
    """The paired legacy + OpenInference session attributes, or none without an id."""
    if not session_id:
        return {}
    return {
        KESTREL_SESSION_ID: session_id,
        OPENINFERENCE_SESSION_ID: session_id,
    }


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
class _TurnState:
    """Per-turn bookkeeping — the exported turn root + per-turn totals.

    Holds only the ended turn-root span (for its ``SpanContext``, the new
    per-turn trace root) and counters — NEVER a held-open span (#42/#55).
    ``tool_count``/``success_count`` cover EXECUTED tools only; refused/aborted
    tools land in the distinct ``denied_count``/``incomplete_count`` (#84).
    """

    root: Any            # the ended turn-root span (holds the turn trace root SpanContext)
    index: int           # monotonic turn number (1-based) within the session
    turn_id: str         # ``<session_id>#<index>``
    started_ns: int      # turn start (turn-root marker) — for the summary duration
    tool_count: int = 0
    success_count: int = 0
    denied_count: int = 0
    incomplete_count: int = 0
    # Guards the "exactly one summary per turn" invariant: a turn interrupted
    # before its ``Stop`` is summarized retroactively at the next prompt/session
    # close, and must not be summarized twice if a late ``Stop`` still arrives.
    summarized: bool = False


@dataclass
class _PendingTool:
    """A ``PreToolUse`` start no completing event has claimed yet (#84).

    Carries the turn that was live when the tool STARTED, so reconciliation
    always parents the terminal span to its OWN turn — never to whatever turn
    happens to be current when the leftover is finally noticed (the phantom
    attribution this whole span shape exists to avoid). Holding the
    :class:`_TurnState` itself (not a snapshot) keeps its counters live even
    after ``current_turn`` has moved on.
    """

    tool_name: str
    start_ns: int
    turn: Optional[_TurnState]


@dataclass
class _SessionState:
    """Per-session bookkeeping — the exported root + running totals.

    Holds only the ended session-marker span (for its ``SpanContext``), the
    monotonic turn counter, the current turn (if a prompt has been submitted),
    the unclaimed tool starts, and session-wide counters — NEVER a held-open
    span (#42).
    """

    root: Any            # the ended session-marker span (holds the root SpanContext)
    started_ns: int      # session start (marker start) — for the summary duration
    tool_count: int = 0
    success_count: int = 0
    denied_count: int = 0
    incomplete_count: int = 0
    turn_count: int = 0                       # monotonic turn counter
    current_turn: Optional[_TurnState] = None  # the live turn, if any
    # Tool starts awaiting a completing event, keyed by tool name (the SDK's
    # ``HookInput`` has no per-call id, so pairing is name-keyed LIFO).
    pending_tools: Dict[str, List[_PendingTool]] = field(default_factory=dict)
    # Tool names reconciliation has ALREADY given a terminal span. A late
    # ``PostToolUse`` claims one of these instead of emitting a second terminal
    # span for the same call (#84). Bounded — see ``_MAX_TERMINALIZED``.
    terminalized: Deque[str] = field(
        default_factory=lambda: deque(maxlen=_MAX_TERMINALIZED)
    )


class ObservabilityHook(Hook):
    """Per-agent emitter — emits OTel spans (session ⊃ turn ⊃ tool ⊃ markers) via KestrelTracer."""

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
        # Frozen at construction: opt-in turn-root prompt capture + its cap.
        self._capture_prompts: bool = _env_flag(_CAPTURE_PROMPTS_ENV)
        self._max_io_chars: int = _max_io_chars()
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

    def _turn_attrs(
        self, session_id: Optional[str], turn: Optional[_TurnState]
    ) -> Dict[str, Any]:
        """Session + (given) turn attributes stamped on every span of a turn.

        ``kestrel.session_id`` and OpenInference ``session.id`` group a session
        across turns (attribute grouping, not a trace); ``kestrel.turn_id`` /
        ``kestrel.turn_index`` label that turn's spans so the renderer can group a
        turn by attribute OR by trace membership (#55). Turn attributes are absent
        when there is no turn (e.g. events arriving before any prompt).

        Taking the turn explicitly is what lets reconciliation stamp a leftover
        tool with its OWN turn rather than the live one (#84).
        """
        attrs = _session_attrs(session_id)
        if turn is not None:
            attrs[KESTREL_TURN_ID] = turn.turn_id
            attrs[KESTREL_TURN_INDEX] = turn.index
        return attrs

    def _scope_attrs(
        self, session: "_SessionState", session_id: Optional[str]
    ) -> Dict[str, Any]:
        """Session + CURRENT-turn attributes (the live-event path)."""
        return self._turn_attrs(session_id, session.current_turn)

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

        attributes = _session_attrs(session_id)
        agent_did = self._agent_did()
        if agent_did:
            attributes["kestrel.agent_did"] = agent_did

        # Export the session root IMMEDIATELY: a short session-marker span opened
        # AND ended now. We keep its SpanContext (via the returned ended span) to
        # parent every subsequent child. Referencing an already-exported parent is
        # valid OTel and renders correctly in Phoenix — and it means no held-open
        # root that could arrive orphaned for a long-lived agent (#42). ``root=True``
        # forces a fresh trace root so it never inherits an ambient host span the
        # hook happens to run inside (which would merge the session into that trace).
        session_marker_ns = time.time_ns()
        root = self._tracer.emit_span(
            agent_name,
            KIND_AGENT,
            root=True,
            start_time=session_marker_ns,
            end_time=session_marker_ns,
            agent_name=agent_name,
            orchestrator=orchestrator,
            attributes=attributes,
        )
        state = _SessionState(root=root, started_ns=session_marker_ns)
        self._sessions[session_id] = state
        return state

    def _start_turn(
        self,
        session: _SessionState,
        session_id: Optional[str],
        agent_name: str,
        user_message: Optional[str] = None,
    ) -> None:
        """Begin a turn on ``UserPromptSubmit`` — mint a new per-turn trace root.

        Bumps the session's monotonic turn counter and emits an immediately-ended
        ``AGENT`` span (``<agent> turn <n>``, ``kestrel.marker=start``) as a **new
        trace root** (``root=True`` → fresh empty context, so it never inherits an
        ambient host span), keeping its ``SpanContext`` in session state so the
        turn's tool spans/markers/summary parent to it. Keeps the #42 invariant:
        nothing is held open — the root is exported right away.

        When opt-in prompt capture is enabled (``KESTREL_OTEL_CAPTURE_PROMPTS=1``)
        the ``user_message`` prompt is stamped on the turn root as OpenInference
        ``input.value`` (truncated to the IO cap); default OFF records nothing.
        """
        session.turn_count += 1
        index = session.turn_count
        turn_id = f"{session_id}#{index}" if session_id else f"#{index}"

        # current_turn isn't set yet, so stamp the turn identity explicitly here
        # (every span of the turn carries session id + turn id + turn index).
        attributes = _session_attrs(session_id)
        attributes.update(
            {
                KESTREL_TURN_ID: turn_id,
                KESTREL_TURN_INDEX: index,
                KESTREL_MARKER: _MARKER_START,
            }
        )

        # Opt-in (KESTREL_OTEL_CAPTURE_PROMPTS=1): stamp the user prompt on the
        # turn root as OpenInference ``input.value``, truncated to the IO cap. Off
        # by default so user-message content is not recorded unless enabled.
        if self._capture_prompts and user_message:
            attributes[_INPUT_VALUE_KEY] = str(user_message)[: self._max_io_chars]

        turn_marker_ns = time.time_ns()
        root = self._tracer.emit_span(
            f"{agent_name} turn {index}",
            KIND_AGENT,
            root=True,
            start_time=turn_marker_ns,
            end_time=turn_marker_ns,
            agent_name=agent_name,
            attributes=attributes,
        )
        session.current_turn = _TurnState(
            root=root, index=index, turn_id=turn_id, started_ns=turn_marker_ns
        )

    def _turn_parent(self, session: _SessionState) -> Any:
        """The current turn root, or the session root as a fallback.

        Tool spans/markers parent to the live turn; before any prompt (e.g. the
        scheduler pseudo-session, or events arriving pre-prompt) they fall back to
        the session-marker root as today.
        """
        turn = session.current_turn
        return turn.root if turn is not None else session.root

    # ------------------------------------------------------------------
    # Tool lifecycle: start marker → pending bookkeeping → terminal span
    # ------------------------------------------------------------------

    def _should_emit_tool_start(self, input: HookInput) -> bool:
        """Whether this PreToolUse should emit a tool-start marker + pending entry.

        Normal agent tool calls always emit. Scheduler-sourced ticks (#42) are
        suppressed unless ``KESTREL_OTEL_TRACE_SCHEDULER`` opts into full tick
        tracing — at pre-tool time the tick's outcome isn't known yet, so a marker
        on every idle scheduler tick would re-introduce exactly the every-minute
        no-op noise, and (since the ``scheduler`` pseudo-session never sees a
        ``Stop``) its pending entry would linger instead of reconciling.
        """
        if input.session_id != _SCHEDULER_SESSION_ID:
            return True
        return self._trace_scheduler

    def _emit_tool_start_marker(
        self,
        session: _SessionState,
        session_id: Optional[str],
        agent_name: str,
        input: HookInput,
    ) -> None:
        """Emit an instant tool-start marker (``<tool> (started)``) on PreToolUse.

        The innermost doll: an attribute-light ``TOOL`` marker (tool name +
        session/turn ids + ``kestrel.marker=start``) parented to the current turn,
        which the Timeline pairs with the tool's terminal span (talon#80's
        start/close pairing convention). The start is also recorded as pending so
        a tool that never completes still gets a terminal span at turn end (#84).
        """
        attributes = self._scope_attrs(session, session_id)
        attributes[KESTREL_MARKER] = _MARKER_START
        attributes[KESTREL_TOOL_NAME] = input.tool_name

        marker_ns = time.time_ns()
        self._tracer.emit_span(
            f"{input.tool_name} (started)",
            KIND_TOOL,
            parent=self._turn_parent(session),
            start_time=marker_ns,
            end_time=marker_ns,
            agent_name=agent_name,
            attributes=attributes,
        )
        session.pending_tools.setdefault(input.tool_name, []).append(
            _PendingTool(
                tool_name=input.tool_name,
                start_ns=marker_ns,
                turn=session.current_turn,
            )
        )

    def _pop_pending_tool(
        self, session: _SessionState, tool_name: Optional[str]
    ) -> Optional[_PendingTool]:
        """Claim the pending start this completing event closes (name-keyed LIFO)."""
        if not tool_name:
            return None
        stack = session.pending_tools.get(tool_name)
        if not stack:
            return None
        record = stack.pop()
        if not stack:
            session.pending_tools.pop(tool_name, None)
        return record

    def _claim_terminalized(
        self, session: _SessionState, tool_name: Optional[str]
    ) -> bool:
        """Whether this completing event closes a call that is ALREADY terminal (#84).

        Reconciliation gives every still-pending start a terminal ``incomplete``
        span and drops the pending record. A ``PostToolUse`` that arrives AFTER
        that (the tool resolved on the far side of the turn barrier) would
        otherwise be treated as a brand-new call: a SECOND terminal span for one
        call, double-counted and parented to whatever turn is current by then.
        An OTLP span cannot be retroactively re-labeled ``incomplete`` →
        ``completed``, so the honest resolution is to keep the terminal span
        already exported and drop the late duplicate.

        Claiming is LIFO over the tool name (the SDK exposes no per-call id), and
        consumes the tombstone so only ONE late event per reconciled call is
        absorbed.
        """
        if not tool_name:
            return False
        tombstones = session.terminalized
        for i in range(len(tombstones) - 1, -1, -1):
            if tombstones[i] == tool_name:
                del tombstones[i]
                return True
        return False

    def _emit_outcome_span(
        self,
        session: _SessionState,
        session_id: Optional[str],
        agent_name: str,
        record: _PendingTool,
        outcome: str,
    ) -> None:
        """Emit the terminal span for a tool that never ran (denied / incomplete).

        A **zero-duration point span at the recorded start**: the tool never
        executed, so a start→now bar would claim runtime it never had (the exact
        phantom-bar failure this shape avoids). Parented to the pending record's
        OWN turn, so a leftover reconciled after the turn ended still nests where
        it belongs — and pairs with its ``(started)`` marker there.
        """
        attributes = self._turn_attrs(session_id, record.turn)
        attributes[KESTREL_TOOL_NAME] = record.tool_name
        attributes[KESTREL_TOOL_OUTCOME] = outcome
        parent = record.turn.root if record.turn is not None else session.root
        self._tracer.emit_tool_span(
            record.tool_name,
            parent=parent,
            start_time=record.start_ns,
            end_time=record.start_ns,
            agent_name=agent_name,
            extra={"tool.success": False},
            attributes=attributes,
        )

    def _reconcile_pending(
        self, session: _SessionState, session_id: Optional[str], agent_name: str
    ) -> None:
        """Turn-end reconciliation — the backbone of honest tool outcomes (#84).

        A turn does not end until its tools resolve, so anything still pending
        here was refused (a guard or permission rule denying it — neither fires
        any event the hook can see) or aborted. Each becomes a terminal
        ``incomplete`` span in its own turn, counted in the distinct
        ``incomplete_count`` dimension rather than as an agent error.

        A leftover can only belong to a turn that never saw its ``Stop`` (a
        normal ``Stop`` reconciles everything pending), so any stranded turn is
        also given its retroactive ``turn <n> summary`` — bounded to the last
        activity observed in it, never the live edge.
        """
        pending = session.pending_tools
        if not pending:
            return
        records = [record for stack in pending.values() for record in stack]
        session.pending_tools = {}
        records.sort(key=lambda record: record.start_ns)

        stranded: Dict[int, Any] = {}  # turn index → (turn, last activity ns)
        for record in records:
            self._emit_outcome_span(
                session, session_id, agent_name, record, TOOL_OUTCOME_INCOMPLETE
            )
            # Tombstone the call: this tool now HAS its terminal span, so a late
            # ``PostToolUse`` must not emit a second one (#84).
            session.terminalized.append(record.tool_name)
            session.incomplete_count += 1
            turn = record.turn
            if turn is None:
                continue
            turn.incomplete_count += 1
            if turn is not session.current_turn:
                previous = stranded.get(turn.index)
                latest = (
                    max(previous[1], record.start_ns)
                    if previous is not None
                    else record.start_ns
                )
                stranded[turn.index] = (turn, latest)

        for turn, latest in stranded.values():
            self._emit_turn_summary(
                session, session_id, agent_name, turn=turn, end_ns=latest
            )

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
        self,
        session: _SessionState,
        session_id: Optional[str],
        agent_name: str,
        input: HookInput,
        record: Optional[_PendingTool] = None,
    ) -> None:
        """Emit a child tool span for a completed tool call (PostToolUse).

        When the paired ``PreToolUse`` start is available, the span is parented,
        attributed AND counted against that record's OWN turn rather than
        whatever turn is current now — a completion that lands after its turn
        closed (an interrupt, or a late event) belongs to the turn it started in,
        never to the turn that happens to be live (#84). With no record (a tool
        whose start was never seen, e.g. the filtered scheduler path) the live
        turn stays the best available attribution.
        """
        # Derive success from the envelope: prefer top-level ``success`` but fall
        # back to ``status`` for external-feature ToolResults that omit it (#42),
        # so an errored scheduler tick isn't stamped tool.success=True.
        success = _tick_success(input.tool_response)

        extra: Dict[str, Any] = {"tool.success": success}
        if input.execution_time_ms is not None:
            extra["tool.duration_ms"] = input.execution_time_ms

        turn = record.turn if record is not None else session.current_turn
        attributes = self._turn_attrs(session_id, turn)
        attributes[KESTREL_TOOL_NAME] = input.tool_name
        attributes[KESTREL_TOOL_OUTCOME] = TOOL_OUTCOME_COMPLETED
        if input.feature_name:
            attributes["kestrel.feature_name"] = input.feature_name
        if not success and isinstance(input.tool_response, dict):
            # Privacy: truncate error text (never user-message content).
            attributes["tool.error"] = str(input.tool_response.get("error", ""))[:200]

        # PostToolUse fires after the tool completed, so backdate the span start
        # to end − duration → the exported span's duration is the real tool
        # runtime (a correct waterfall), not ~0. The span is explicitly parented
        # to the current turn (fallback: the session root) so nesting stays correct.
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
            parent=turn.root if turn is not None else session.root,
            start_time=start_time,
            end_time=end_ns,
            agent_name=agent_name,
            extra=extra,
            attributes=attributes,
        )
        session.tool_count += 1
        if success:
            session.success_count += 1
        if turn is not None:
            turn.tool_count += 1
            if success:
                turn.success_count += 1

    def _emit_turn_summary(
        self,
        session: _SessionState,
        session_id: Optional[str],
        agent_name: str,
        turn: Optional[_TurnState] = None,
        end_ns: Optional[int] = None,
    ) -> None:
        """Emit a ``turn <n> summary`` span (parented to the turn root) with per-turn totals.

        Defaults to the live turn ending now; reconciliation passes a stranded
        (interrupted) turn explicitly with an ``end_ns`` bounded to the last
        activity observed in it, so every turn is summarized exactly once.
        """
        turn = turn if turn is not None else session.current_turn
        if turn is None or turn.summarized:
            return
        turn.summarized = True
        end_ns = end_ns if end_ns is not None else time.time_ns()
        end_ns = max(end_ns, turn.started_ns)
        tool_count = turn.tool_count
        success_count = turn.success_count
        success_ratio = (success_count / tool_count) if tool_count else 1.0
        duration_ms = (end_ns - turn.started_ns) / 1_000_000

        extra: Dict[str, Any] = {
            # Executed tools only — a refused tool is not an agent error (#84).
            "kestrel.tool_count": tool_count,
            "kestrel.error_count": tool_count - success_count,
            "kestrel.success_ratio": success_ratio,
            KESTREL_DENIED_COUNT: turn.denied_count,
            KESTREL_INCOMPLETE_COUNT: turn.incomplete_count,
            # Unified go-forward duration key across turn + session summaries.
            "kestrel.duration_ms": duration_ms,
            # Legacy per-scope duration key, emitted alongside for back-compat
            # with existing dashboards / the #62 renderer; drop in a future major.
            "kestrel.turn_duration_ms": duration_ms,
        }
        # Session + turn ids on the summary too (every span of the turn carries them).
        attributes = self._turn_attrs(session_id, turn)

        # A `turn <n> summary` span in the SAME per-turn trace, parented to the
        # exported turn root — per-turn totals without any held-open span (#42/#55).
        self._tracer.emit_span(
            f"turn {turn.index} summary",
            KIND_CHAIN,
            parent=turn.root,
            start_time=turn.started_ns,
            end_time=end_ns,
            agent_name=agent_name,
            extra=extra,
            attributes=attributes,
        )

    def _close_turn(self, session_id: Optional[str], agent_name: str) -> None:
        """End the turn: reconcile leftovers FIRST, then emit its summary.

        Reconciliation runs before the summary so the turn's
        ``incomplete_count`` is complete when the summary is stamped. Also
        drives ``UserPromptSubmit`` (closing a turn interrupted before its
        ``Stop``), so every turn is summarized exactly once.

        The session stays live (its marker root + paired ``kestrel.session_id`` /
        ``session.id`` attributes are stable across turns); the next
        ``UserPromptSubmit`` mints turn n+1. A close with no live turn and
        nothing pending is a no-op.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            self._reconcile_pending(session, session_id, agent_name)
            self._emit_turn_summary(session, session_id, agent_name)
        except Exception as e:  # noqa: BLE001 - never fatal
            logger.debug(
                "ObservabilityHook turn summary emit failed (non-fatal): %s", e
            )
        # Turn done: clear it so any stray post-Stop tool event falls back to the
        # session root, and turn totals stay captured in the session totals.
        session.current_turn = None

    def _emit_session_summary(
        self, state: _SessionState, session_id: Optional[str], agent_name: str
    ) -> None:
        """Emit a ``session summary`` span (parented to the root) aggregating turns + totals."""
        end_ns = time.time_ns()
        tool_count = state.tool_count
        success_count = state.success_count
        success_ratio = (success_count / tool_count) if tool_count else 1.0
        duration_ms = (end_ns - state.started_ns) / 1_000_000

        extra: Dict[str, Any] = {
            "kestrel.turn_count": state.turn_count,
            # Executed tools only — a refused tool is not an agent error (#84).
            "kestrel.tool_count": tool_count,
            "kestrel.error_count": tool_count - success_count,
            "kestrel.success_ratio": success_ratio,
            KESTREL_DENIED_COUNT: state.denied_count,
            KESTREL_INCOMPLETE_COUNT: state.incomplete_count,
            # Unified go-forward duration key across turn + session summaries.
            "kestrel.duration_ms": duration_ms,
            # Legacy per-scope duration key (back-compat); drop in a future major.
            "kestrel.session_duration_ms": duration_ms,
        }
        attributes = _session_attrs(session_id)

        # A `session summary` span in the session-marker trace, parented to the
        # exported root — carries session totals without any held-open span (#42).
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
        """Emit the true session summary and pop the session (AgentTerminate / teardown)."""
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        try:
            # Reconcile + summarize a turn abandoned without its ``Stop`` before
            # the session totals are stamped, so no tool and no turn goes
            # unrepresented when a session ends mid-turn (#84).
            self._reconcile_pending(state, session_id, agent_name)
            self._emit_turn_summary(state, session_id, agent_name)
            state.current_turn = None
        except Exception as e:  # noqa: BLE001 - never fatal
            logger.debug(
                "ObservabilityHook turn reconciliation failed (non-fatal): %s", e
            )
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
                        record = self._pop_pending_tool(session, input.tool_name)
                        if record is None and self._claim_terminalized(
                            session, input.tool_name
                        ):
                            # A completion for a call reconciliation already
                            # terminalized as ``incomplete``: emitting now would
                            # give ONE call two terminal spans, the second one
                            # counted twice and parented to the wrong turn (#84).
                            pass
                        else:
                            # Pass the claimed start so the span nests + counts in
                            # the turn the tool STARTED in, not the current one.
                            self._emit_tool_span(
                                session, session_id, agent_name, input, record
                            )
                    else:
                        # A filtered idle scheduler tick (#42) emits no span, so
                        # drop its pending start too — otherwise reconciliation
                        # would later mislabel a SUCCESSFUL idle tick as refused.
                        existing = self._sessions.get(session_id)
                        if existing is not None:
                            if self._pop_pending_tool(existing, input.tool_name) is None:
                                # Nothing pending: retire any tombstone instead, so
                                # a filtered tick can't strand one.
                                self._claim_terminalized(existing, input.tool_name)
                elif event_type == "PreToolUse" and input.tool_name:
                    # Innermost doll: an instant tool-start marker parented to the
                    # current turn — the Timeline pairs it with the tool's terminal
                    # span, and the recorded start lets turn-end reconciliation
                    # represent a tool that never completes (#84).
                    if self._should_emit_tool_start(input):
                        session = self._ensure_session(session_id, agent_name, input)
                        self._emit_tool_start_marker(
                            session, session_id, agent_name, input
                        )
                elif event_type == "UserPromptSubmit":
                    # Start a turn: a new per-turn trace root under the session.
                    # Use the post-rewrite prompt (an earlier hook may have
                    # redacted/rewritten it into ``tool_input``) so capture never
                    # exports a prompt the model never saw.
                    session = self._ensure_session(session_id, agent_name, input)
                    # A turn still live here never saw its ``Stop`` (interrupted):
                    # close it — reconciling its leftovers into ITS OWN turn —
                    # before minting the next one, so nothing is ever attributed
                    # to the new turn (#84).
                    self._close_turn(session_id, agent_name)
                    self._start_turn(
                        session, session_id, agent_name, _effective_prompt(input)
                    )
                elif event_type == "Stop":
                    # End the turn (reconcile leftovers, then the turn summary) —
                    # but keep the session live.
                    self._close_turn(session_id, agent_name)
                elif event_type == "AgentTerminate":
                    # End the session (true session summary aggregating turns).
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
