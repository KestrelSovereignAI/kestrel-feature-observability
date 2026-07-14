# kestrel-feature-observability

Per-agent lifecycle observability **emitter** for Kestrel Sovereign agents.
Attaches an `ObservabilityHook` to the agent's hook system; every lifecycle
event is POSTed to the fleet host's observability store
(`POST {KESTREL_OBSERVABILITY_URL}/api/host/observability/events`). Prometheus
metrics emit through the SDK's shared registry, so a single `/metrics` scrape
stays coherent across the framework + every feature package.

This package is **producer-only**. The event store, query routes, and Console
UI (LLM-call table + fleet swimlane) are owned by the fleet host feature — the
single tenant-aware owner of the observability data plane.

## Installation

```bash
uv pip install kestrel-feature-observability
```

For real Prometheus output:

```bash
uv pip install 'kestrel-feature-observability[metrics]'
```

The feature is auto-discovered by Kestrel Sovereign via the `kestrel_sovereign.features` entry point — install it alongside `kestrel-sovereign` and `ObservabilityFeature` registers itself at startup.

## Emitter transport

The hook reads two **frozen** env vars (the same keys talon's emitter uses):

- `KESTREL_OBSERVABILITY_URL` — the fleet host root. The hook POSTs to this URL
  plus the path `/api/host/observability/events`. When unset, the emit path is a
  **no-op** (the agent still runs; Prometheus counters still fire).
- `KESTREL_OBSERVABILITY_KEY` — sent as the `X-API-Key` request header when set.

Delivery is lightweight and best-effort: an `httpx.AsyncClient` POST,
fire-and-forget with a short (~2s) timeout, **all failures swallowed, no
buffering, no retry, no `entities` dependency**. Each event payload carries
`orchestrator`/`session_id`/`tool_name`/lineage and friends; `orchestrator` is
the agent itself when self-driven, else `null` (rendered "Direct" by the fleet
store).

## Privacy

The hook is observational — it never blocks, denies, or modifies. User-message content is **not** sent (only length); tool errors are truncated to 200 chars; exceptions in the hook are swallowed so they cannot affect agent operation.

## Dependencies

- `kestrel-sovereign-sdk>=0.14.1,<1` — base `Feature`, `Hook`, and shared `metrics` module
- `httpx>=0.27.0` — lightweight HTTP client for the emitter POST
- Optional `[metrics]` extra → `kestrel-sovereign-sdk[metrics]` → `prometheus-client`

No runtime dependency on `kestrel-sovereign` (or any `entities`/fleet package);
the hook talks to the fleet host purely over HTTP.

## Development

```bash
uv pip install -e '.[test]'
uv run pytest
```

## License

Apache-2.0
