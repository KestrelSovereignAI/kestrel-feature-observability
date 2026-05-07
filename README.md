# kestrel-feature-observability

Lifecycle event observability for Kestrel Sovereign agents. Attaches an `ObservabilityHook` to the agent's hook system; every lifecycle event is logged to the agent's `observability_store`. Prometheus metrics emit through the SDK's shared registry, so a single `/metrics` scrape stays coherent across the framework + every feature package.

## Installation

```bash
uv pip install kestrel-feature-observability
```

For real Prometheus output:

```bash
uv pip install 'kestrel-feature-observability[metrics]'
```

The feature is auto-discovered by Kestrel Sovereign via the `kestrel_sovereign.features` entry point — install it alongside `kestrel-sovereign` and `ObservabilityFeature` registers itself at startup.

## Tools

| Tool | Category | Description |
|------|----------|-------------|
| `observability_summary` | DATA_ACCESS | Recent metric and error counts |
| `observability_query` | DATA_ACCESS | Query lifecycle events by type and time window |
| `observability_session` | DATA_ACCESS | Per-session event timeline |

## Privacy

The hook is observational — it never blocks, denies, or modifies. User-message content is **not** logged (only length); tool errors are truncated to 200 chars; exceptions in the hook are swallowed so they cannot affect agent operation.

## Dependencies

- `kestrel-sovereign-sdk>=0.3,<1` — base `Feature`, `tool`, `ToolCategory`, `Hook`, and shared `metrics` module
- Optional `[metrics]` extra → `kestrel-sovereign-sdk[metrics]` → `prometheus-client`

No runtime dependency on `kestrel-sovereign` itself; the feature accesses `agent.observability_store` via duck typing, so it works against any host that provides one.

## Development

```bash
uv pip install -e '.[test]'
uv run pytest
```

## License

Apache-2.0
