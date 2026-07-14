# kestrel-feature-observability — Agent Instructions

See [README.md](README.md) for package overview.

## Package Structure

```
kestrel-feature-observability/
├── pyproject.toml
├── README.md
├── kestrel_feature_observability/
│   ├── __init__.py
│   ├── feature.py               # ObservabilityFeature entry point
│   └── hook.py                  # Lifecycle event emitter (POSTs to fleet)
└── tests/
    └── test_observability_feature.py
```

## Entry Points

- `kestrel_sovereign.features`: `ObservabilityFeature = "kestrel_feature_observability.feature:ObservabilityFeature"`

## Key Files to Read First

1. `kestrel_feature_observability/feature.py` — Observability feature (hook registration only)
2. `kestrel_feature_observability/hook.py` — Lifecycle event emitter (POSTs to the fleet store)

## Running Tests

```bash
uv run pytest
```

## Agent-Specific Instructions

- ObservabilityFeature is producer-only: the hook POSTs lifecycle events to the fleet host's
  observability ingest (`POST {KESTREL_OBSERVABILITY_URL}/api/host/observability/events`, auth via
  `X-API-Key: {KESTREL_OBSERVABILITY_KEY}`). No local store, query tools, HTTP router, or UI panels —
  those belong to the fleet host feature. Keep the emitter free of any `entities` dependency.
- User-message content is not sent; keep the hook observational, non-blocking, and fire-and-forget
  (short timeout, failures swallowed, no retry/buffering)
- Prometheus metrics use the SDK's shared registry when the optional metrics extra is installed
