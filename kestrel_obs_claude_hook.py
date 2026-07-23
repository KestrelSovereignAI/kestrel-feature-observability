"""
Claude Code hook emitter — ``kestrel-obs-claude-hook`` console script.

Claude Code's hook system runs a shell command per lifecycle event with a JSON
payload on stdin (``hook_event_name``, ``session_id``, ``cwd``,
``transcript_path`` and, for tool events, ``tool_name`` / ``tool_input`` /
``tool_response``). This module turns those out-of-process one-shot invocations
into the very same OpenInference/Kestrel spans the in-process per-agent emitter
produces (:mod:`kestrel_feature_observability.hook`, #55), so Claude Code sessions
show up in the fleet Observability Timeline exactly like kestrel agents and talon
runs.

Span shape (mirrors the #55 conventions — session ⊃ turn ⊃ tool ⊃ tool-start
markers, one trace per turn):

- ``SessionStart`` → an immediately-ended ``AGENT`` session-marker root, a fresh
  trace whose ``SpanContext`` every later span parents to. ``kestrel.session_id``
  = the Claude ``session_id``; ``kestrel.agent_name`` = ``claude-code``;
  ``kestrel.orchestrator`` = ``$KESTREL_OBSERVABILITY_ORCHESTRATOR`` else
  ``Direct``. A ``source`` of ``compact`` / ``resume`` / ``fork`` on an existing
  session is a **no-op** (the live session is preserved, not re-minted) since
  Claude Code reuses the ``session_id`` across those.
- ``UserPromptSubmit`` → a labeled ``<agent> turn <n>`` ``AGENT`` root (a **new**
  trace), ``kestrel.turn_id`` = ``<session_id>#<n>``, ``kestrel.turn_index`` = n,
  ``kestrel.marker=start``.
- ``PreToolUse`` → an instant ``<tool> (started)`` ``TOOL`` marker
  (``kestrel.marker=start``) parented to the current turn; its start is recorded
  keyed by ``tool_use_id`` so parallel same-name tools pair correctly. The
  ``tool_use_id`` is also stamped as ``tool.call_id`` on the marker AND its
  completed span so the Timeline pairs concurrent same-name calls one-to-one.
- ``PostToolUse`` / ``PostToolUseFailure`` → a completed ``TOOL`` span parented to
  the current turn (``tool.name`` / ``tool.success`` / truncated ``tool.error``).
  ``PostToolUse`` derives success from ``tool_response``; ``PostToolUseFailure``
  reports the outcome out-of-band via top-level ``error`` / ``duration_ms`` and is
  always a failure. Duration prefers the payload's own ``duration_ms`` (the real
  tool runtime), else the gap to the paired ``PreToolUse`` (never negative).
- ``Stop`` → a ``turn <n> summary`` ``CHAIN`` spanning turn-start→now (tool count,
  duration, success ratio). The session stays open.
- ``SessionEnd`` (and a defensive staleness sweep) → a ``session summary``
  ``CHAIN`` aggregating the turns, then the session state file is removed.
- ``SubagentStop`` → a nested ``AGENT`` span under the current turn (best effort).

Cross-process parenting without a daemon: each hook invocation is a new process,
so a tiny per-session state file (``$KESTREL_OBS_CLAUDE_STATE_DIR`` /
``$XDG_STATE_HOME/kestrel-obs-claude`` / ``$TMPDIR/kestrel-obs-claude`` →
``<session_id>.json``) carries the session-root trace/span ids, the current turn
ids + counter + start ts, and the resolved project. Later invocations reconstruct
a remote ``SpanContext`` from those ids so spans across invocations share traces.
The file is written atomically (write-rename) under a per-session ``flock`` so
concurrent hook processes (Claude Code fires ``PostToolUse`` concurrently for
parallel tools) serialize their read-modify-write instead of losing updates; a
missing/corrupt file is tolerated by re-minting a session root.

Projects = repos: ``openinference.project.name`` resolves from the payload
``cwd``'s git remote (``owner/repo``), else ``$KESTREL_OTEL_PROJECT``, else it is
omitted. The result is cached per session in the state file so git runs at most
once per session.

Hard constraints (this must never disturb the Claude Code session):

- Always exit 0; print NOTHING to stdout (Claude Code interprets PreToolUse/Stop
  stdout for gating). Errors go at most to stderr and only under
  ``$KESTREL_OBS_CLAUDE_DEBUG``.
- No-op **instantly** — before importing OpenTelemetry or this package — when
  neither ``OTEL_EXPORTER_OTLP_ENDPOINT`` nor ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``
  is set. That is why this is a standalone top-level module (importing it never
  runs the ``kestrel_feature_observability`` package ``__init__``) and every OTel /
  tracing import is deferred until the enabled path.
- A single OTLP HTTP POST with a ~1s timeout, ``force_flush`` before exit, fail
  silent. User-message content (the ``prompt``) is never recorded on any span.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

# The emitter's span identity (kept as local literals so importing this module
# never drags in kestrel_sdk / OpenTelemetry — mirrors
# ``kestrel_feature_observability.hook``).
_AGENT_NAME = "claude-code"
_SERVICE_NAME = "claude-code"
_INSTRUMENTATION_NAME = "kestrel_feature_observability.claude_hook"
_PROJECT_NAME_KEY = "openinference.project.name"

# Standard Kestrel span attribute keys (mirror ``hook.py``).
KESTREL_SESSION_ID = "kestrel.session_id"
KESTREL_TURN_ID = "kestrel.turn_id"
KESTREL_TURN_INDEX = "kestrel.turn_index"
KESTREL_MARKER = "kestrel.marker"
KESTREL_TOOL_NAME = "tool.name"
# Non-sensitive per-call correlation id (Claude Code's ``tool_use_id``) stamped
# on BOTH the ``<tool> (started)`` marker and its completed span so the Timeline
# pairs concurrent same-name tools one-to-one instead of the first close hiding
# every same-name marker (#62 P2).
KESTREL_TOOL_CALL_ID = "tool.call_id"
_MARKER_START = "start"

# OpenInference span-kind values (uppercase literals — identical to
# ``tracing.KIND_*`` on both the SDK-present and fallback paths).
_KIND_AGENT = "AGENT"
_KIND_CHAIN = "CHAIN"
_KIND_TOOL = "TOOL"

# Standard OTLP endpoint env vars — drive the no-op-when-unset behavior.
_ENDPOINT_ENVS = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)

# Orchestrator + project selection.
_ORCHESTRATOR_ENV = "KESTREL_OBSERVABILITY_ORCHESTRATOR"
_PROJECT_ENV = "KESTREL_OTEL_PROJECT"
_DEFAULT_ORCHESTRATOR = "Direct"

# State + staleness knobs.
_STATE_DIR_ENV = "KESTREL_OBS_CLAUDE_STATE_DIR"
_TTL_ENV = "KESTREL_OBS_CLAUDE_SESSION_TTL"  # seconds
_DEBUG_ENV = "KESTREL_OBS_CLAUDE_DEBUG"
_DEFAULT_TTL_S = 6 * 3600
_MAX_STALE_SWEEP = 16

# Budget knobs — a single fast POST, capped so a dead endpoint can't hang Claude.
_HTTP_TIMEOUT_S = 1
_FLUSH_TIMEOUT_MS = 1200
_GIT_TIMEOUT_S = 0.5

# ``tool_response`` status tokens (and kin) that mean the tool did NOT succeed.
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})

# ``SessionStart.source`` values that CONTINUE an existing session rather than
# beginning a fresh one. Claude Code reuses the same ``session_id`` across
# compaction/resume/fork, so re-minting on these would blow away the live turn
# counter, pending tools and session root (duplicate turn ids, lost summaries).
_CONTINUATION_SOURCES = frozenset({"compact", "resume", "fork"})


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _endpoint() -> Optional[str]:
    """The configured OTLP endpoint, or ``None`` → instant no-op (no OTel import)."""
    for env in _ENDPOINT_ENVS:
        val = os.environ.get(env)
        if val:
            return val
    return None


def _orchestrator() -> str:
    """``$KESTREL_OBSERVABILITY_ORCHESTRATOR`` else ``Direct``."""
    return os.environ.get(_ORCHESTRATOR_ENV) or _DEFAULT_ORCHESTRATOR


def _debug(msg: Any) -> None:
    """Emit a diagnostic to stderr — only under ``$KESTREL_OBS_CLAUDE_DEBUG``. Never stdout."""
    if os.environ.get(_DEBUG_ENV):
        try:
            print(f"kestrel-obs-claude-hook: {msg}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            pass


# ---------------------------------------------------------------------------
# Project resolution (projects = repos)
# ---------------------------------------------------------------------------

def _slug_from_remote_url(url: Optional[str]) -> Optional[str]:
    """Parse ``owner/repo`` out of an ``origin`` remote URL (SSH or HTTPS)."""
    url = (url or "").strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        # https://host/owner/repo or ssh://git@host/owner/repo
        rest = url.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    elif "@" in url and ":" in url:
        # scp-like: git@host:owner/repo
        path = url.split(":", 1)[1]
    else:
        path = url
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return None


def _git_remote_slug(cwd: Optional[str]) -> Optional[str]:
    """``owner/repo`` for ``cwd``'s ``origin`` remote, or ``None`` (fast, bounded)."""
    if not cwd:
        return None
    try:
        import subprocess

        out = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - git missing / slow / not a repo → no project
        return None
    if out.returncode != 0:
        return None
    return _slug_from_remote_url(out.stdout)


def _resolve_project(cwd: Optional[str], cached: Optional[str]) -> Optional[str]:
    """Cached slug wins, else the ``cwd`` git remote, else ``$KESTREL_OTEL_PROJECT``, else omit."""
    if cached:
        return cached
    slug = _git_remote_slug(cwd)
    if slug:
        return slug
    return os.environ.get(_PROJECT_ENV) or None


# ---------------------------------------------------------------------------
# Per-session state file (atomic, corruption-tolerant)
# ---------------------------------------------------------------------------

def _state_dir() -> pathlib.Path:
    """The per-session state directory (env override → XDG state → tmp)."""
    override = os.environ.get(_STATE_DIR_ENV)
    if override:
        return pathlib.Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return pathlib.Path(xdg) / "kestrel-obs-claude"
    return pathlib.Path(tempfile.gettempdir()) / "kestrel-obs-claude"


def _sanitize(session_id: str) -> str:
    """A filesystem-safe stem for a session id."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in session_id)
    return safe or "session"


def _state_filename(session_id: str) -> str:
    return f"{_sanitize(session_id)}.json"


def _state_path(session_id: str) -> pathlib.Path:
    return _state_dir() / _state_filename(session_id)


def _read_state(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Load a state file; a missing/corrupt/non-dict file yields ``None``."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_state_atomic(path: pathlib.Path, state: Dict[str, Any]) -> None:
    """Write the state via a temp file + rename so concurrent readers never see a partial file."""
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unlink_quiet(path: pathlib.Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _lock_path(session_id: str) -> pathlib.Path:
    """The per-session advisory-lock file (sibling of the ``.json`` state file)."""
    return _state_dir() / f"{_sanitize(session_id)}.lock"


@contextlib.contextmanager
def _session_lock(session_id: str):
    """Best-effort exclusive per-session lock (POSIX ``flock``); a no-op where unavailable.

    Serializes the read-modify-write of a session's state file so concurrent hook
    processes — Claude Code fires ``PostToolUse`` concurrently for parallel tools —
    can't clobber each other's counters or pending-tool entries (atomic rename
    prevents partial files, not lost updates). Scoped to the read+dispatch+write
    window only; the OTLP export runs outside it. Locking failures degrade to
    running unserialized rather than disturbing the session.
    """
    try:
        import fcntl
    except Exception:  # noqa: BLE001 - non-POSIX → run unserialized rather than fail
        fcntl = None
    f = None
    if fcntl is not None:
        try:
            path = _lock_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            f = open(path, "w")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception as e:  # noqa: BLE001 - locking is best effort
            _debug(e)
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
                f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass


def _ttl_ns() -> int:
    """Session staleness TTL in ns (``$KESTREL_OBS_CLAUDE_SESSION_TTL`` seconds)."""
    raw = os.environ.get(_TTL_ENV)
    try:
        secs = float(raw) if raw else _DEFAULT_TTL_S
    except ValueError:
        secs = _DEFAULT_TTL_S
    return int(secs * 1_000_000_000)


def _last_event_ns(state: Dict[str, Any]) -> int:
    return int(state.get("last_event_ns") or state.get("session_started_ns") or 0)


def _is_stale(state: Dict[str, Any], now_ns: int) -> bool:
    return (now_ns - _last_event_ns(state)) > _ttl_ns()


# ---------------------------------------------------------------------------
# OTel wiring (deferred — only reached on the enabled path)
# ---------------------------------------------------------------------------

def _build_tracer(endpoint: str, project: Optional[str]):
    """Build a ``(KestrelTracer, TracerProvider)`` wired to the OTLP/HTTP exporter.

    Reuses the #55 span builders (:class:`kestrel_feature_observability.tracing.KestrelTracer`).
    The OTLP exporter reads the endpoint from the standard env vars (appending
    ``/v1/traces`` to the base one), so only a hard ~1s HTTP timeout is passed.
    ``project`` (when known) is stamped as the ``openinference.project.name``
    Resource attribute so Phoenix routes the spans into the matching project.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from kestrel_feature_observability.tracing import KestrelTracer

    attrs: Dict[str, str] = {"service.name": _SERVICE_NAME}
    if project:
        attrs[_PROJECT_NAME_KEY] = project
    provider = TracerProvider(resource=Resource.create(attrs))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(timeout=_HTTP_TIMEOUT_S))
    )
    tracer = KestrelTracer(tracer=provider.get_tracer(_INSTRUMENTATION_NAME))
    return tracer, provider


def _flush(provider: Any) -> None:
    """Force a single export before exit; bounded so a dead endpoint can't hang."""
    if provider is None:
        return
    try:
        provider.force_flush(_FLUSH_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - export must never disturb the session
        pass


def _ids_of(span: Any) -> Dict[str, str]:
    """The trace/span ids of an emitted span, as hex — stored for cross-process parenting."""
    ctx = span.get_span_context()
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


def _remote_parent(ids: Dict[str, str]) -> Any:
    """Reconstruct a live-enough parent span from stored ids (a remote, SAMPLED ``SpanContext``)."""
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    ctx = SpanContext(
        trace_id=int(ids["trace_id"], 16),
        span_id=int(ids["span_id"], 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return NonRecordingSpan(ctx)


# ---------------------------------------------------------------------------
# Span emission (mirrors kestrel_feature_observability.hook)
# ---------------------------------------------------------------------------

def _scope_attrs(state: Dict[str, Any]) -> Dict[str, Any]:
    """Session + current-turn ids stamped on every span of a turn."""
    attrs: Dict[str, Any] = {KESTREL_SESSION_ID: state["session_id"]}
    turn = state.get("current_turn")
    if turn:
        attrs[KESTREL_TURN_ID] = turn["turn_id"]
        attrs[KESTREL_TURN_INDEX] = turn["index"]
    return attrs


def _turn_parent(state: Dict[str, Any]) -> Any:
    """The current turn root (fallback: the session root) as a reconstructed parent."""
    turn = state.get("current_turn")
    ids = turn["root"] if turn else state["session_root"]
    return _remote_parent(ids)


def _new_session(
    tracer: Any, session_id: str, project: Optional[str], now_ns: int
) -> Dict[str, Any]:
    """Mint an immediately-ended ``AGENT`` session-marker root and its fresh state."""
    orchestrator = _orchestrator()
    span = tracer.emit_span(
        _AGENT_NAME,
        _KIND_AGENT,
        root=True,
        start_time=now_ns,
        end_time=now_ns,
        agent_name=_AGENT_NAME,
        orchestrator=orchestrator,
        attributes={KESTREL_SESSION_ID: session_id},
    )
    return {
        "session_id": session_id,
        "project": project,
        "orchestrator": orchestrator,
        "session_root": _ids_of(span),
        "session_started_ns": now_ns,
        "turn_count": 0,
        "tool_count": 0,
        "success_count": 0,
        "current_turn": None,
        "pending_tools": {},
    }


def _ensure_session(
    tracer: Any,
    state: Optional[Dict[str, Any]],
    session_id: str,
    project: Optional[str],
    now_ns: int,
) -> Dict[str, Any]:
    """Return the session state, re-minting a root when it is missing/corrupt."""
    if state is not None and state.get("session_root"):
        return state
    return _new_session(tracer, session_id, project, now_ns)


def _start_turn(tracer: Any, state: Dict[str, Any], session_id: str, now_ns: int) -> None:
    """``UserPromptSubmit`` → an immediately-ended ``<agent> turn <n>`` root (a new trace)."""
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    index = state["turn_count"]
    turn_id = f"{session_id}#{index}"
    span = tracer.emit_span(
        f"{_AGENT_NAME} turn {index}",
        _KIND_AGENT,
        root=True,
        start_time=now_ns,
        end_time=now_ns,
        agent_name=_AGENT_NAME,
        attributes={
            KESTREL_SESSION_ID: session_id,
            KESTREL_TURN_ID: turn_id,
            KESTREL_TURN_INDEX: index,
            KESTREL_MARKER: _MARKER_START,
        },
    )
    state["current_turn"] = {
        "root": _ids_of(span),
        "index": index,
        "turn_id": turn_id,
        "started_ns": now_ns,
        "tool_count": 0,
        "success_count": 0,
    }


def _emit_tool_start(tracer: Any, state: Dict[str, Any], payload: Dict[str, Any], now_ns: int) -> None:
    """``PreToolUse`` → an instant ``<tool> (started)`` marker parented to the current turn."""
    tool_name = str(payload.get("tool_name") or "tool")
    tool_use_id = payload.get("tool_use_id")
    attrs = _scope_attrs(state)
    attrs[KESTREL_MARKER] = _MARKER_START
    attrs[KESTREL_TOOL_NAME] = tool_name
    # Stamp the per-call id so the Timeline pairs THIS marker with its own
    # completed span even when concurrent same-name tools share the turn (#62 P2).
    if tool_use_id:
        attrs[KESTREL_TOOL_CALL_ID] = str(tool_use_id)
    tracer.emit_span(
        f"{tool_name} (started)",
        _KIND_TOOL,
        parent=_turn_parent(state),
        start_time=now_ns,
        end_time=now_ns,
        agent_name=_AGENT_NAME,
        attributes=attrs,
    )
    # Record the start so the paired PostToolUse can backdate a real duration.
    # Key by tool_use_id when Claude Code provides one so concurrent same-name
    # tools (parallel Bash calls) pair to their OWN start; fall back to the name.
    pending = state.setdefault("pending_tools", {})
    key = _pending_key(tool_name, tool_use_id)
    pending.setdefault(key, []).append(now_ns)


def _pending_key(tool_name: str, tool_use_id: Any) -> str:
    """Correlation key for a pending tool start — the ``tool_use_id`` when present, else the name."""
    if tool_use_id:
        return f"id:{tool_use_id}"
    return f"name:{tool_name}"


def _pop_pending(state: Dict[str, Any], tool_name: str, tool_use_id: Any) -> Optional[int]:
    """Pop the start paired to this call (by ``tool_use_id``, else the tool name LIFO)."""
    pending = state.get("pending_tools") or {}
    key = _pending_key(tool_name, tool_use_id)
    stack: List[int] = pending.get(key) or []
    if not stack:
        return None
    start = stack.pop()
    if not stack:
        pending.pop(key, None)
    return int(start)


def _tool_success(resp: Any) -> bool:
    """Best-effort success for a Claude Code ``tool_response`` (object or string)."""
    if isinstance(resp, dict):
        if "success" in resp:
            return bool(resp["success"])
        for key in ("is_error", "isError", "error"):
            if resp.get(key):
                return False
        status = str(resp.get("status") or "").strip().lower()
        if status in _FAILURE_STATUSES:
            return False
    return True


def _error_text(payload: Dict[str, Any]) -> str:
    """Truncated error text — top-level ``error`` (``PostToolUseFailure``) else the ``tool_response``.

    Never user-message content; capped at 200 chars for privacy.
    """
    txt = payload.get("error")
    if not txt:
        resp = payload.get("tool_response")
        if isinstance(resp, dict):
            txt = resp.get("error") or resp.get("stderr") or ""
        elif isinstance(resp, str):
            txt = resp
        else:
            txt = ""
    return str(txt)[:200]


def _payload_duration_ms(payload: Dict[str, Any]) -> Optional[float]:
    """The tool's own measured ``duration_ms`` (``PostToolUse``/``PostToolUseFailure``), if valid."""
    raw = payload.get("duration_ms")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val >= 0 else None


def _emit_tool_span(
    tracer: Any,
    state: Dict[str, Any],
    payload: Dict[str, Any],
    now_ns: int,
    failed: bool = False,
) -> None:
    """``PostToolUse`` / ``PostToolUseFailure`` → a completed ``TOOL`` span under the turn.

    ``PostToolUse`` carries a ``tool_response`` (success derived from it);
    ``PostToolUseFailure`` reports the outcome out-of-band via top-level ``error`` /
    ``duration_ms`` and is ALWAYS a failure. Duration prefers the payload's own
    ``duration_ms`` (the real tool runtime), else the gap to the paired
    ``PreToolUse`` (never negative).
    """
    tool_name = str(payload.get("tool_name") or "tool")
    tool_use_id = payload.get("tool_use_id")
    success = False if failed else _tool_success(payload.get("tool_response"))

    # Always pop the paired PreToolUse start so pending state can't leak, even
    # when the payload's own duration_ms ultimately wins.
    start_ns = _pop_pending(state, tool_name, tool_use_id)
    duration_ms = _payload_duration_ms(payload)
    if duration_ms is not None:
        span_start = max(now_ns - int(duration_ms * 1_000_000), 0)
    elif start_ns is not None and start_ns <= now_ns:
        span_start = start_ns
        duration_ms = (now_ns - start_ns) / 1_000_000
    else:
        # No derivable start → a zero-duration point span. NEVER start > end.
        span_start = now_ns

    extra: Dict[str, Any] = {"tool.success": success}
    if duration_ms is not None:
        extra["tool.duration_ms"] = duration_ms

    attrs = _scope_attrs(state)
    # Same per-call id as the marker so the Timeline pairs this completed span to
    # its own "(started)" marker among concurrent same-name calls (#62 P2).
    if tool_use_id:
        attrs[KESTREL_TOOL_CALL_ID] = str(tool_use_id)
    if not success:
        err = _error_text(payload)
        if err:
            attrs["tool.error"] = err

    tracer.emit_tool_span(
        tool_name,
        parent=_turn_parent(state),
        start_time=span_start,
        end_time=now_ns,
        agent_name=_AGENT_NAME,
        extra=extra,
        attributes=attrs,
    )

    state["tool_count"] = int(state.get("tool_count", 0)) + 1
    if success:
        state["success_count"] = int(state.get("success_count", 0)) + 1
    turn = state.get("current_turn")
    if turn:
        turn["tool_count"] = int(turn.get("tool_count", 0)) + 1
        if success:
            turn["success_count"] = int(turn.get("success_count", 0)) + 1


def _emit_subagent(tracer: Any, state: Dict[str, Any], payload: Dict[str, Any], now_ns: int) -> None:
    """``SubagentStop`` → a nested ``AGENT`` marker under the current turn (best effort)."""
    name = str(payload.get("agent_type") or "subagent")
    tracer.emit_span(
        name,
        _KIND_AGENT,
        parent=_turn_parent(state),
        start_time=now_ns,
        end_time=now_ns,
        agent_name=name,
        attributes=_scope_attrs(state),
    )


def _close_turn(tracer: Any, state: Dict[str, Any], now_ns: int) -> None:
    """``Stop`` → a ``turn <n> summary`` ``CHAIN`` (session stays open)."""
    turn = state.get("current_turn")
    if not turn:
        return
    tool_count = int(turn.get("tool_count", 0))
    success_ratio = (turn.get("success_count", 0) / tool_count) if tool_count else 1.0
    tracer.emit_span(
        f"turn {turn['index']} summary",
        _KIND_CHAIN,
        parent=_remote_parent(turn["root"]),
        start_time=int(turn["started_ns"]),
        end_time=now_ns,
        agent_name=_AGENT_NAME,
        extra={
            "kestrel.tool_count": tool_count,
            "kestrel.success_ratio": success_ratio,
            "kestrel.turn_duration_ms": (now_ns - int(turn["started_ns"])) / 1_000_000,
        },
        attributes=_scope_attrs(state),
    )
    state["current_turn"] = None


def _close_session(tracer: Any, state: Dict[str, Any], now_ns: int) -> None:
    """``SessionEnd`` / staleness expiry → a ``session summary`` ``CHAIN`` aggregating turns."""
    root = state.get("session_root")
    if not root:
        return
    tool_count = int(state.get("tool_count", 0))
    success_ratio = (state.get("success_count", 0) / tool_count) if tool_count else 1.0
    started = int(state.get("session_started_ns") or now_ns)
    tracer.emit_span(
        "session summary",
        _KIND_CHAIN,
        parent=_remote_parent(root),
        start_time=started,
        end_time=now_ns,
        agent_name=_AGENT_NAME,
        extra={
            "kestrel.turn_count": int(state.get("turn_count", 0)),
            "kestrel.tool_count": tool_count,
            "kestrel.success_ratio": success_ratio,
            "kestrel.session_duration_ms": (now_ns - started) / 1_000_000,
        },
        attributes={KESTREL_SESSION_ID: state["session_id"]},
    )


def _expire_current(
    state: Dict[str, Any], state_path: pathlib.Path, now_ns: int, endpoint: str
) -> None:
    """A stale *current* session: emit + flush its overdue ``session summary``, then drop its file.

    The cross-session sweep deliberately skips the current session, so without
    this a re-minted session would silently swallow the abandoned one's required
    ``session summary`` CHAIN. Runs before the fresh state is minted.
    """
    try:
        tracer, provider = _build_tracer(endpoint, state.get("project"))
        try:
            _close_session(tracer, state, now_ns)
        finally:
            _flush(provider)
    except Exception as e:  # noqa: BLE001 - an expiry summary must never disturb anything
        _debug(e)
    _unlink_quiet(state_path)


def _sweep_stale(current_session_id: str, now_ns: int, endpoint: str) -> None:
    """Defensively close + remove abandoned session files (session boundaries only)."""
    d = _state_dir()
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return
    current = _state_filename(current_session_id)
    ttl = _ttl_ns()
    closed = 0
    for path in files:
        if closed >= _MAX_STALE_SWEEP:
            break
        if path.name == current:
            continue
        st = _read_state(path)
        if st is None:
            _unlink_quiet(path)  # corrupt leftover → clean up
            continue
        if (now_ns - _last_event_ns(st)) <= ttl:
            continue
        try:
            tracer, provider = _build_tracer(endpoint, st.get("project"))
            try:
                _close_session(tracer, st, now_ns)
            finally:
                _flush(provider)
        except Exception as e:  # noqa: BLE001 - a stale summary must never disturb anything
            _debug(e)
        _unlink_quiet(path)
        closed += 1


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch(
    event: str,
    tracer: Any,
    state: Optional[Dict[str, Any]],
    session_id: str,
    project: Optional[str],
    payload: Dict[str, Any],
    now_ns: int,
    state_path: pathlib.Path,
) -> Optional[Dict[str, Any]]:
    """Map one hook event to spans; return the state to persist (``None`` → don't persist)."""
    if event == "SessionStart":
        source = str(payload.get("source") or "")
        if (
            source in _CONTINUATION_SOURCES
            and state is not None
            and state.get("session_root")
        ):
            # Compaction/resume/fork reuse the session_id — keep the live session
            # (turn counter, pending tools, session root) intact; do NOT re-mint.
            return state
        return _new_session(tracer, session_id, project, now_ns)
    if event == "UserPromptSubmit":
        state = _ensure_session(tracer, state, session_id, project, now_ns)
        _start_turn(tracer, state, session_id, now_ns)
        return state
    if event == "PreToolUse":
        state = _ensure_session(tracer, state, session_id, project, now_ns)
        _emit_tool_start(tracer, state, payload, now_ns)
        return state
    if event in ("PostToolUse", "PostToolUseFailure"):
        state = _ensure_session(tracer, state, session_id, project, now_ns)
        _emit_tool_span(
            tracer, state, payload, now_ns, failed=(event == "PostToolUseFailure")
        )
        return state
    if event == "Stop":
        if state is not None:
            _close_turn(tracer, state, now_ns)
        return state
    if event == "SubagentStop":
        if state is not None:
            _emit_subagent(tracer, state, payload, now_ns)
        return state
    if event == "SessionEnd":
        if state is not None:
            _close_session(tracer, state, now_ns)
        _unlink_quiet(state_path)
        return None
    # Unknown / unhandled event → touch nothing new.
    return state


def _handle(payload: Dict[str, Any], now_ns: Optional[int] = None) -> None:
    """Core: resolve state + project, build the tracer, dispatch, persist, flush.

    The read-modify-write of the per-session state file runs under a per-session
    lock so concurrent hook processes (parallel tools) can't clobber each other's
    counters or pending-tool entries; the OTLP export (``_flush``) and the
    cross-session stale sweep run outside it.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    event = str(payload.get("hook_event_name") or "")
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    endpoint = _endpoint()
    if not endpoint:
        return

    state_path = _state_path(session_id)
    provider = None
    try:
        with _session_lock(session_id):
            state = _read_state(state_path)
            if state is not None and _is_stale(state, now_ns):
                # Abandoned: emit its overdue session summary + remove it BEFORE
                # re-minting, so the summary is never silently dropped.
                _expire_current(state, state_path, now_ns, endpoint)
                state = None

            project = _resolve_project(
                payload.get("cwd"), state.get("project") if state else None
            )
            tracer, provider = _build_tracer(endpoint, project)
            state = _dispatch(
                event, tracer, state, session_id, project, payload, now_ns, state_path
            )
            if state is not None:
                state["last_event_ns"] = now_ns
                try:
                    _write_state_atomic(state_path, state)
                except Exception as e:  # noqa: BLE001 - a write failure is non-fatal
                    _debug(e)
        if event in ("SessionStart", "SessionEnd"):
            _sweep_stale(session_id, now_ns, endpoint)
    except Exception as e:  # noqa: BLE001 - the emitter must never disturb the session
        _debug(e)
    finally:
        _flush(provider)


def main(argv: Optional[List[str]] = None) -> int:
    """Console entry: read the payload, emit spans, ALWAYS exit 0, print NOTHING to stdout."""
    try:
        if not _endpoint():
            return 0  # instant no-op — OpenTelemetry is never imported
        raw = sys.stdin.read()
        if not raw:
            return 0
        try:
            payload = json.loads(raw)
        except ValueError:
            return 0
        if isinstance(payload, dict):
            _handle(payload)
    except Exception as e:  # noqa: BLE001 - the emitter must never disturb the session
        _debug(e)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
