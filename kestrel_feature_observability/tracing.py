"""
Kestrel OTel instrumentation helper — OpenInference span builders + OTLP export.

Foundation of the OTel-native observability pivot: a small, importable helper
usable by the per-agent hook AND by talon. It sets up an OTel ``TracerProvider``
wired to an OTLP/HTTP exporter and hands back a :class:`KestrelTracer` whose
``run_span`` / ``stage_span`` / ``tool_span`` / ``llm_span`` context managers
emit spans using **OpenInference** semantic conventions, each stamped with the
standard **Kestrel** attributes (``kestrel.repo`` / ``kestrel.orchestrator`` /
``kestrel.run_id`` / ``kestrel.stage`` / ``kestrel.agent_name``).

Endpoint discovery is OTel-standard and pluggable:

- ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (a full traces endpoint, used as-is), or
- ``OTEL_EXPORTER_OTLP_ENDPOINT`` (a base endpoint; the exporter appends
  ``/v1/traces``) — e.g. the host-supervised local Phoenix.
- ``OTEL_EXPORTER_OTLP_HEADERS`` is honored for auth (read by the exporter).

INV-SOLO: when no OTLP endpoint is configured the helper is a **no-op** — no
provider, no exporter, no network — and its span builders yield inert spans. It
never errors and never blocks the agent.

Process-global identity is sourced from the environment (``KESTREL_REPO`` /
``KESTREL_RUN_ID`` / ``KESTREL_ORCHESTRATOR``) as Resource defaults, and any of
those plus ``stage`` / ``agent_name`` may be overridden per span. ``kestrel.repo``
is first-class — it's how you group/filter by which repo an agent/talon is
working; when a repo isn't given it is mirrored from the ``owner/repo#issue``
``run_id``.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

# --- OTel SDK (lightweight; no entities/DB). Guarded so an unexpectedly missing
# --- dep degrades to a pure no-op rather than breaking the agent import path.
try:
    from opentelemetry import trace as _trace
    from opentelemetry.trace import set_span_in_context as _set_span_in_context
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001 - degrade to no-op tracing when SDK absent
    _set_span_in_context = None
    _OTEL_AVAILABLE = False

# --- OpenInference semantic conventions (span kinds + I/O attribute keys). ---
try:
    from openinference.semconv.trace import (
        OpenInferenceSpanKindValues,
        SpanAttributes,
    )

    _OI_KIND_KEY = SpanAttributes.OPENINFERENCE_SPAN_KIND
    _OI_INPUT_KEY = SpanAttributes.INPUT_VALUE
    _OI_OUTPUT_KEY = SpanAttributes.OUTPUT_VALUE
    _OI_MODEL_KEY = SpanAttributes.LLM_MODEL_NAME
    _KIND_AGENT = OpenInferenceSpanKindValues.AGENT.value
    _KIND_CHAIN = OpenInferenceSpanKindValues.CHAIN.value
    _KIND_TOOL = OpenInferenceSpanKindValues.TOOL.value
    _KIND_LLM = OpenInferenceSpanKindValues.LLM.value
except Exception:  # noqa: BLE001 - fall back to the literal convention strings
    _OI_KIND_KEY = "openinference.span.kind"
    _OI_INPUT_KEY = "input.value"
    _OI_OUTPUT_KEY = "output.value"
    _OI_MODEL_KEY = "llm.model_name"
    _KIND_AGENT = "AGENT"
    _KIND_CHAIN = "CHAIN"
    _KIND_TOOL = "TOOL"
    _KIND_LLM = "LLM"

# --- OpenInference project-name Resource attribute. Phoenix routes traces into
# --- projects by this Resource attribute; stamping it keeps per-agent hook
# --- traces in the named project the fleet embed deep-links to (obs#41). ------
try:
    from openinference.semconv.resource import ResourceAttributes as _OIResource

    _OI_PROJECT_NAME_KEY = _OIResource.PROJECT_NAME
except Exception:  # noqa: BLE001 - fall back to the literal convention string
    _OI_PROJECT_NAME_KEY = "openinference.project.name"

# --- Standard Kestrel span/resource attribute keys. ``kestrel.repo`` is
# --- first-class (grouping/filtering by repo), NOT a tenancy knob. -----------
KESTREL_REPO = "kestrel.repo"
KESTREL_ORCHESTRATOR = "kestrel.orchestrator"
KESTREL_RUN_ID = "kestrel.run_id"
KESTREL_STAGE = "kestrel.stage"
KESTREL_AGENT_NAME = "kestrel.agent_name"

# --- Env vars carrying process-global identity (Resource defaults). ---------
_REPO_ENV = "KESTREL_REPO"
_RUN_ID_ENV = "KESTREL_RUN_ID"
_ORCHESTRATOR_ENV = "KESTREL_ORCHESTRATOR"

# --- Phoenix project selection. ``KESTREL_OTEL_PROJECT`` picks the Phoenix
# --- project the emitter writes into (stamped as the ``openinference.project.name``
# --- Resource attribute). Defaults to ``DEFAULT_OTEL_PROJECT`` so per-agent hook
# --- traces land in the same named project as everything else instead of
# --- Phoenix's "default". This MUST match the deep-link default in the fleet
# --- embed (``fleet/static/observability.js`` → ``DEFAULT_PROJECT``) so the
# --- curated Observability panel opens the very project these spans populate.
_OTEL_PROJECT_ENV = "KESTREL_OTEL_PROJECT"
DEFAULT_OTEL_PROJECT = "kestrel-fleet"

# --- Standard OTLP endpoint env vars (drive the no-op-when-unset behavior). --
_TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

_INSTRUMENTATION_NAME = "kestrel_feature_observability"


def _repo_from_run_id(run_id: Optional[str]) -> Optional[str]:
    """Mirror ``kestrel.repo`` (``owner/repo``) out of an ``owner/repo#issue`` run id."""
    if not run_id:
        return None
    repo = run_id.split("#", 1)[0].strip()
    return repo or None


def _resolve_endpoint(endpoint: Optional[str]) -> Optional[str]:
    """Explicit endpoint wins; else the standard OTLP env vars. ``None`` → no-op."""
    if endpoint:
        return endpoint
    return (
        os.environ.get(_TRACES_ENDPOINT_ENV)
        or os.environ.get(_ENDPOINT_ENV)
        or None
    )


def _resolve_project(resource_attributes: Optional[Mapping[str, str]]) -> str:
    """Resolve the Phoenix project name: explicit override → env → default.

    An explicit ``resource_attributes`` value wins (accepted under the canonical
    ``openinference.project.name`` key or the ``project`` shorthand), else
    ``KESTREL_OTEL_PROJECT``, else :data:`DEFAULT_OTEL_PROJECT` (``kestrel-fleet``)
    so the emitter and the curated fleet embed agree out of the box.
    """
    if resource_attributes:
        for key in (_OI_PROJECT_NAME_KEY, "project", "project_name"):
            value = resource_attributes.get(key)
            if value:
                return value
    return os.environ.get(_OTEL_PROJECT_ENV) or DEFAULT_OTEL_PROJECT


def _env_defaults() -> Dict[str, str]:
    """Process-global identity from the environment (repo mirrored from run_id)."""
    repo = os.environ.get(_REPO_ENV)
    run_id = os.environ.get(_RUN_ID_ENV)
    orchestrator = os.environ.get(_ORCHESTRATOR_ENV)
    if not repo:
        repo = _repo_from_run_id(run_id)

    defaults: Dict[str, str] = {}
    if repo:
        defaults["repo"] = repo
    if run_id:
        defaults["run_id"] = run_id
    if orchestrator:
        defaults["orchestrator"] = orchestrator
    return defaults


class _NoopSpan:
    """Inert span yielded when tracing is unconfigured — accepts every call."""

    def set_attribute(self, *_a: Any, **_k: Any) -> None:
        pass

    def set_attributes(self, *_a: Any, **_k: Any) -> None:
        pass

    def add_event(self, *_a: Any, **_k: Any) -> None:
        pass

    def set_status(self, *_a: Any, **_k: Any) -> None:
        pass

    def record_exception(self, *_a: Any, **_k: Any) -> None:
        pass

    def update_name(self, *_a: Any, **_k: Any) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def end(self, *_a: Any, **_k: Any) -> None:
        pass


class KestrelTracer:
    """Holds an OTel tracer and builds OpenInference spans with Kestrel attributes.

    The span builders return context managers that auto-nest via OTel context, so
    a caller writes one ``with`` each::

        with t.run_span("talon-run", agent_name="talon"):
            with t.stage_span("analyze"):
                with t.tool_span("Bash"):
                    ...
                with t.llm_span("chat", input_value=prompt) as span:
                    span.set_attribute("output.value", response)

    When constructed without a tracer (endpoint unset) every builder yields an
    inert span: no recording, no export, no error.
    """

    def __init__(self, tracer: Any, defaults: Optional[Mapping[str, str]] = None):
        self._tracer = tracer
        self._defaults: Dict[str, str] = dict(defaults or {})

    @property
    def enabled(self) -> bool:
        """True when a real exporter-backed tracer is configured."""
        return self._tracer is not None

    def _kestrel_attrs(
        self,
        *,
        agent_name: Optional[str],
        stage: Optional[str],
        repo: Optional[str],
        run_id: Optional[str],
        orchestrator: Optional[str],
    ) -> Dict[str, str]:
        """Resolve per-call values over env defaults; mirror repo from run_id."""
        run_id = run_id if run_id is not None else self._defaults.get("run_id")
        repo = repo if repo is not None else self._defaults.get("repo")
        if not repo:
            repo = _repo_from_run_id(run_id)
        orchestrator = (
            orchestrator
            if orchestrator is not None
            else self._defaults.get("orchestrator")
        )

        attrs: Dict[str, str] = {}
        if repo:
            attrs[KESTREL_REPO] = repo
        if orchestrator:
            attrs[KESTREL_ORCHESTRATOR] = orchestrator
        if run_id:
            attrs[KESTREL_RUN_ID] = run_id
        if stage:
            attrs[KESTREL_STAGE] = stage
        if agent_name:
            attrs[KESTREL_AGENT_NAME] = agent_name
        return attrs

    @contextmanager
    def _span(
        self,
        name: str,
        kind: str,
        *,
        agent_name: Optional[str] = None,
        stage: Optional[str] = None,
        repo: Optional[str] = None,
        run_id: Optional[str] = None,
        orchestrator: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> Iterator[Any]:
        span_attrs: Dict[str, Any] = {_OI_KIND_KEY: kind}
        span_attrs.update(
            self._kestrel_attrs(
                agent_name=agent_name,
                stage=stage,
                repo=repo,
                run_id=run_id,
                orchestrator=orchestrator,
            )
        )
        if extra:
            span_attrs.update({k: v for k, v in extra.items() if v is not None})
        if attributes:
            span_attrs.update({k: v for k, v in attributes.items() if v is not None})

        if self._tracer is None:
            yield _NoopSpan()
            return

        with self._tracer.start_as_current_span(name, attributes=span_attrs) as span:
            yield span

    def _start_span(
        self,
        name: str,
        kind: str,
        *,
        parent: Optional[Any] = None,
        start_time: Optional[int] = None,
        agent_name: Optional[str] = None,
        stage: Optional[str] = None,
        repo: Optional[str] = None,
        run_id: Optional[str] = None,
        orchestrator: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Start a span WITHOUT making it current and return the live span.

        Unlike :meth:`_span`, this never attaches the span to the ambient OTel
        context, so a long-held span cannot leak parentage onto unrelated spans
        created between its start and end. The caller owns the span lifetime and
        MUST call ``span.end()`` (optionally with an explicit ``end_time``).

        - ``parent`` (a live span) sets explicit nesting — required because the
          span is never current, so implicit-context parenting won't apply.
        - ``start_time`` (epoch-ns) backdates the span start (e.g. to reflect a
          tool's real runtime on a completed event).
        """
        span_attrs: Dict[str, Any] = {_OI_KIND_KEY: kind}
        span_attrs.update(
            self._kestrel_attrs(
                agent_name=agent_name,
                stage=stage,
                repo=repo,
                run_id=run_id,
                orchestrator=orchestrator,
            )
        )
        if extra:
            span_attrs.update({k: v for k, v in extra.items() if v is not None})
        if attributes:
            span_attrs.update({k: v for k, v in attributes.items() if v is not None})

        if self._tracer is None:
            return _NoopSpan()

        ctx = (
            _set_span_in_context(parent)
            if parent is not None and _set_span_in_context is not None
            else None
        )
        return self._tracer.start_span(
            name, context=ctx, start_time=start_time, attributes=span_attrs
        )

    def start_run_span(self, name: str, **kwargs: Any) -> Any:
        """Start a held-open AGENT span (NOT made current); caller must ``end()`` it.

        For a session/agent run whose lifetime spans many discrete events: hold
        the returned span on the caller, parent child spans to it explicitly, and
        end it on the terminal event. Because it is never made current, it never
        leaks into the ambient context — overlapping runs stay separate traces.
        """
        return self._start_span(name, _KIND_AGENT, **kwargs)

    def emit_tool_span(
        self,
        name: str,
        *,
        parent: Optional[Any] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """Emit a completed TOOL span with explicit timing, parented to ``parent``.

        ``start_time`` / ``end_time`` are epoch-ns. Backdate ``start_time`` to
        ``end_time - duration`` so the exported span's duration reflects the real
        tool runtime (a correct Phoenix/waterfall) rather than ~0. The span is
        started and immediately ended; the caller does not manage its lifetime.
        """
        span = self._start_span(
            name, _KIND_TOOL, parent=parent, start_time=start_time, **kwargs
        )
        span.end(end_time=end_time)
        return span

    # ------------------------------------------------------------------
    # Span builders — one ``with`` each, auto-nesting via OTel context.
    # ------------------------------------------------------------------

    def run_span(self, name: str, **kwargs: Any):
        """Top-level run/agent span (OpenInference ``AGENT``)."""
        return self._span(name, _KIND_AGENT, **kwargs)

    def stage_span(self, name: str, *, stage: Optional[str] = None, **kwargs: Any):
        """Chain/stage span (OpenInference ``CHAIN``); ``stage`` defaults to ``name``."""
        return self._span(
            name, _KIND_CHAIN, stage=stage if stage is not None else name, **kwargs
        )

    def tool_span(self, name: str, **kwargs: Any):
        """Tool-invocation span (OpenInference ``TOOL``)."""
        return self._span(name, _KIND_TOOL, **kwargs)

    def llm_span(
        self,
        name: str,
        *,
        input_value: Optional[str] = None,
        output_value: Optional[str] = None,
        model_name: Optional[str] = None,
        **kwargs: Any,
    ):
        """LLM span (OpenInference ``LLM``) carrying ``input.value`` / ``output.value`` /
        ``llm.model_name``. ``output_value`` is often set on the span after the call."""
        extra = {
            _OI_INPUT_KEY: input_value,
            _OI_OUTPUT_KEY: output_value,
            _OI_MODEL_KEY: model_name,
        }
        return self._span(name, _KIND_LLM, extra=extra, **kwargs)


def configure(
    *,
    endpoint: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    resource_attributes: Optional[Mapping[str, str]] = None,
    service_name: str = "kestrel",
    set_global: bool = False,
) -> KestrelTracer:
    """Build a :class:`KestrelTracer`, wiring an OTLP/HTTP exporter when configured.

    Endpoint is the explicit ``endpoint`` arg, else the standard
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_ENDPOINT`` env
    vars. When none is set (or the OTel SDK is unavailable) the returned tracer is
    a **no-op**: no provider, no exporter, no network — INV-SOLO.

    Process-global identity (``KESTREL_REPO`` / ``KESTREL_RUN_ID`` /
    ``KESTREL_ORCHESTRATOR``) becomes Resource + default span attributes;
    ``resource_attributes`` overrides those. The Phoenix project is stamped as the
    ``openinference.project.name`` Resource attribute from ``KESTREL_OTEL_PROJECT``
    (default :data:`DEFAULT_OTEL_PROJECT` — ``kestrel-fleet``), so traces land in
    the named project the fleet embed deep-links to rather than Phoenix's
    "default". Auth headers come from ``OTEL_EXPORTER_OTLP_HEADERS`` (read by the
    exporter) or the ``headers`` arg.
    """
    defaults = _env_defaults()
    if resource_attributes:
        # Accept identity overrides under either the shorthand key or the
        # canonical ``kestrel.*`` key; both land in ``defaults`` (shorthand) so
        # they become Resource *and* default span attributes consistently.
        for dst, keys in (
            ("repo", ("repo", KESTREL_REPO)),
            ("run_id", ("run_id", KESTREL_RUN_ID)),
            ("orchestrator", ("orchestrator", KESTREL_ORCHESTRATOR)),
        ):
            for src in keys:
                if src in resource_attributes:
                    defaults[dst] = resource_attributes[src]
        # Mirror repo from an overridden run_id when repo wasn't given.
        if not defaults.get("repo"):
            mirrored = _repo_from_run_id(defaults.get("run_id"))
            if mirrored:
                defaults["repo"] = mirrored

    resolved = _resolve_endpoint(endpoint)
    if not resolved or not _OTEL_AVAILABLE:
        # Unconfigured (or SDK absent) → pure no-op tracer.
        return KestrelTracer(tracer=None, defaults=defaults)

    try:
        resource = Resource.create(_resource_attrs(defaults, service_name, resource_attributes))
        provider = TracerProvider(resource=resource)
        exporter_kwargs: Dict[str, Any] = {}
        if endpoint:
            exporter_kwargs["endpoint"] = endpoint
        if headers:
            exporter_kwargs["headers"] = dict(headers)
        exporter = OTLPSpanExporter(**exporter_kwargs)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        if set_global:
            _trace.set_tracer_provider(provider)
        tracer = provider.get_tracer(_INSTRUMENTATION_NAME)
        return KestrelTracer(tracer=tracer, defaults=defaults)
    except Exception as e:  # noqa: BLE001 - never let setup break the agent
        logger.debug("KestrelTracer setup failed (non-fatal, no-op): %s", e)
        return KestrelTracer(tracer=None, defaults=defaults)


def _resource_attrs(
    defaults: Mapping[str, str],
    service_name: str,
    resource_attributes: Optional[Mapping[str, str]],
) -> Dict[str, str]:
    """Assemble Resource attributes: service name + process-global Kestrel identity."""
    attrs: Dict[str, str] = {"service.name": service_name}
    if defaults.get("repo"):
        attrs[KESTREL_REPO] = defaults["repo"]
    if defaults.get("orchestrator"):
        attrs[KESTREL_ORCHESTRATOR] = defaults["orchestrator"]
    if defaults.get("run_id"):
        attrs[KESTREL_RUN_ID] = defaults["run_id"]
    # Phoenix routes traces into projects by this Resource attribute; stamp it
    # (KESTREL_OTEL_PROJECT → default "kestrel-fleet") so per-agent hook traces
    # land in the same named project the fleet embed deep-links to (obs#41).
    attrs[_OI_PROJECT_NAME_KEY] = _resolve_project(resource_attributes)
    if resource_attributes:
        # Pass through any extra caller-supplied resource attributes verbatim
        # (skip the identity keys — shorthand *and* canonical — already resolved
        # into ``defaults`` above and stamped from there, plus the project keys
        # already resolved into ``openinference.project.name``).
        _identity_keys = frozenset(
            ("repo", "run_id", "orchestrator",
             KESTREL_REPO, KESTREL_RUN_ID, KESTREL_ORCHESTRATOR,
             _OI_PROJECT_NAME_KEY, "project", "project_name")
        )
        for key, value in resource_attributes.items():
            if key not in _identity_keys and value is not None:
                attrs[key] = value
    return attrs


__all__ = [
    "KestrelTracer",
    "configure",
    "KESTREL_REPO",
    "KESTREL_ORCHESTRATOR",
    "KESTREL_RUN_ID",
    "KESTREL_STAGE",
    "KESTREL_AGENT_NAME",
    "DEFAULT_OTEL_PROJECT",
]
