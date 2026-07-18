"""
Tests for the OTel instrumentation helper (``kestrel_feature_observability.tracing``).

Covers:
1. No-op when no OTLP endpoint is configured (no provider, no network, no error).
2. ``configure`` builds an enabled tracer when the standard OTLP env var is set.
3. Span builders produce a run→stage→tool→LLM tree, auto-nesting via OTel context.
4. Every span carries the Kestrel attributes + OpenInference conventions.
5. Env-sourced Resource defaults, overridable per call; repo mirrored from run_id.
6. LLM span carries input.value / output.value / llm.model_name.
"""

import pathlib
import re
from unittest.mock import patch

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kestrel_feature_observability.tracing import (
    DEFAULT_OTEL_PROJECT,
    KESTREL_AGENT_NAME,
    KESTREL_ORCHESTRATOR,
    KESTREL_REPO,
    KESTREL_RUN_ID,
    KESTREL_STAGE,
    KestrelTracer,
    configure,
)

_PROJECT_NAME_ATTR = "openinference.project.name"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _memory_tracer(defaults=None):
    """A KestrelTracer backed by an in-memory exporter; returns (tracer, exporter)."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = KestrelTracer(tracer=provider.get_tracer("test"), defaults=defaults or {})
    return tracer, exporter


def _by_name(spans):
    return {s.name: s for s in spans}


# ---------------------------------------------------------------------------
# 1. No-op when unconfigured
# ---------------------------------------------------------------------------

class TestNoOpWhenUnconfigured:
    def test_configure_returns_disabled_tracer(self):
        with patch.dict("os.environ", {}, clear=True):
            t = configure()
        assert isinstance(t, KestrelTracer)
        assert t.enabled is False

    def test_span_builders_are_inert_no_error(self):
        with patch.dict("os.environ", {}, clear=True):
            t = configure()
        # A full tree must run without touching the network or raising.
        with t.run_span("run", agent_name="a"):
            with t.stage_span("stage"):
                with t.tool_span("Bash"):
                    pass
                with t.llm_span("chat", input_value="hi") as span:
                    span.set_attribute("output.value", "yo")
                    assert span.is_recording() is False

    def test_no_exporter_constructed_when_unset(self):
        # If unset, we must never construct an OTLP exporter (no network setup).
        with patch(
            "kestrel_feature_observability.tracing.OTLPSpanExporter"
        ) as exporter:
            with patch.dict("os.environ", {}, clear=True):
                t = configure()
        exporter.assert_not_called()
        assert t.enabled is False


# ---------------------------------------------------------------------------
# 2. configure() enables export when endpoint set
# ---------------------------------------------------------------------------

class TestConfigureEnabled:
    def test_standard_env_var_enables_tracer(self):
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"},
            clear=True,
        ):
            t = configure()
        assert t.enabled is True

    def test_traces_specific_env_var_enables_tracer(self):
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://localhost:6006/v1/traces"},
            clear=True,
        ):
            t = configure()
        assert t.enabled is True

    def test_explicit_endpoint_enables_tracer(self):
        with patch.dict("os.environ", {}, clear=True):
            t = configure(endpoint="http://localhost:6006/v1/traces")
        assert t.enabled is True


# ---------------------------------------------------------------------------
# 3 & 4. Span tree + Kestrel attributes + OpenInference conventions
# ---------------------------------------------------------------------------

class TestSpanTree:
    def test_run_stage_tool_llm_tree_nests(self):
        t, exporter = _memory_tracer(
            defaults={"repo": "KestrelSovereignAI/kestrel-sovereign"}
        )
        with t.run_span("run", agent_name="talon"):
            with t.stage_span("analyze"):
                with t.tool_span("Bash"):
                    pass
                with t.llm_span(
                    "chat", input_value="prompt", model_name="claude-opus-4-8"
                ) as span:
                    span.set_attribute("output.value", "response")

        spans = _by_name(exporter.get_finished_spans())
        assert set(spans) == {"run", "analyze", "Bash", "chat"}

        run, stage, tool, llm = (
            spans["run"], spans["analyze"], spans["Bash"], spans["chat"]
        )
        # Auto-nesting via OTel context: shared trace, correct parentage.
        trace_id = run.context.trace_id
        for s in (stage, tool, llm):
            assert s.context.trace_id == trace_id
        assert stage.parent.span_id == run.context.span_id
        assert tool.parent.span_id == stage.context.span_id
        assert llm.parent.span_id == stage.context.span_id

    def test_openinference_span_kinds(self):
        t, exporter = _memory_tracer()
        with t.run_span("run"):
            with t.stage_span("stage"):
                with t.tool_span("tool"):
                    pass
                with t.llm_span("llm"):
                    pass
        spans = _by_name(exporter.get_finished_spans())
        assert spans["run"].attributes["openinference.span.kind"] == "AGENT"
        assert spans["stage"].attributes["openinference.span.kind"] == "CHAIN"
        assert spans["tool"].attributes["openinference.span.kind"] == "TOOL"
        assert spans["llm"].attributes["openinference.span.kind"] == "LLM"

    def test_kestrel_attributes_on_every_span(self):
        t, exporter = _memory_tracer(
            defaults={
                "repo": "owner/repo",
                "run_id": "owner/repo#42",
                "orchestrator": "talon",
            }
        )
        with t.run_span("run", agent_name="talon"):
            with t.stage_span("analyze", agent_name="talon"):
                with t.tool_span("Bash", agent_name="talon"):
                    pass

        for span in exporter.get_finished_spans():
            attrs = span.attributes
            assert attrs[KESTREL_REPO] == "owner/repo"
            assert attrs[KESTREL_RUN_ID] == "owner/repo#42"
            assert attrs[KESTREL_ORCHESTRATOR] == "talon"
            assert attrs[KESTREL_AGENT_NAME] == "talon"
        stage = _by_name(exporter.get_finished_spans())["analyze"]
        assert stage.attributes[KESTREL_STAGE] == "analyze"


# ---------------------------------------------------------------------------
# 5. Defaults / per-call overrides / repo mirroring
# ---------------------------------------------------------------------------

class TestAttributeResolution:
    def test_repo_mirrored_from_run_id_when_absent(self):
        t, exporter = _memory_tracer(defaults={"run_id": "KestrelSovereignAI/kestrel#7"})
        with t.run_span("run"):
            pass
        run = exporter.get_finished_spans()[0]
        assert run.attributes[KESTREL_REPO] == "KestrelSovereignAI/kestrel"

    def test_per_call_overrides_env_defaults(self):
        t, exporter = _memory_tracer(defaults={"repo": "default/repo"})
        with t.run_span("run", repo="override/repo", agent_name="a"):
            pass
        run = exporter.get_finished_spans()[0]
        assert run.attributes[KESTREL_REPO] == "override/repo"

    def test_stage_defaults_to_span_name(self):
        t, exporter = _memory_tracer()
        with t.stage_span("build"):
            pass
        span = exporter.get_finished_spans()[0]
        assert span.attributes[KESTREL_STAGE] == "build"

    def test_canonical_resource_attr_becomes_default(self):
        # Overriding via the canonical ``kestrel.repo`` key must land in the
        # tracer defaults (so spans carry it), not just the OTel Resource.
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"},
            clear=True,
        ):
            t = configure(resource_attributes={KESTREL_REPO: "canon/repo"})
        assert t._defaults["repo"] == "canon/repo"

    def test_run_id_override_mirrors_repo(self):
        # A run_id override with no repo mirrors kestrel.repo into the defaults.
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"},
            clear=True,
        ):
            t = configure(resource_attributes={"run_id": "owner/repo#1"})
        assert t._defaults["run_id"] == "owner/repo#1"
        assert t._defaults["repo"] == "owner/repo"

    def test_canonical_run_id_override_mirrors_repo(self):
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"},
            clear=True,
        ):
            t = configure(resource_attributes={KESTREL_RUN_ID: "owner/repo#2"})
        assert t._defaults["run_id"] == "owner/repo#2"
        assert t._defaults["repo"] == "owner/repo"

    def test_env_defaults_read_by_configure(self):
        with patch.dict(
            "os.environ",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006",
                "KESTREL_RUN_ID": "acme/widgets#99",
                "KESTREL_ORCHESTRATOR": "talon",
            },
            clear=True,
        ):
            t = configure()
        # repo mirrored from run_id, orchestrator carried through.
        assert t._defaults["repo"] == "acme/widgets"
        assert t._defaults["run_id"] == "acme/widgets#99"
        assert t._defaults["orchestrator"] == "talon"


# ---------------------------------------------------------------------------
# 6. LLM span I/O attributes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. openinference.project.name Resource attribute (obs#41)
# ---------------------------------------------------------------------------

class TestProjectName:
    def _resource_attrs(self, t):
        """Resource attributes on the configured tracer's provider."""
        return dict(t._tracer.resource.attributes)

    def test_defaults_to_kestrel_fleet_when_unset(self):
        # No KESTREL_OTEL_PROJECT → the shared default so the emitter and the
        # fleet embed's deep-link agree out of the box.
        assert DEFAULT_OTEL_PROJECT == "kestrel-fleet"
        with patch.dict(
            "os.environ",
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"},
            clear=True,
        ):
            t = configure()
        assert self._resource_attrs(t)[_PROJECT_NAME_ATTR] == "kestrel-fleet"

    def test_env_var_overrides_default(self):
        with patch.dict(
            "os.environ",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006",
                "KESTREL_OTEL_PROJECT": "team-alpha",
            },
            clear=True,
        ):
            t = configure()
        assert self._resource_attrs(t)[_PROJECT_NAME_ATTR] == "team-alpha"

    def test_explicit_resource_attribute_wins(self):
        # Canonical key passed via resource_attributes beats env + default.
        with patch.dict(
            "os.environ",
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006",
                "KESTREL_OTEL_PROJECT": "from-env",
            },
            clear=True,
        ):
            t = configure(resource_attributes={_PROJECT_NAME_ATTR: "explicit"})
        assert self._resource_attrs(t)[_PROJECT_NAME_ATTR] == "explicit"

    def test_project_is_not_stamped_as_span_attribute(self):
        # It's a Resource attribute (how Phoenix routes projects), not per-span.
        t, exporter = _memory_tracer()
        with t.run_span("run"):
            pass
        span = exporter.get_finished_spans()[0]
        assert _PROJECT_NAME_ATTR not in span.attributes

    def test_js_deep_link_default_matches_python_default(self):
        # Single source of truth guard: the fleet embed's DEFAULT_PROJECT must
        # match tracing.DEFAULT_OTEL_PROJECT or the curated panel deep-links to a
        # different (empty) Phoenix project than the hook writes to.
        js = (
            pathlib.Path(__file__).resolve().parent.parent
            / "kestrel_feature_observability"
            / "fleet"
            / "static"
            / "observability.js"
        ).read_text(encoding="utf-8")
        m = re.search(r'const\s+DEFAULT_PROJECT\s*=\s*"([^"]+)"', js)
        assert m is not None, "DEFAULT_PROJECT constant not found in observability.js"
        assert m.group(1) == DEFAULT_OTEL_PROJECT


class TestLLMSpan:
    def test_llm_span_carries_io_and_model(self):
        t, exporter = _memory_tracer()
        with t.llm_span(
            "chat",
            input_value="what is 2+2?",
            output_value="4",
            model_name="claude-opus-4-8",
        ):
            pass
        span = exporter.get_finished_spans()[0]
        assert span.attributes["input.value"] == "what is 2+2?"
        assert span.attributes["output.value"] == "4"
        assert span.attributes["llm.model_name"] == "claude-opus-4-8"
