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
│   ├── hook.py                  # Lifecycle event emitter (OTel spans via tracing.py)
│   ├── tracing.py               # KestrelTracer: OpenInference span builders + OTLP export
│   └── fleet/                   # [fleet] extra — host role, guarded import
│       ├── __init__.py          # Guarded export (None when host SDK contract absent)
│       ├── feature.py           # FleetObservabilityHostFeature (host_features)
│       └── static/              # observability.js (Phoenix embed panel)
└── tests/
    ├── test_observability_feature.py   # emitter
    ├── test_tracing.py                 # KestrelTracer
    └── test_feature.py                 # fleet HostFeature (UI contribution)
```

## Entry Points

- `kestrel_sovereign.features`: `ObservabilityFeature = "kestrel_feature_observability.feature:ObservabilityFeature"` (base emitter, every agent)
- `kestrel_sovereign.host_features`: `FleetObservabilityHostFeature = "kestrel_feature_observability.fleet:FleetObservabilityHostFeature"` (guarded; host role, `[fleet]` extra)

## Key Files to Read First

1. `kestrel_feature_observability/feature.py` — emitter feature (hook registration only)
2. `kestrel_feature_observability/hook.py` — Lifecycle event emitter (OTel spans via `tracing.py`)
3. `kestrel_feature_observability/fleet/feature.py` — fleet HostFeature (Phoenix embed panel, `[fleet]` extra)

## Running Tests

```bash
uv run pytest
```

## Agent-Specific Instructions

- One package, two roles via two entry-point groups. The **base emitter**
  (`kestrel_feature_observability.feature`/`hook`) emits OTel spans (session `run_span` → child
  `tool_span`s) via `KestrelTracer` (`tracing.py`), exported over OTLP/HTTP to whatever
  `OTEL_EXPORTER_OTLP_ENDPOINT` points at (e.g. a host-supervised Phoenix). The **fleet host role**
  (`kestrel_feature_observability.fleet`) owns the single "Observability" console panel — a thin embed of
  the host-supervised Phoenix UI — behind the `[fleet]` extra.
- Keep the emitter path lightweight and DB-free: the emitter package (`feature.py`, `hook.py`, `tracing.py`,
  the top-level `__init__.py`) must never import `kestrel_feature_observability.fleet`. The fleet
  subpackage's import stays **guarded** (`fleet/__init__.py` → `FleetObservabilityHostFeature is None`), but
  since the store/entities were retired the guard is now keyed on the **SDK version**, not the presence of the
  `[fleet]` extra: `fleet/feature.py` imports only `HostFeature`/`UIContributions` from `kestrel_sdk`, so on a
  modern SDK the class binds for real; only a too-old SDK (below the HostFeature contract) trips the guard,
  which logs a warning and resolves the `host_features` entry point to `None` so the host skips the panel.
- The tightened HostFeature SDK pin (`>=0.30.0,<0.31`) lives in the `[fleet]` extra **only**; the base install
  keeps the wider SDK floor (`>=0.14.1,<1`).
- Fleet UI panels are always-on: `UIContributions.capability=None` (host gate bug fixed separately in
  kestrel-sovereign#2459).
- User-message content is never recorded on any span; keep the hook observational, non-blocking, and
  a no-op when no OTLP endpoint is configured (exceptions swallowed).
- Prometheus metrics use the SDK's shared registry when the optional metrics extra is installed
