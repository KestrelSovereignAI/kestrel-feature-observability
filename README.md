# kestrel-feature-observability

The **single** observability package for Kestrel Sovereign — one repo, one
version, one source of truth for the whole observability domain — with two roles
selected by the two entry-point groups (package boundaries need not match
process boundaries):

- **Base install** (`pip install kestrel-feature-observability`) — the
  lightweight per-agent **emitter** `Feature` (the `kestrel_sovereign.features`
  entry point). It attaches an `ObservabilityHook` to the agent's hook system;
  every lifecycle event is emitted as an OpenTelemetry span (a session
  `run_span` with child `tool_span`s) via `KestrelTracer`, exported over
  OTLP/HTTP to whatever `OTEL_EXPORTER_OTLP_ENDPOINT` points at (e.g. a
  host-supervised Phoenix). No DB — this is what every agent gets. Prometheus
  metrics emit through the SDK's shared registry, so a single `/metrics` scrape
  stays coherent across the framework + every feature package.
- **Host extra** (`kestrel-feature-observability[fleet]`) — enables the
  **`FleetObservabilityHostFeature`** (the `kestrel_sovereign.host_features`
  entry point), which ships the single "Observability" console panel with a
  two-item sub-nav: **Navigator** (default) — the hierarchical fleet drill-down
  (Tenant → Fleet → Agent → Subagent → Session → Turn → Events) rendered
  kestrel-native as a lazily-expanding virtualized tree, a pure read-model over
  Phoenix's GraphQL through the same-origin `/phoenix/graphql` proxy (no store,
  no new host routes) — and **Phoenix**, the curated thin embed of the
  host-supervised Phoenix UI, which the navigator's per-trace "open in Phoenix"
  links deep-link into. The HostFeature lives in the
  `kestrel_feature_observability.fleet` subpackage. Since the custom
  store/entities were retired, `fleet/feature.py` imports only the
  `HostFeature`/`UIContributions` contract from `kestrel_sdk`, so the host role
  is gated by the **SDK version**, not by an extra-only importable module: the
  `[fleet]` extra tightens the SDK pin (`>=0.30.0,<0.31`) to the range that
  exports that contract. The import/entry point stays **guarded** — if the
  resolved SDK is too old to export the contract, it degrades to `None` (with a
  warning logged) and the host skips the panel instead of crashing the feature
  scan.

> This package supersedes the separate `kestrel-feature-observability-fleet`
> package, which is deprecated.

## Installation

```bash
uv pip install kestrel-feature-observability
```

For real Prometheus output:

```bash
uv pip install 'kestrel-feature-observability[metrics]'
```

For the fleet host role (the Phoenix-embed console panel):

```bash
uv pip install 'kestrel-feature-observability[fleet]'
```

Both features are auto-discovered by Kestrel Sovereign via their entry-point
groups — install the base package alongside `kestrel-sovereign` and
`ObservabilityFeature` registers itself into every agent; install with `[fleet]`
on the host and `FleetObservabilityHostFeature` registers at host scope.

## Emitter transport

The hook emits OpenTelemetry spans via `KestrelTracer`
(`kestrel_feature_observability.tracing`), exported over OTLP/HTTP. Endpoint
discovery is OTel-standard:

- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` — a full traces endpoint (used as-is), or
- `OTEL_EXPORTER_OTLP_ENDPOINT` — a base endpoint (the exporter appends
  `/v1/traces`), e.g. the host-supervised local Phoenix.
- `OTEL_EXPORTER_OTLP_HEADERS` — honored for auth.
- `KESTREL_OTEL_PROJECT` — the Phoenix project spans land in, stamped as the
  `openinference.project.name` Resource attribute. Defaults to `kestrel-fleet`,
  which the fleet console's curated Observability panel deep-links to — so
  per-agent hook traces show up there instead of Phoenix's "default" project.

When no OTLP endpoint is configured the tracer is a **no-op** — no provider, no
exporter, no network — so the emit path costs nothing and the agent runs
unaffected (Prometheus counters still fire locally). A session `run_span` is
opened lazily on the first lifecycle event and closed on `Stop`/`AgentTerminate`;
each `PostToolUse` emits a child `tool_span` carrying tool name, real duration,
and success. `orchestrator` is the agent itself when self-driven, else inherited.

## Privacy

The hook is observational — it never blocks, denies, or modifies. User-message content is **not** recorded (never stamped on any span); tool errors are truncated to 200 chars; exceptions in the hook are swallowed so they cannot affect agent operation.

## Dependencies

- `kestrel-sovereign-sdk>=0.14.1,<1` — base `Feature`, `Hook`, and shared `metrics` module
- `httpx>=0.27.0` — lightweight HTTP client (OTLP/HTTP export transport)
- `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` +
  `openinference-semantic-conventions` — the OTel span builders + OTLP export
- Optional `[metrics]` extra → `kestrel-sovereign-sdk[metrics]` → `prometheus-client`
- Optional `[fleet]` extra → `kestrel-sovereign-sdk>=0.30.0,<0.31` (the HostFeature
  contract for the Phoenix-embed console panel). No DB.

The base emitter has **no** runtime dependency on `kestrel-sovereign` (or any
fleet package); it emits OTel spans over OTLP/HTTP. The `[fleet]` extra adds only
the host SDK contract for the embed panel, so agents stay lightweight.

## Development

```bash
uv pip install -e '.[test]'
uv run pytest
```

## License

Apache-2.0
