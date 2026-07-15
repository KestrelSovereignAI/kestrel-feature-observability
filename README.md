# kestrel-feature-observability

The **single** observability package for Kestrel Sovereign — one repo, one
version, one source of truth for the whole observability domain — with two roles
selected by the two entry-point groups (package boundaries need not match
process boundaries):

- **Base install** (`pip install kestrel-feature-observability`) — the
  lightweight per-agent **emitter** `Feature` (the `kestrel_sovereign.features`
  entry point). It attaches an `ObservabilityHook` to the agent's hook system;
  every lifecycle event is POSTed to the fleet host's observability store
  (`POST {KESTREL_OBSERVABILITY_URL}/api/host/observability/events`). No
  `entities`, no DB — this is what every agent gets. Prometheus metrics emit
  through the SDK's shared registry, so a single `/metrics` scrape stays
  coherent across the framework + every feature package.
- **Host extra** (`kestrel-feature-observability[fleet]`) — pulls
  `kestrel-feature-entities` and enables the **`FleetObservabilityHostFeature`**
  (tenant-scoped event store + query routes + orchestrator swimlane; the
  `kestrel_sovereign.host_features` entry point). The HostFeature lives in the
  `kestrel_feature_observability.fleet` subpackage and its import/entry point is
  **guarded** — on a base install (extra absent) it resolves to `None` and the
  host skips it, so the emitter path never imports `entities`.

> This package supersedes the separate `kestrel-feature-observability-fleet`
> package, which is deprecated: its HostFeature, store, and swimlane now live
> here behind the `[fleet]` extra.

## Installation

```bash
uv pip install kestrel-feature-observability
```

For real Prometheus output:

```bash
uv pip install 'kestrel-feature-observability[metrics]'
```

For the fleet host role (event store + swimlane):

```bash
uv pip install 'kestrel-feature-observability[fleet]'
```

Both features are auto-discovered by Kestrel Sovereign via their entry-point
groups — install the base package alongside `kestrel-sovereign` and
`ObservabilityFeature` registers itself into every agent; install with `[fleet]`
on the host and `FleetObservabilityHostFeature` registers at host scope.

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
- Optional `[fleet]` extra → `kestrel-sovereign-sdk>=0.29.2,<0.30` (the HostFeature
  contract) + `kestrel-feature-entities` (the tenant-scoped event store, pulling
  SQLAlchemy 2.0 async + Alembic). Only this extra pulls `entities`.

The base emitter has **no** runtime dependency on `kestrel-sovereign` (or any
`entities`/fleet package); the hook talks to the fleet host purely over HTTP.
The store's heavy dependencies live entirely behind the `[fleet]` extra, so
agents stay lightweight.

## Development

```bash
uv pip install -e '.[test]'
uv run pytest
```

## License

Apache-2.0
