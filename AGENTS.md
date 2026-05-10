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
│   └── hook.py                  # Lifecycle event hooks
└── tests/
    ├── test_observability_feature.py
    └── test_tool_result_contracts.py
```

## Entry Points

- `kestrel_sovereign.features`: `ObservabilityFeature = "kestrel_feature_observability.feature:ObservabilityFeature"`

## Key Files to Read First

1. `kestrel_feature_observability/feature.py` — Observability feature and tools
2. `kestrel_feature_observability/hook.py` — Lifecycle event hook

## Running Tests

```bash
uv run pytest
```

## Agent-Specific Instructions

- ObservabilityFeature uses the hook system for event logging
- User-message content is not logged; keep the hook observational and non-blocking
- Prometheus metrics use the SDK's shared registry when the optional metrics extra is installed
