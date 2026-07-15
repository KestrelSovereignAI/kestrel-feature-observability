# kestrel-feature-observability — Agent Instructions

See [README.md](README.md) for package overview.

## Package Structure

```
kestrel-feature-observability/
├── pyproject.toml
├── README.md
├── kestrel_feature_observability/
│   ├── __init__.py
│   ├── feature.py               # ObservabilityFeature (emitter) entry point
│   ├── hook.py                  # Lifecycle event emitter (POSTs to fleet)
│   └── fleet/                   # [fleet] extra — host role, guarded import
│       ├── __init__.py          # Guarded exports (None when entities absent)
│       ├── feature.py           # FleetObservabilityHostFeature (host_features)
│       ├── store.py             # Tenant-scoped event store (entities)
│       ├── endpoints.py         # Host-root ingest/query/tree/stream router
│       ├── models.py            # ObservabilityEvent ORM model
│       ├── backplane.py         # Live-stream pub/sub fan-out
│       ├── redaction.py         # Recursive metadata denylist redaction
│       ├── migrations/          # Alembic revisions (kestrel_entities.migrations)
│       └── static/              # swimlane.js + swimlane.lanes.js
└── tests/
    ├── test_observability_feature.py   # emitter
    └── test_{store,endpoints,feature,backplane,models,redaction}.py  # fleet
```

## Entry Points

- `kestrel_sovereign.features`: `ObservabilityFeature = "kestrel_feature_observability.feature:ObservabilityFeature"` (base emitter, every agent)
- `kestrel_sovereign.host_features`: `FleetObservabilityHostFeature = "kestrel_feature_observability.fleet:FleetObservabilityHostFeature"` (guarded; host role, `[fleet]` extra)
- `kestrel_entities.models` / `kestrel_entities.migrations`: `observability_fleet` (entities discovery, `[fleet]` extra only)

## Key Files to Read First

1. `kestrel_feature_observability/feature.py` — emitter feature (hook registration only)
2. `kestrel_feature_observability/hook.py` — Lifecycle event emitter (POSTs to the fleet store)
3. `kestrel_feature_observability/fleet/feature.py` — fleet HostFeature (store + swimlane, `[fleet]` extra)

## Running Tests

```bash
uv run pytest
```

## Agent-Specific Instructions

- One package, two roles via two entry-point groups. The **base emitter**
  (`kestrel_feature_observability.feature`/`hook`) POSTs lifecycle events to the fleet host's
  observability ingest (`POST {KESTREL_OBSERVABILITY_URL}/api/host/observability/events`, auth via
  `X-API-Key: {KESTREL_OBSERVABILITY_KEY}`). The **fleet host role** (`kestrel_feature_observability.fleet`)
  owns the store, query routes, and swimlane panel behind the `[fleet]` extra.
- Keep the emitter path free of any `entities` dependency: the emitter package (`feature.py`, `hook.py`,
  the top-level `__init__.py`) must never import `kestrel_feature_observability.fleet` or `entities`.
  The fleet subpackage's import is **guarded** (`fleet/__init__.py` → `FleetObservabilityHostFeature is None`
  when the extra is absent) so a base install imports clean and the `host_features` entry point resolves to `None`.
- `entities` + the HostFeature SDK contract (`>=0.29.2,<0.30`) live in the `[fleet]` extra **only**.
- Fleet UI panels are always-on: `UIContributions.capability=None` (host gate bug fixed separately in
  kestrel-sovereign#2459).
- User-message content is not sent; keep the hook observational, non-blocking, and fire-and-forget
  (short timeout, failures swallowed, no retry/buffering)
- Prometheus metrics use the SDK's shared registry when the optional metrics extra is installed
