"""
Tests for the Claude Code hook emitter (``kestrel_obs_claude_hook`` /
``kestrel-obs-claude-hook`` console script).

Covers:
1. Instant no-op (and no OTel import) when no OTLP endpoint is configured.
2. ``main`` always returns 0 and prints NOTHING to stdout (even on garbage stdin).
3. SessionStart mints an AGENT session-marker root (session_id / agent_name / orchestrator).
4. The full cross-process flow: session ⊃ turn ⊃ tool ⊃ tool-start markers, one
   trace per turn, parented across separate invocations via the state file.
5. PreToolUse emits a ``(started)`` marker and records the start, so PostToolUse
   duration is backdated from it (never negative) and unclaimed starts are
   evicted by TTL/cap.
6. tool_response success/error detection + error truncation to 200 chars (privacy).
7. Stop → turn summary CHAIN; SessionEnd → session summary CHAIN + state cleanup.
8. Projects = repos: git remote URL parsing + project resolution/caching.
9. State file is atomic + corruption-tolerant (re-mints a session root).
10. Staleness expiry closes + removes abandoned sessions (summary emitted first).
11. PostToolUseFailure (failed tools are a separate event with top-level error/duration_ms).
12. SessionStart during compaction/resume/fork preserves the active session.
13. Parallel tools pair by tool_use_id; per-session lock serializes state writes.
14. Every tool ends in exactly one terminal span: completed / denied
    (``PermissionDenied``) / incomplete (turn-end reconciliation), the last two
    zero-duration and counted apart from executed tools (#84).
"""

from __future__ import annotations

import io
import json

import pytest

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import kestrel_obs_claude_hook as chook
from kestrel_feature_observability.tracing import KestrelTracer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _memory_build_tracer(exporter):
    """A drop-in for ``_build_tracer`` that exports to a shared in-memory exporter.

    Each call returns a *fresh* provider (as a real invocation would) but all
    providers share one exporter, so spans emitted across separate ``_handle``
    calls (separate "processes") are captured together — exactly what the
    cross-process state-file parenting has to reconstruct.
    """

    def _factory(endpoint, project):
        attrs = {"service.name": "claude-code"}
        if project:
            attrs["openinference.project.name"] = project
        provider = TracerProvider(resource=Resource.create(attrs))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = KestrelTracer(tracer=provider.get_tracer("test"))
        return tracer, provider

    return _factory


@pytest.fixture
def emitter(monkeypatch, tmp_path):
    """Enabled emitter wired to an in-memory exporter + a tmp state dir.

    Returns the shared ``InMemorySpanExporter``. Git is stubbed out so project
    resolution is deterministic (no repo probing during tests).
    """
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("KESTREL_OBS_CLAUDE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("KESTREL_OBSERVABILITY_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("KESTREL_OTEL_PROJECT", raising=False)
    # Prompt capture is opt-in; keep it off by default so tests are deterministic.
    monkeypatch.delenv("KESTREL_OTEL_CAPTURE_PROMPTS", raising=False)
    monkeypatch.delenv("KESTREL_OTEL_MAX_IO_CHARS", raising=False)
    monkeypatch.setattr(chook, "_build_tracer", _memory_build_tracer(exporter))
    monkeypatch.setattr(chook, "_git_remote_slug", lambda cwd: None)
    return exporter


def _payload(event, session_id="sess-abc", **overrides):
    p = {"hook_event_name": event, "session_id": session_id, "cwd": None}
    p.update(overrides)
    return p


def _by_name(spans):
    return {s.name: s for s in spans}


# ---------------------------------------------------------------------------
# 1. Instant no-op when unconfigured
# ---------------------------------------------------------------------------

class TestNoOpWhenUnconfigured:
    def test_endpoint_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        assert chook._endpoint() is None

    def test_handle_is_noop_without_endpoint(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.setenv("KESTREL_OBS_CLAUDE_STATE_DIR", str(tmp_path))
        called = {"build": False}
        monkeypatch.setattr(
            chook, "_build_tracer",
            lambda *a, **k: called.__setitem__("build", True) or (None, None),
        )
        chook._handle(_payload("SessionStart"), now_ns=1)
        assert called["build"] is False
        assert list(tmp_path.glob("*.json")) == []  # no state written

    def test_main_noop_never_builds_tracer(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.setattr(chook.sys, "stdin", io.StringIO(json.dumps(_payload("PreToolUse"))))
        built = []
        monkeypatch.setattr(chook, "_build_tracer", lambda *a, **k: built.append(1) or (None, None))
        assert chook.main() == 0
        assert built == []


# ---------------------------------------------------------------------------
# 2. main() contract: exit 0, no stdout
# ---------------------------------------------------------------------------

class TestMainContract:
    def test_main_returns_zero_and_prints_nothing(self, emitter, monkeypatch, capsys):
        monkeypatch.setattr(chook.sys, "stdin", io.StringIO(json.dumps(_payload("SessionStart"))))
        rc = chook.main()
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""  # NOTHING to stdout (Claude interprets it)

    def test_main_survives_garbage_stdin(self, emitter, monkeypatch, capsys):
        monkeypatch.setattr(chook.sys, "stdin", io.StringIO("}{ not json"))
        assert chook.main() == 0
        assert capsys.readouterr().out == ""

    def test_main_survives_empty_stdin(self, emitter, monkeypatch):
        monkeypatch.setattr(chook.sys, "stdin", io.StringIO(""))
        assert chook.main() == 0

    def test_missing_session_id_is_noop(self, emitter):
        chook._handle({"hook_event_name": "SessionStart"}, now_ns=1)
        assert emitter.get_finished_spans() == ()


# ---------------------------------------------------------------------------
# 3. SessionStart → AGENT session-marker root
# ---------------------------------------------------------------------------

class TestSessionStart:
    def test_emits_agent_root_with_identity(self, emitter):
        chook._handle(_payload("SessionStart"), now_ns=1_000)
        spans = emitter.get_finished_spans()
        assert len(spans) == 1
        root = spans[0]
        assert root.name == "claude-code"
        assert root.attributes["openinference.span.kind"] == "AGENT"
        assert root.attributes[chook.KESTREL_SESSION_ID] == "sess-abc"
        assert root.attributes["kestrel.agent_name"] == "claude-code"
        assert root.attributes["kestrel.orchestrator"] == "Direct"

    def test_orchestrator_from_env(self, emitter, monkeypatch):
        monkeypatch.setenv("KESTREL_OBSERVABILITY_ORCHESTRATOR", "talon")
        chook._handle(_payload("SessionStart"), now_ns=1)
        root = emitter.get_finished_spans()[0]
        assert root.attributes["kestrel.orchestrator"] == "talon"

    def test_root_is_its_own_trace(self, emitter):
        chook._handle(_payload("SessionStart"), now_ns=1)
        root = emitter.get_finished_spans()[0]
        assert root.parent is None  # a fresh trace root


# ---------------------------------------------------------------------------
# 4. Full cross-process flow (session ⊃ turn ⊃ tool ⊃ marker)
# ---------------------------------------------------------------------------

class TestFullFlow:
    def _run_flow(self):
        ns = 1_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1_000)
        chook._handle(_payload("PreToolUse", tool_name="Bash"), now_ns=ns + 2_000_000)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={"stdout": "ok"}),
            now_ns=ns + 5_000_000,
        )
        chook._handle(
            _payload("SubagentStop", agent_type="Explore"),
            now_ns=ns + 7_000_000,
        )
        chook._handle(_payload("Stop"), now_ns=ns + 9_000_000)
        chook._handle(_payload("SessionEnd", reason="other"), now_ns=ns + 10_000_000)

    def test_span_set_and_kinds(self, emitter):
        self._run_flow()
        spans = _by_name(emitter.get_finished_spans())
        assert set(spans) == {
            "claude-code",
            "claude-code turn 1",
            "Bash (started)",
            "Bash",
            "Explore",
            "turn 1 summary",
            "session summary",
        }
        assert spans["claude-code"].attributes["openinference.span.kind"] == "AGENT"
        assert spans["claude-code turn 1"].attributes["openinference.span.kind"] == "AGENT"
        assert spans["Bash (started)"].attributes["openinference.span.kind"] == "TOOL"
        assert spans["Bash"].attributes["openinference.span.kind"] == "TOOL"
        assert spans["Explore"].attributes["openinference.span.kind"] == "AGENT"
        assert spans["turn 1 summary"].attributes["openinference.span.kind"] == "CHAIN"
        assert spans["session summary"].attributes["openinference.span.kind"] == "CHAIN"

    def test_one_trace_per_turn_and_parenting(self, emitter):
        self._run_flow()
        spans = _by_name(emitter.get_finished_spans())
        turn = spans["claude-code turn 1"]
        tool = spans["Bash"]
        marker = spans["Bash (started)"]
        summary = spans["turn 1 summary"]

        # The tool span, its start marker and the turn summary all share the
        # turn's trace and parent to the turn root — reconstructed across
        # separate invocations purely from the state file.
        for s in (tool, marker, summary):
            assert s.context.trace_id == turn.context.trace_id
            assert s.parent.span_id == turn.context.span_id

        # A turn root is a NEW trace (distinct from the session-marker root).
        assert turn.context.trace_id != spans["claude-code"].context.trace_id

    def test_session_ids_stamped_on_every_span_shape(self, emitter):
        self._run_flow()
        spans = _by_name(emitter.get_finished_spans())
        assert chook.OPENINFERENCE_SESSION_ID == "session.id"
        for span in spans.values():
            assert span.attributes[chook.KESTREL_SESSION_ID] == "sess-abc"
            assert (
                span.attributes["session.id"]
                == span.attributes[chook.KESTREL_SESSION_ID]
            )

        tool = spans["Bash"]
        assert tool.attributes[chook.KESTREL_TURN_ID] == "sess-abc#1"
        assert tool.attributes[chook.KESTREL_TURN_INDEX] == 1

    def test_markers_flag_start(self, emitter):
        # The turn root and each tool-start marker are live-visibility markers;
        # the terminal tool span that closes the marker never is (#84).
        self._run_flow()
        spans = _by_name(emitter.get_finished_spans())
        assert spans["claude-code turn 1"].attributes[chook.KESTREL_MARKER] == "start"
        assert spans["Bash (started)"].attributes[chook.KESTREL_MARKER] == "start"
        assert chook.KESTREL_MARKER not in spans["Bash"].attributes

    def test_summaries_carry_totals(self, emitter):
        self._run_flow()
        spans = _by_name(emitter.get_finished_spans())
        turn_summary = spans["turn 1 summary"]
        assert turn_summary.attributes["kestrel.tool_count"] == 1
        assert turn_summary.attributes["kestrel.success_ratio"] == 1.0
        session_summary = spans["session summary"]
        assert session_summary.attributes["kestrel.turn_count"] == 1
        assert session_summary.attributes["kestrel.tool_count"] == 1

    def test_session_end_removes_state_file(self, emitter, tmp_path):
        self._run_flow()
        assert list(tmp_path.glob("*.json")) == []

    def test_second_turn_is_new_trace(self, emitter):
        ns = 2_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("Stop"), now_ns=ns + 2)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 3)
        spans = _by_name(emitter.get_finished_spans())
        t1 = spans["claude-code turn 1"]
        t2 = spans["claude-code turn 2"]
        assert t1.context.trace_id != t2.context.trace_id
        assert t2.attributes[chook.KESTREL_TURN_ID] == "sess-abc#2"


# ---------------------------------------------------------------------------
# 5. PostToolUse duration backdating
# ---------------------------------------------------------------------------

class TestToolDuration:
    def test_duration_backdated_from_pretool(self, emitter):
        ns = 3_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash"), now_ns=ns + 1_000_000)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={"stdout": "ok"}),
            now_ns=ns + 4_000_000,
        )
        tool = _by_name(emitter.get_finished_spans())["Bash"]
        # 3ms real gap between Pre and Post → real duration, real backdated start.
        assert tool.attributes["tool.duration_ms"] == pytest.approx(3.0)
        assert tool.end_time - tool.start_time == 3_000_000

    def test_no_pretool_is_zero_duration_never_negative(self, emitter):
        ns = 4_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={"stdout": "ok"}),
            now_ns=ns + 2,
        )
        tool = _by_name(emitter.get_finished_spans())["Bash"]
        assert tool.end_time >= tool.start_time  # never negative
        assert "tool.duration_ms" not in tool.attributes


# ---------------------------------------------------------------------------
# 6. Success / error detection + truncation
# ---------------------------------------------------------------------------

class TestToolOutcome:
    @pytest.mark.parametrize(
        "resp,expected",
        [
            ({"stdout": "ok"}, True),
            ({"success": True}, True),
            ({"success": False}, False),
            ({"is_error": True}, False),
            ({"error": "boom"}, False),
            ({"status": "error"}, False),
            ("plain string output", True),
            (None, True),
        ],
    )
    def test_tool_success(self, resp, expected):
        assert chook._tool_success(resp) is expected

    def test_error_truncated_to_200_chars(self, emitter):
        ns = 5_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload(
                "PostToolUse",
                tool_name="Bash",
                tool_response={"error": "x" * 500},
            ),
            now_ns=ns + 2,
        )
        tool = _by_name(emitter.get_finished_spans())["Bash"]
        assert tool.attributes["tool.success"] is False
        assert len(tool.attributes["tool.error"]) == 200

    def test_prompt_content_never_recorded(self, emitter):
        # Privacy: UserPromptSubmit carries the user message, which must NOT land
        # on any span attribute.
        ns = 6_000_000_000
        secret = "my secret prompt text"
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit", prompt=secret), now_ns=ns + 1)
        for span in emitter.get_finished_spans():
            for value in span.attributes.values():
                assert secret not in str(value)


# ---------------------------------------------------------------------------
# 7. Project resolution (projects = repos)
# ---------------------------------------------------------------------------

class TestProjectResolution:
    @pytest.mark.parametrize(
        "url,slug",
        [
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("ssh://git@github.com/owner/repo.git", "owner/repo"),
            ("https://gitlab.com/group/sub/repo.git", "sub/repo"),
            ("", None),
            ("not-a-url", None),
        ],
    )
    def test_slug_from_remote_url(self, url, slug):
        assert chook._slug_from_remote_url(url) == slug

    def test_resolve_prefers_cache(self, monkeypatch):
        monkeypatch.setattr(chook, "_git_remote_slug", lambda cwd: "from/git")
        assert chook._resolve_project("/some/cwd", "cached/repo") == "cached/repo"

    def test_resolve_uses_git_then_env(self, monkeypatch):
        monkeypatch.setattr(chook, "_git_remote_slug", lambda cwd: "from/git")
        monkeypatch.setenv("KESTREL_OTEL_PROJECT", "from-env")
        assert chook._resolve_project("/cwd", None) == "from/git"

    def test_resolve_falls_back_to_env_then_none(self, monkeypatch):
        monkeypatch.setattr(chook, "_git_remote_slug", lambda cwd: None)
        monkeypatch.setenv("KESTREL_OTEL_PROJECT", "from-env")
        assert chook._resolve_project("/cwd", None) == "from-env"
        monkeypatch.delenv("KESTREL_OTEL_PROJECT")
        assert chook._resolve_project("/cwd", None) is None

    def test_project_stamped_as_resource_and_cached(self, emitter, monkeypatch):
        calls = {"n": 0}

        def _slug(cwd):
            calls["n"] += 1
            return "acme/widgets"

        monkeypatch.setattr(chook, "_git_remote_slug", _slug)
        chook._handle(_payload("SessionStart", cwd="/repo"), now_ns=1)
        chook._handle(_payload("UserPromptSubmit", cwd="/repo"), now_ns=2)
        # git probed once; the resolved slug is cached in the state file.
        assert calls["n"] == 1
        root = _by_name(emitter.get_finished_spans())["claude-code"]
        assert root.resource.attributes["openinference.project.name"] == "acme/widgets"


# ---------------------------------------------------------------------------
# 8. State file: atomic + corruption-tolerant
# ---------------------------------------------------------------------------

class TestStateFile:
    def test_written_atomically_and_readable(self, emitter, tmp_path):
        chook._handle(_payload("SessionStart"), now_ns=1)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["session_id"] == "sess-abc"
        assert "session_root" in data
        assert data["session_root"]["trace_id"]

    def test_corrupt_state_is_re_minted(self, emitter, tmp_path):
        path = chook._state_path("sess-abc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is corrupt")
        # A PostToolUse against a corrupt file must not crash — it re-mints a
        # session root and still records the tool span.
        chook._handle(_payload("UserPromptSubmit"), now_ns=1)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={"stdout": "ok"}),
            now_ns=2,
        )
        spans = _by_name(emitter.get_finished_spans())
        assert "claude-code" in spans  # re-minted root
        assert "Bash" in spans


# ---------------------------------------------------------------------------
# 9. Staleness expiry
# ---------------------------------------------------------------------------

class TestStaleness:
    def test_stale_session_closed_and_removed(self, emitter, tmp_path, monkeypatch):
        monkeypatch.setenv("KESTREL_OBS_CLAUDE_SESSION_TTL", "1")  # 1 second TTL
        old_ns = 1_000_000_000_000
        # An abandoned session, last seen long ago.
        chook._handle(_payload("SessionStart", session_id="old-sess"), now_ns=old_ns)
        emitter.clear()

        # A new session starts much later → the sweep closes the abandoned one.
        new_ns = old_ns + 5_000_000_000  # +5s, past the 1s TTL
        chook._handle(_payload("SessionStart", session_id="new-sess"), now_ns=new_ns)

        names = [s.name for s in emitter.get_finished_spans()]
        assert "session summary" in names  # the stale session was summarized
        # The abandoned file is gone; the fresh one remains.
        remaining = {p.name for p in tmp_path.glob("*.json")}
        assert chook._state_filename("old-sess") not in remaining
        assert chook._state_filename("new-sess") in remaining

    def test_own_stale_state_re_minted(self, emitter, monkeypatch):
        monkeypatch.setenv("KESTREL_OBS_CLAUDE_SESSION_TTL", "1")
        chook._handle(_payload("SessionStart"), now_ns=1_000_000_000_000)
        emitter.clear()
        # Same session id, but way past the TTL → the abandoned session is
        # summarized + removed BEFORE a fresh root is minted (never dropped).
        chook._handle(_payload("PreToolUse", tool_name="Bash"), now_ns=1_000_000_000_000 + 10_000_000_000)
        names = [s.name for s in emitter.get_finished_spans()]
        assert "session summary" in names  # the abandoned session got its overdue summary
        assert "claude-code" in names  # and a new session-marker root was minted

    def test_expiry_summary_aggregates_prior_turns(self, emitter, tmp_path, monkeypatch):
        # A real session with a completed tool, then abandoned: its overdue
        # summary must carry the accumulated totals, not a bare re-mint.
        monkeypatch.setenv("KESTREL_OBS_CLAUDE_SESSION_TTL", "1")
        ns = 1_000_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="t"), now_ns=ns + 2)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="t", tool_response={"stdout": "ok"}),
            now_ns=ns + 3,
        )
        emitter.clear()
        # Way past the TTL, a new event on the same session id.
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 10_000_000_000)
        summary = _by_name(emitter.get_finished_spans())["session summary"]
        assert summary.attributes["kestrel.turn_count"] == 1
        assert summary.attributes["kestrel.tool_count"] == 1
        # The abandoned file was replaced by the re-minted session's file.
        assert list(tmp_path.glob("*.json"))  # a fresh state file exists


# ---------------------------------------------------------------------------
# 10. PostToolUseFailure (failed tools are a separate event)
# ---------------------------------------------------------------------------

class TestFailedTool:
    def test_failure_event_forces_failure_and_decodes_toplevel_fields(self, emitter):
        # PostToolUseFailure reports the outcome out-of-band: top-level `error`
        # and `duration_ms`, and NO `tool_response`.
        ns = 7_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PreToolUse", tool_name="Bash", tool_use_id="tu-1"),
            now_ns=ns + 1_000_000,
        )
        chook._handle(
            _payload(
                "PostToolUseFailure",
                tool_name="Bash",
                tool_use_id="tu-1",
                error="command failed: boom",
                duration_ms=42,
            ),
            now_ns=ns + 9_000_000,
        )
        tool = _by_name(emitter.get_finished_spans())["Bash"]
        assert tool.attributes["tool.success"] is False
        assert tool.attributes["tool.error"] == "command failed: boom"
        # The payload's own duration_ms is authoritative.
        assert tool.attributes["tool.duration_ms"] == pytest.approx(42.0)
        assert tool.end_time - tool.start_time == 42_000_000

    def test_failure_without_duration_backdates_from_pretool(self, emitter):
        ns = 7_500_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PreToolUse", tool_name="Edit", tool_use_id="e1"),
            now_ns=ns + 2_000_000,
        )
        chook._handle(
            _payload("PostToolUseFailure", tool_name="Edit", tool_use_id="e1", error="nope"),
            now_ns=ns + 5_000_000,
        )
        tool = _by_name(emitter.get_finished_spans())["Edit"]
        assert tool.attributes["tool.success"] is False
        assert tool.attributes["tool.duration_ms"] == pytest.approx(3.0)

    def test_failure_error_truncated_to_200_chars(self, emitter):
        ns = 7_800_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PostToolUseFailure", tool_name="Bash", error="x" * 500),
            now_ns=ns + 2,
        )
        tool = _by_name(emitter.get_finished_spans())["Bash"]
        assert len(tool.attributes["tool.error"]) == 200

    def test_failure_drags_down_turn_success_ratio(self, emitter):
        ns = 7_900_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={"stdout": "ok"}),
            now_ns=ns + 2,
        )
        chook._handle(
            _payload("PostToolUseFailure", tool_name="Bash", error="boom"),
            now_ns=ns + 3,
        )
        chook._handle(_payload("Stop"), now_ns=ns + 4)
        summary = _by_name(emitter.get_finished_spans())["turn 1 summary"]
        assert summary.attributes["kestrel.tool_count"] == 2
        assert summary.attributes["kestrel.success_ratio"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 11. SessionStart during compaction preserves the active session
# ---------------------------------------------------------------------------

class TestCompaction:
    def test_compact_preserves_active_session(self, emitter):
        ns = 8_000_000_000
        chook._handle(_payload("SessionStart", source="startup"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="b1"), now_ns=ns + 2)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="b1", tool_response={"stdout": "ok"}),
            now_ns=ns + 3,
        )
        chook._handle(_payload("Stop"), now_ns=ns + 4)
        # Compaction fires SessionStart(source=compact) mid-session.
        chook._handle(_payload("SessionStart", source="compact"), now_ns=ns + 5)
        # The next turn must be #2 — NOT a duplicate #1.
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 6)

        spans = _by_name(emitter.get_finished_spans())
        assert "claude-code turn 2" in spans
        assert spans["claude-code turn 2"].attributes[chook.KESTREL_TURN_ID] == "sess-abc#2"
        assert spans["claude-code turn 2"].attributes[chook.KESTREL_SESSION_ID] == "sess-abc"
        # Exactly ONE session-marker root — compaction did NOT mint a second.
        roots = [s for s in emitter.get_finished_spans() if s.name == "claude-code"]
        assert len(roots) == 1

    @pytest.mark.parametrize("source", ["resume", "fork"])
    def test_resume_and_fork_also_preserve(self, emitter, source):
        ns = 8_200_000_000
        chook._handle(_payload("SessionStart", source="startup"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("SessionStart", source=source), now_ns=ns + 2)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 3)
        spans = _by_name(emitter.get_finished_spans())
        assert "claude-code turn 2" in spans
        assert len([s for s in emitter.get_finished_spans() if s.name == "claude-code"]) == 1

    def test_compact_without_prior_state_mints(self, emitter):
        # No usable state (e.g. resumed on a fresh host) → a compact/resume start
        # still mints a session root rather than silently doing nothing.
        chook._handle(_payload("SessionStart", source="compact"), now_ns=8_400_000_000)
        roots = [s for s in emitter.get_finished_spans() if s.name == "claude-code"]
        assert len(roots) == 1


# ---------------------------------------------------------------------------
# 12. Parallel tools: pair by tool_use_id, serialize under a lock
# ---------------------------------------------------------------------------

class TestParallelTools:
    def test_tool_use_id_pairs_concurrent_same_tool(self, emitter):
        # Two concurrent Bash calls; PostToolUse for the FIRST-started arrives
        # first. Each must pair to ITS OWN PreToolUse start via tool_use_id — a
        # LIFO-by-name pairing would swap their start times.
        ns = 9_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="A"), now_ns=ns + 1_000_000)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="B"), now_ns=ns + 2_000_000)
        # Post A before Post B → LIFO would pop B's start for A (wrong).
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="A", tool_response={"stdout": "a"}),
            now_ns=ns + 4_000_000,
        )
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="B", tool_response={"stdout": "b"}),
            now_ns=ns + 6_000_000,
        )
        durations = sorted(
            s.attributes["tool.duration_ms"]
            for s in emitter.get_finished_spans()
            if s.name == "Bash"
        )
        # A: 4ms - 1ms = 3ms; B: 6ms - 2ms = 4ms. LIFO-by-name would give 2/5ms.
        assert durations == pytest.approx([3.0, 4.0])

    def test_tool_use_id_stamped_as_call_id_on_marker_and_span(self, emitter):
        # The Timeline pairs concurrent same-name tools one-to-one by tool.call_id,
        # so BOTH the "(started)" marker and the completed span must carry it (#62).
        ns = 9_500_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="tc-1"), now_ns=ns + 2)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="tc-1", tool_response={"stdout": "ok"}),
            now_ns=ns + 3,
        )
        by_name = _by_name(emitter.get_finished_spans())
        assert by_name["Bash (started)"].attributes["tool.call_id"] == "tc-1"
        assert by_name["Bash"].attributes["tool.call_id"] == "tc-1"
        assert by_name["Bash"].attributes["tool.name"] == "Bash"

    def test_missing_tool_use_id_omits_call_id(self, emitter):
        # No id (the in-process emitter path shape) → no tool.call_id stamped; the
        # Timeline falls back to name-order pairing.
        ns = 9_700_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash"), now_ns=ns + 2)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={"stdout": "ok"}),
            now_ns=ns + 3,
        )
        by_name = _by_name(emitter.get_finished_spans())
        assert "tool.call_id" not in by_name["Bash (started)"].attributes
        assert "tool.call_id" not in by_name["Bash"].attributes

    def test_session_lock_is_clean_and_counts_accumulate(self, emitter):
        # The per-session lock must acquire + release cleanly (sequential events
        # unaffected, counters accumulate across separate invocations).
        with chook._session_lock("sess-lock"):
            pass
        with chook._session_lock("sess-lock"):
            pass
        ns = 10_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="x1"), now_ns=ns + 2)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="x1", tool_response={"stdout": "ok"}),
            now_ns=ns + 3,
        )
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="x2"), now_ns=ns + 4)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="x2", tool_response={"stdout": "ok"}),
            now_ns=ns + 5,
        )
        chook._handle(_payload("Stop"), now_ns=ns + 6)
        summary = _by_name(emitter.get_finished_spans())["turn 1 summary"]
        assert summary.attributes["kestrel.tool_count"] == 2


# ---------------------------------------------------------------------------
# 13. Turn-root prompt capture (opt-in) + complete summary stats (#63)
# ---------------------------------------------------------------------------

class TestTurnPromptCapture:
    def test_prompt_not_captured_by_default(self, emitter):
        ns = 11_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit", prompt="hello world"), now_ns=ns + 1)
        turn = _by_name(emitter.get_finished_spans())["claude-code turn 1"]
        assert "input.value" not in turn.attributes

    def test_prompt_captured_when_opted_in(self, emitter, monkeypatch):
        monkeypatch.setenv("KESTREL_OTEL_CAPTURE_PROMPTS", "1")
        ns = 11_100_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit", prompt="hello world"), now_ns=ns + 1)
        turn = _by_name(emitter.get_finished_spans())["claude-code turn 1"]
        assert turn.attributes["input.value"] == "hello world"

    def test_prompt_truncated_to_env_cap(self, emitter, monkeypatch):
        monkeypatch.setenv("KESTREL_OTEL_CAPTURE_PROMPTS", "1")
        monkeypatch.setenv("KESTREL_OTEL_MAX_IO_CHARS", "5")
        ns = 11_200_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit", prompt="x" * 40), now_ns=ns + 1)
        turn = _by_name(emitter.get_finished_spans())["claude-code turn 1"]
        assert turn.attributes["input.value"] == "x" * 5

    def test_prompt_default_cap_is_20000(self, emitter, monkeypatch):
        monkeypatch.setenv("KESTREL_OTEL_CAPTURE_PROMPTS", "1")
        ns = 11_300_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit", prompt="x" * 25_000), now_ns=ns + 1)
        turn = _by_name(emitter.get_finished_spans())["claude-code turn 1"]
        assert len(turn.attributes["input.value"]) == 20_000


class TestSummaryStats:
    def test_summaries_carry_error_count_and_duration(self, emitter):
        ns = 11_400_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PostToolUse", tool_name="a", tool_response={"stdout": "ok"}),
            now_ns=ns + 2,
        )
        chook._handle(
            _payload("PostToolUseFailure", tool_name="b", error="boom"), now_ns=ns + 3
        )
        chook._handle(_payload("Stop"), now_ns=ns + 4)
        chook._handle(_payload("SessionEnd"), now_ns=ns + 5)
        spans = _by_name(emitter.get_finished_spans())
        turn = spans["turn 1 summary"]
        assert turn.attributes["kestrel.tool_count"] == 2
        assert turn.attributes["kestrel.error_count"] == 1
        assert (
            turn.attributes["kestrel.duration_ms"]
            == turn.attributes["kestrel.turn_duration_ms"]
        )
        sess = spans["session summary"]
        assert sess.attributes["kestrel.tool_count"] == 2
        assert sess.attributes["kestrel.error_count"] == 1
        assert (
            sess.attributes["kestrel.duration_ms"]
            == sess.attributes["kestrel.session_duration_ms"]
        )


# ---------------------------------------------------------------------------
# 14. Three tool outcomes; pending starts stay bounded (#84)
# ---------------------------------------------------------------------------

def _pending(tmp_path, session_id="sess-abc"):
    """The persisted ``pending_tools`` map for a session."""
    state = chook._read_state(tmp_path / chook._state_filename(session_id))
    return (state or {}).get("pending_tools") or {}


def _terminal(spans, tool="Bash"):
    """The terminal (non-marker) spans for a tool — its completed/denied/incomplete twins."""
    return [s for s in spans if s.name == tool]


class TestToolOutcomes:
    def test_pre_tool_use_emits_a_start_marker(self, emitter):
        # PreToolUse fires BEFORE the permission check, so even a to-be-denied
        # tool is visible the moment it is attempted (#84 reverts #82's drop).
        ns = 12_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        emitter.clear()
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="p1"), now_ns=ns + 2)
        marker = _by_name(emitter.get_finished_spans())["Bash (started)"]
        assert marker.attributes[chook.KESTREL_MARKER] == "start"
        assert marker.end_time == marker.start_time

    def test_completed_tool_stamps_the_completed_outcome(self, emitter):
        ns = 12_200_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="o1"), now_ns=ns + 1_000_000)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="o1", tool_response={"stdout": "ok"}),
            now_ns=ns + 4_000_000,
        )
        terminal = _terminal(emitter.get_finished_spans())
        assert len(terminal) == 1
        assert terminal[0].attributes[chook.KESTREL_TOOL_OUTCOME] == "completed"
        # One span covering the REAL start→end, not an instant point.
        assert terminal[0].end_time - terminal[0].start_time == 3_000_000

    def test_permission_denied_emits_a_terminal_denied_span(self, emitter):
        # The classifier refusing a call is an explicit, emittable signal: pair it
        # to the marker by tool_use_id and label it `denied` right away.
        ns = 12_050_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="d1"), now_ns=ns + 2_000_000)
        chook._handle(
            _payload("PermissionDenied", tool_name="Bash", tool_use_id="d1"),
            now_ns=ns + 5_000_000,
        )
        spans = _by_name(emitter.get_finished_spans())
        denied, marker = spans["Bash"], spans["Bash (started)"]
        assert denied.attributes[chook.KESTREL_TOOL_OUTCOME] == "denied"
        assert denied.attributes[chook.KESTREL_TOOL_DENIED_BY] == "classifier"
        assert denied.attributes["tool.success"] is False
        assert denied.attributes["tool.call_id"] == "d1"
        # It never RAN: a zero-duration point at the recorded start, never a bar
        # spanning start→now (the phantom-duration failure this shape avoids).
        assert denied.start_time == denied.end_time == marker.start_time
        # Paired to the turn its marker belongs to, in the same trace.
        assert denied.parent.span_id == spans["claude-code turn 1"].context.span_id

    def test_denied_span_survives_a_missing_start(self, emitter):
        # The marker may have been pruned (or predate this release): the denial
        # still emits, anchored at now, rather than being silently dropped.
        ns = 12_060_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(
            _payload("PermissionDenied", tool_name="Bash", tool_use_id="gone"),
            now_ns=ns + 5_000_000,
        )
        denied = _terminal(emitter.get_finished_spans())[0]
        assert denied.attributes[chook.KESTREL_TOOL_OUTCOME] == "denied"
        assert denied.start_time == denied.end_time == ns + 5_000_000

    def test_stop_reconciles_an_unresolved_tool_as_incomplete(self, emitter):
        # A guard/permission-rule denial fires NO event at all — the backbone is
        # turn-end reconciliation, which must stand alone (#84).
        ns = 12_100_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="i1"), now_ns=ns + 2_000_000)
        chook._handle(_payload("Stop"), now_ns=ns + 9_000_000)
        spans = _by_name(emitter.get_finished_spans())
        incomplete, marker = spans["Bash"], spans["Bash (started)"]
        assert incomplete.attributes[chook.KESTREL_TOOL_OUTCOME] == "incomplete"
        assert incomplete.attributes["tool.success"] is False
        assert chook.KESTREL_TOOL_DENIED_BY not in incomplete.attributes
        # Zero-duration at the start, NOT start→Stop: it never ran.
        assert incomplete.start_time == incomplete.end_time == marker.start_time
        assert incomplete.parent.span_id == spans["claude-code turn 1"].context.span_id

    def test_refusals_are_counted_apart_from_executed_tools(self, emitter):
        # A refusal is not an agent error: tool_count/success_ratio stay over
        # EXECUTED tools, and the refusals get their own additive dimensions.
        ns = 12_150_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Read", tool_use_id="ok"), now_ns=ns + 1_000_000)
        chook._handle(
            _payload("PostToolUse", tool_name="Read", tool_use_id="ok", tool_response={}),
            now_ns=ns + 2_000_000,
        )
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="dn"), now_ns=ns + 3_000_000)
        chook._handle(
            _payload("PermissionDenied", tool_name="Bash", tool_use_id="dn"),
            now_ns=ns + 4_000_000,
        )
        chook._handle(_payload("PreToolUse", tool_name="Edit", tool_use_id="ic"), now_ns=ns + 5_000_000)
        chook._handle(_payload("Stop"), now_ns=ns + 6_000_000)
        chook._handle(_payload("SessionEnd", reason="other"), now_ns=ns + 7_000_000)
        spans = _by_name(emitter.get_finished_spans())
        for summary in (spans["turn 1 summary"], spans["session summary"]):
            assert summary.attributes["kestrel.tool_count"] == 1
            assert summary.attributes["kestrel.error_count"] == 0
            assert summary.attributes["kestrel.success_ratio"] == 1.0
            assert summary.attributes[chook.KESTREL_DENIED_COUNT] == 1
            assert summary.attributes[chook.KESTREL_INCOMPLETE_COUNT] == 1

    def test_reconciliation_runs_before_the_turn_summary(self, emitter):
        # Both fire on Stop, so the ordering silently decides whether the count
        # lands: the summary must already see the incomplete tool.
        ns = 12_160_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="r1"), now_ns=ns + 2)
        chook._handle(_payload("Stop"), now_ns=ns + 3_000_000)
        summary = _by_name(emitter.get_finished_spans())["turn 1 summary"]
        assert summary.attributes[chook.KESTREL_INCOMPLETE_COUNT] == 1

    def test_interrupted_turn_reconciles_into_its_own_turn(self, emitter):
        # An interrupt fires no Stop, so leftovers are reconciled at the next
        # UserPromptSubmit — into the ORIGINAL turn, never the new one, and the
        # stranded turn gets its retroactive summary so it is closed exactly once.
        ns = 12_170_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="k1"), now_ns=ns + 2_000_000)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 9_000_000)  # interrupted
        spans = _by_name(emitter.get_finished_spans())
        incomplete = spans["Bash"]
        turn1, turn2 = spans["claude-code turn 1"], spans["claude-code turn 2"]
        assert incomplete.attributes[chook.KESTREL_TOOL_OUTCOME] == "incomplete"
        assert incomplete.parent.span_id == turn1.context.span_id
        assert incomplete.context.trace_id == turn1.context.trace_id
        assert incomplete.context.trace_id != turn2.context.trace_id
        assert incomplete.attributes[chook.KESTREL_TURN_ID] == "sess-abc#1"
        # The interrupted turn is summarized retroactively — closing its band in
        # its OWN trace, no later than the turn that supersedes it, instead of
        # being left open to the abandoned-cap.
        summary = spans["turn 1 summary"]
        assert summary.attributes[chook.KESTREL_INCOMPLETE_COUNT] == 1
        assert summary.parent.span_id == turn1.context.span_id
        assert summary.context.trace_id == turn1.context.trace_id
        assert summary.end_time <= turn2.start_time

    def test_every_turn_is_summarized_exactly_once(self, emitter):
        ns = 12_180_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="s1"), now_ns=ns + 2)
        chook._handle(_payload("Stop"), now_ns=ns + 3_000_000)
        chook._handle(_payload("SessionEnd", reason="other"), now_ns=ns + 4_000_000)
        names = [s.name for s in emitter.get_finished_spans()]
        assert names.count("turn 1 summary") == 1
        assert names.count("Bash") == 1  # exactly one terminal span for the tool

    def test_session_end_mid_turn_leaves_nothing_unrepresented(self, emitter):
        ns = 12_190_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="e1"), now_ns=ns + 2)
        chook._handle(_payload("SessionEnd", reason="other"), now_ns=ns + 5_000_000)
        spans = _by_name(emitter.get_finished_spans())
        assert spans["Bash"].attributes[chook.KESTREL_TOOL_OUTCOME] == "incomplete"
        assert spans["turn 1 summary"].attributes[chook.KESTREL_INCOMPLETE_COUNT] == 1
        assert spans["session summary"].attributes[chook.KESTREL_INCOMPLETE_COUNT] == 1

    def test_parallel_same_name_tools_reconcile_independently(self, emitter):
        # tool_use_id keying means the completed twin claims only ITS start; the
        # other stays pending and reconciles as its own incomplete span.
        ns = 12_195_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="a"), now_ns=ns + 1_000_000)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="b"), now_ns=ns + 2_000_000)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="a", tool_response={}),
            now_ns=ns + 4_000_000,
        )
        chook._handle(_payload("Stop"), now_ns=ns + 8_000_000)
        outcomes = {
            s.attributes["tool.call_id"]: s.attributes[chook.KESTREL_TOOL_OUTCOME]
            for s in _terminal(emitter.get_finished_spans())
        }
        assert outcomes == {"a": "completed", "b": "incomplete"}

    def test_late_completion_never_adds_a_second_terminal_span(self, emitter):
        # Hook events are separate processes: the per-session lock serializes
        # writes but guarantees nothing about ORDER, so a PostToolUse can land
        # after Stop already reconciled its call. One call gets ONE terminal span
        # — an exported OTel span cannot be re-labeled incomplete → completed, so
        # the span already exported wins and the late duplicate is dropped.
        ns = 12_196_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="L1"), now_ns=ns + 1_000_000)
        chook._handle(_payload("Stop"), now_ns=ns + 5_000_000)  # reconciles → incomplete
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="L1", tool_response={}),
            now_ns=ns + 6_000_000,
        )
        terminal = _terminal(emitter.get_finished_spans())
        assert [s.attributes[chook.KESTREL_TOOL_OUTCOME] for s in terminal] == ["incomplete"]
        # ...and it is not counted a second time in the session totals either.
        chook._handle(_payload("SessionEnd", reason="other"), now_ns=ns + 7_000_000)
        summary = _by_name(emitter.get_finished_spans())["session summary"]
        assert summary.attributes["kestrel.tool_count"] == 0
        assert summary.attributes[chook.KESTREL_INCOMPLETE_COUNT] == 1

    def test_late_failure_after_a_denial_never_adds_a_second_span(self, emitter):
        # Same barrier, the other terminal event and the other outcome.
        ns = 12_197_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="L2"), now_ns=ns + 1_000_000)
        chook._handle(
            _payload("PermissionDenied", tool_name="Bash", tool_use_id="L2"),
            now_ns=ns + 2_000_000,
        )
        chook._handle(
            _payload("PostToolUseFailure", tool_name="Bash", tool_use_id="L2", error="boom"),
            now_ns=ns + 3_000_000,
        )
        terminal = _terminal(emitter.get_finished_spans())
        assert [s.attributes[chook.KESTREL_TOOL_OUTCOME] for s in terminal] == ["denied"]

    def test_repeated_permission_denied_emits_one_span(self, emitter):
        ns = 12_198_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="L3"), now_ns=ns + 1_000_000)
        for offset in (2_000_000, 3_000_000):
            chook._handle(
                _payload("PermissionDenied", tool_name="Bash", tool_use_id="L3"),
                now_ns=ns + offset,
            )
        assert len(_terminal(emitter.get_finished_spans())) == 1

    def test_tombstone_absorbs_only_one_late_completion(self, emitter):
        # Claiming CONSUMES the tombstone, so a genuinely new call with the same
        # tool_use_id-less name still emits its own span afterwards.
        ns = 12_199_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash"), now_ns=ns + 1_000_000)
        chook._handle(_payload("Stop"), now_ns=ns + 2_000_000)  # → incomplete
        chook._handle(  # late, absorbed
            _payload("PostToolUse", tool_name="Bash", tool_response={}),
            now_ns=ns + 3_000_000,
        )
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 4_000_000)
        chook._handle(_payload("PreToolUse", tool_name="Bash"), now_ns=ns + 5_000_000)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_response={}),
            now_ns=ns + 6_000_000,
        )
        outcomes = sorted(
            s.attributes[chook.KESTREL_TOOL_OUTCOME]
            for s in _terminal(emitter.get_finished_spans())
        )
        assert outcomes == ["completed", "incomplete"]

    def test_completion_is_parented_by_its_start_not_the_live_turn(self, emitter):
        # The tool started in turn 1; its completion lands while turn 2 is live.
        # It belongs to turn 1 — parenting/counting it against the live turn is
        # the phantom attribution this span shape exists to kill. Driven through
        # the emitter directly since every dispatch path reconciles first.
        ns = 12_199_500_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="P1"), now_ns=ns + 1_000_000)
        state = chook._read_state(chook._state_path("sess-abc"))
        pending = dict(state["pending_tools"])
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 2_000_000)  # turn 2
        state = chook._read_state(chook._state_path("sess-abc"))
        state["pending_tools"] = pending  # the start its completion still holds
        state["terminalized"] = {}
        tracer, _provider = chook._build_tracer("http://x", None)
        chook._emit_tool_span(
            tracer, state,
            _payload("PostToolUse", tool_name="Bash", tool_use_id="P1", tool_response={}),
            ns + 3_000_000,
        )
        spans = _by_name(emitter.get_finished_spans())
        completed = [
            s for s in _terminal(emitter.get_finished_spans())
            if s.attributes[chook.KESTREL_TOOL_OUTCOME] == "completed"
        ][0]
        turn1, turn2 = spans["claude-code turn 1"], spans["claude-code turn 2"]
        assert completed.parent.span_id == turn1.context.span_id
        assert completed.context.trace_id == turn1.context.trace_id
        assert completed.context.trace_id != turn2.context.trace_id
        assert completed.attributes[chook.KESTREL_TURN_ID] == "sess-abc#1"
        # Counted against its own (now closed) turn, never the live one.
        assert int(state["current_turn"]["tool_count"]) == 0


class TestPendingBounded:
    def test_completed_tool_clears_its_pending_entry(self, emitter, tmp_path):
        ns = 12_300_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="c1"), now_ns=ns + 1)
        assert _pending(tmp_path)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="c1", tool_response={}),
            now_ns=ns + 2,
        )
        assert _pending(tmp_path) == {}

    def test_unclaimed_entry_evicted_after_ttl(self, emitter, tmp_path):
        # Reconciliation normally drains pending starts at turn end, so the TTL is
        # a pure backstop for a session abandoned mid-turn — driven here with
        # events that never close the turn (#84).
        ns = 12_400_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="x"), now_ns=ns + 1)
        assert _pending(tmp_path)  # unclaimed — the tool never completed
        # Still pending just inside the TTL (a long-running tool must NOT lose
        # the start its duration is derived from).
        chook._handle(
            _payload("PreToolUse", tool_name="Read", tool_use_id="y"),
            now_ns=ns + chook._PENDING_TTL_S * 1_000_000_000 - 1,
        )
        assert "id:x" in _pending(tmp_path)
        # Past the TTL → evicted on the next event.
        chook._handle(
            _payload("PreToolUse", tool_name="Read", tool_use_id="z"),
            now_ns=ns + chook._PENDING_TTL_S * 1_000_000_000 + 1_000_000_000,
        )
        assert "id:x" not in _pending(tmp_path)

    def test_long_running_tool_keeps_its_real_duration(self, emitter, tmp_path):
        # The TTL must exceed any realistic tool: a 10-minute build still pairs
        # with its start instead of collapsing to a zero-duration point span.
        ns = 12_500_000_000
        ten_min_ns = 600 * 1_000_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        chook._handle(_payload("UserPromptSubmit"), now_ns=ns + 1)
        chook._handle(_payload("PreToolUse", tool_name="Bash", tool_use_id="slow"), now_ns=ns + 2)
        chook._handle(
            _payload("PostToolUse", tool_name="Bash", tool_use_id="slow", tool_response={}),
            now_ns=ns + 2 + ten_min_ns,
        )
        tool = _by_name(emitter.get_finished_spans())["Bash"]
        assert tool.attributes["tool.duration_ms"] == pytest.approx(600_000.0)

    def test_pending_capped_keeping_most_recent(self, emitter, tmp_path):
        ns = 12_600_000_000
        chook._handle(_payload("SessionStart"), now_ns=ns)
        overflow = chook._MAX_PENDING_STARTS + 20
        for i in range(overflow):
            chook._handle(
                _payload("PreToolUse", tool_name="Bash", tool_use_id=f"cap-{i}"),
                now_ns=ns + 1 + i,
            )
        pending = _pending(tmp_path)
        assert sum(len(v) for v in pending.values()) == chook._MAX_PENDING_STARTS
        # The most RECENT starts survive — the ones a completing event may claim.
        assert f"id:cap-{overflow - 1}" in pending
        assert "id:cap-0" not in pending

    def test_prune_tolerates_malformed_state(self):
        state = {"pending_tools": {"id:a": "not-a-list", "id:b": [None, "x", 5]}}
        chook._prune_pending(state, now_ns=5)
        # Unparseable entries are dropped; the legacy bare-int start survives,
        # normalized into the record shape reconciliation needs (#84).
        assert state["pending_tools"] == {
            "id:b": [
                {"tool_name": "tool", "start_ns": 5, "tool_use_id": None, "turn": None}
            ]
        }

    def test_legacy_bare_int_entry_reconciles_by_name(self):
        # A state file written before the record schema holds bare timestamps: a
        # NAME-keyed one still recovers its tool name and reconciles.
        state = {"pending_tools": {"name:Bash": [7]}}
        record = chook._pop_pending(state, "Bash", None)
        assert record == {
            "tool_name": "Bash", "start_ns": 7, "tool_use_id": None, "turn": None,
        }
