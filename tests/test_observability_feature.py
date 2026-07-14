"""
Tests for the ObservabilityHook (per-agent emitter) and ObservabilityFeature.

Covers:
1. Hook registers on all events
2. Hook always returns ALLOW (never blocks)
3. Hook POSTs a lifecycle-event payload to the fleet ingest
4. Hook is a no-op when KESTREL_OBSERVABILITY_URL is unset
5. Hook swallows POST/transport failures
6. Feature registers hook during initialize()
7. Privacy: user_message content is NOT sent, only length
8. Privacy: tool_response error truncated to 200 chars
9. orchestrator = agent when self-driven, else null (Direct)
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kestrel_sdk.hooks.base import (
    HookEvent,
    HookInput,
    HookOutput,
    PermissionDecision,
)
from kestrel_feature_observability.hook import ObservabilityHook, INGEST_PATH
from kestrel_feature_observability.feature import ObservabilityFeature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FLEET_URL = "https://fleet.example.com"
API_KEY = "secret-key"


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


def _configured_hook(agent=None, key=API_KEY):
    """Build a hook with the fleet ingest env vars set, capturing POSTs.

    Returns ``(hook, posts)`` where ``posts`` is a list the fake
    ``httpx.AsyncClient.post`` appends ``(url, json, headers)`` to.
    """
    agent = agent or _make_agent()
    env = {"KESTREL_OBSERVABILITY_URL": FLEET_URL}
    if key is not None:
        env["KESTREL_OBSERVABILITY_KEY"] = key
    # clear=True so a real KESTREL_OBSERVABILITY_KEY in the ambient env can't
    # leak into the key=None case (construction only reads these two vars).
    with patch.dict("os.environ", env, clear=True):
        hook = ObservabilityHook(agent=agent)

    posts = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posts.append((url, json, headers))
            return MagicMock()

    return hook, posts, _FakeClient


async def _drain():
    """Let fire-and-forget POST tasks run to completion."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 1. Hook registers on all events
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_registers_on_all_hook_events(self):
        hook = ObservabilityHook(agent=_make_agent())
        assert set(hook.events) == set(HookEvent)

    def test_priority_is_999(self):
        assert ObservabilityHook(agent=_make_agent()).priority == 999

    def test_name_is_observability(self):
        assert ObservabilityHook(agent=_make_agent()).name == "observability"

    def test_timeout_is_5_seconds(self):
        assert ObservabilityHook(agent=_make_agent()).timeout == 5.0


# ---------------------------------------------------------------------------
# 2. Hook always returns ALLOW
# ---------------------------------------------------------------------------

class TestHookAlwaysAllows:
    @pytest.mark.asyncio
    async def test_returns_allow_on_pre_tool_use(self):
        hook, _, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            result = await hook.execute(_make_input("PreToolUse", tool_name="some_tool"))
        assert result.continue_execution is True
        assert result.permission_decision == PermissionDecision.ALLOW

    @pytest.mark.asyncio
    async def test_returns_allow_on_stop(self):
        hook, _, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            result = await hook.execute(_make_input("Stop"))
        assert result.continue_execution is True

    @pytest.mark.asyncio
    async def test_returns_allow_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            hook = ObservabilityHook(agent=_make_agent())
        result = await hook.execute(_make_input("PreToolUse"))
        assert result.continue_execution is True


# ---------------------------------------------------------------------------
# 3. Hook POSTs to the fleet ingest
# ---------------------------------------------------------------------------

class TestHookEmitsEvents:
    @pytest.mark.asyncio
    async def test_posts_to_host_root_ingest_path(self):
        hook, posts, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(_make_input("PreToolUse", tool_name="web_search"))
            await _drain()

        assert len(posts) == 1
        url, payload, headers = posts[0]
        assert url == FLEET_URL + INGEST_PATH
        assert INGEST_PATH == "/api/host/observability/events"
        assert headers["X-API-Key"] == API_KEY
        assert payload["agent_name"] == "test-agent"
        assert payload["event_type"] == "tool_call"
        assert payload["metadata"]["hook_event_type"] == "PreToolUse"
        assert payload["tool_name"] == "web_search"
        assert payload["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_no_api_key_omits_header(self):
        hook, posts, client = _configured_hook(key=None)
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(_make_input("SessionStart"))
            await _drain()
        assert len(posts) == 1
        _, _, headers = posts[0]
        assert "X-API-Key" not in headers

    @pytest.mark.asyncio
    async def test_all_event_types_emit(self):
        for event in HookEvent:
            hook, posts, client = _configured_hook()
            with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
                result = await hook.execute(_make_input(event.value))
                await _drain()
            assert result.continue_execution is True
            assert len(posts) == 1

    @pytest.mark.asyncio
    async def test_posts_execution_time_and_success(self):
        hook, posts, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(
                _make_input(
                    "PostToolUse",
                    tool_name="t",
                    execution_time_ms=42,
                    tool_response={"success": True, "result": "ok"},
                )
            )
            await _drain()
        _, payload, _ = posts[0]
        assert payload["duration_ms"] == 42
        assert payload["success"] is True

    @pytest.mark.asyncio
    async def test_posts_feature_name(self):
        hook, posts, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(
                _make_input("PreToolUse", tool_name="t", feature_name="SecurityFeature")
            )
            await _drain()
        _, payload, _ = posts[0]
        assert payload["metadata"]["feature_name"] == "SecurityFeature"


# ---------------------------------------------------------------------------
# 4. No-op when unconfigured
# ---------------------------------------------------------------------------

class TestUnconfigured:
    @pytest.mark.asyncio
    async def test_no_post_when_url_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            hook = ObservabilityHook(agent=_make_agent())

        posts = []

        class _FakeClient:
            def __init__(self, *a, **kw):
                posts.append("constructed")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                posts.append("posted")

        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", _FakeClient):
            result = await hook.execute(_make_input("PreToolUse", tool_name="t"))
            await _drain()

        assert result.continue_execution is True
        assert posts == []  # never touched the client


# ---------------------------------------------------------------------------
# 5. Failures are swallowed
# ---------------------------------------------------------------------------

class TestHookExceptionHandling:
    @pytest.mark.asyncio
    async def test_post_raises_is_swallowed(self):
        hook, _, _ = _configured_hook()

        class _FailingClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                raise RuntimeError("network down")

        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", _FailingClient):
            result = await hook.execute(_make_input("PreToolUse", tool_name="t"))
            await _drain()
        assert result.continue_execution is True

    @pytest.mark.asyncio
    async def test_agent_name_missing(self):
        agent = _make_agent()
        del agent.agent_name
        hook, posts, client = _configured_hook(agent=agent)
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            result = await hook.execute(_make_input("PreToolUse"))
            await _drain()
        assert result.continue_execution is True


# ---------------------------------------------------------------------------
# 6. Feature registers hook during initialize()
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
        """Producer-only: router + UI panels belong to the fleet host.

        The feature no longer overrides the base hooks, so both fall through
        to the SDK defaults (``None``) — nothing mounted, no panels shipped.
        """
        feature = ObservabilityFeature(_make_agent())
        assert feature.get_router() is None
        assert feature.get_ui_contributions() is None


# ---------------------------------------------------------------------------
# 7. Privacy: user_message content NOT sent, only length
# ---------------------------------------------------------------------------

class TestPrivacy:
    @pytest.mark.asyncio
    async def test_user_message_not_in_payload(self):
        hook, posts, client = _configured_hook()
        secret_message = "my password is hunter2"
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(_make_input("UserPromptSubmit", user_message=secret_message))
            await _drain()

        _, payload, _ = posts[0]
        assert payload["metadata"]["user_message_length"] == len(secret_message)
        # Content never appears anywhere in the payload — top-level or metadata.
        for value in list(payload.values()) + list(payload["metadata"].values()):
            if isinstance(value, str):
                assert secret_message not in value

    @pytest.mark.asyncio
    async def test_tool_input_not_sent(self):
        hook, posts, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(
                _make_input(
                    "PreToolUse",
                    tool_name="web_search",
                    tool_input={"query": "sensitive query", "api_key": "secret123"},
                )
            )
            await _drain()

        _, payload, _ = posts[0]
        assert "tool_input" not in payload
        for value in payload.values():
            if isinstance(value, str):
                assert "sensitive query" not in value
                assert "secret123" not in value


# ---------------------------------------------------------------------------
# 8. Error truncation
# ---------------------------------------------------------------------------

class TestErrorTruncation:
    @pytest.mark.asyncio
    async def test_long_error_truncated_to_200_chars(self):
        hook, posts, client = _configured_hook()
        long_error = "x" * 500
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(
                _make_input(
                    "PostToolUse",
                    tool_name="t",
                    tool_response={"success": False, "error": long_error},
                )
            )
            await _drain()

        _, payload, _ = posts[0]
        assert payload["success"] is False
        assert len(payload["error_message"]) == 200


# ---------------------------------------------------------------------------
# 9. orchestrator semantics (self-driven vs driven → Direct)
# ---------------------------------------------------------------------------

class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_self_driven_sets_orchestrator_to_agent(self):
        hook, posts, client = _configured_hook(agent=_make_agent(agent_id="did:agent:me"))
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(_make_input("SessionStart"))
            await _drain()
        _, payload, _ = posts[0]
        # Self-driven → orchestrator is the agent's DISPLAY name (matches how the
        # fleet store groups ``agent_name``), not the DID. DID rides in metadata.
        assert payload["orchestrator"] == "test-agent"
        assert payload["metadata"]["agent_did"] == "did:agent:me"

    @pytest.mark.asyncio
    async def test_driven_agent_sets_orchestrator_null(self):
        hook, posts, client = _configured_hook()
        with patch("kestrel_feature_observability.hook.httpx.AsyncClient", client):
            await hook.execute(_make_input("PreToolUse", parent_did="did:agent:driver"))
            await _drain()
        _, payload, _ = posts[0]
        assert payload["orchestrator"] is None
        assert payload["metadata"]["parent_agent"] == "did:agent:driver"
