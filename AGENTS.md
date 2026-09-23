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
│       └── static/              # observability.js (sub-nav container: Navigator | Phoenix embed)
│                                # + navigator.js (fleet drill-down over Phoenix GraphQL)
│                                # + lifecycle.js (Stop/Hold/Resume receipt read-model, #118)
└── tests/
    ├── test_observability_feature.py   # emitter
    ├── test_tracing.py                 # KestrelTracer
    ├── test_turn_outcome_listener.py   # core turn outcomes end the turn root (#118)
    ├── test_lifecycle_render_model.py  # Stop/Hold/Resume render rules, both views (#118)
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
  (`kestrel_feature_observability.fleet`) owns the single "Observability" console panel — a two-item
  sub-nav: **Navigator** (default; the hierarchical Tenant→Fleet→Agent→Subagent→Session→Turn→Events
  drill-down in `navigator.js`, a pure read-model over Phoenix's GraphQL through the same-origin
  `/phoenix/graphql` proxy — no store, no backend routes) | **Phoenix** (the curated thin embed of the
  host-supervised Phoenix UI, which the navigator's "open in Phoenix" trace links deep-link into) —
  behind the `[fleet]` extra.
- Keep the emitter path lightweight and DB-free: the emitter package (`feature.py`, `hook.py`, `tracing.py`,
  the top-level `__init__.py`) must never import `kestrel_feature_observability.fleet`. The fleet
  subpackage's import stays **guarded** (`fleet/__init__.py` → `FleetObservabilityHostFeature is None`), but
  since the store/entities were retired the guard is now keyed on the **SDK version**, not the presence of the
  `[fleet]` extra: `fleet/feature.py` imports only `HostFeature`/`UIContributions` from `kestrel_sdk`, so on a
  modern SDK the class binds for real; only a too-old SDK (below the HostFeature contract) trips the guard,
  which logs a warning and resolves the `host_features` entry point to `None` so the host skips the panel.
- Every install role declares the SDK as **floor-only** (`>=0.38.1,<1`) — base, `[metrics]`,
  `[fleet]`, test extra, and dev group. **Do not add an upper bound.** The host
  (`kestrel-sovereign`) pins the SDK to a single minor and so decides which SDK the environment
  gets; a second ceiling here has to be walked forward by hand, in a separate repo, on every host
  bump, and any lag makes the graph unsatisfiable. The previous `<0.35` policy — added to *prevent*
  unsatisfiable resolves — was what caused them: the host moved to `>=0.35.0,<0.36` and every host
  with this package installed resolved to a broken pair (issue #99, kestrel-sovereign#2865).
  Compatibility with newer SDKs is proven by the `test-latest-sdk` CI leg, not asserted by a pin.
  If that leg goes red, fix the code or raise the floor; do not reintroduce a ceiling.
- Fleet UI panels are always-on: `UIContributions.capability=None` (host gate bug fixed separately in
  kestrel-sovereign#2459).
- User-message content is never recorded on any span; keep the hook observational, non-blocking, and
  a no-op when no OTLP endpoint is configured (exceptions swallowed).
- Turn outcomes come from core (#118): `ObservabilityFeature.initialize()` registers the hook's
  `on_turn_outcome` through the duck-typed `agent.add_turn_outcome_listener` (and `shutdown()` removes
  it). `shutdown()` keeps `_hook`: the host calls it BEFORE `get_hooks()` to unregister hooks, and
  `initialize()` replaces it on re-enable. While registered, a turn with a canonical `kestrel.turn_id` is closed by that listener — on every
  exit, stamping `kestrel.turn.outcome` on its summary — not by the SDK `Stop` hook.
- Lifecycle rendering (#118) lives in `fleet/static/lifecycle.js`, shared by Timeline and Navigator:
  a turn is stopped by its own `kestrel.turn.outcome` or an exact receipt; only a turn-scope receipt joins a turn, by exact
  `(trace_id, span_id)` (a tool-call Stop naming its tool span never classifies the turn) — agent/host-scope Stops are lane events at `occurred_at`, NEVER time-joined to a turn; every Stop's lane mark is drawn unconditionally (never hidden because its turn's bar or anything else is drawn); an open Timeline popover is re-rendered from the current model after every update (closed if its item is gone);
  only a `did:` identity is placed (anything else is "agent not recorded"); held/resumed come only from the Hold
  receipts and latches; `feed_seq` is opaque (paging passes `next_cursor` back verbatim, dedupe is by `receipt_id` — never
  parse or compare it as a number); and the render model must stay a pure function of (spans, receipts, latches). Receipt reason/actor are
  never copied into spans, view state, URLs or logs; unknown `schema_version`s render as unrecognized.
- Prometheus metrics use the SDK's shared registry when the optional metrics extra is installed
