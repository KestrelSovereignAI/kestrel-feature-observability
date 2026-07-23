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

> **Embed note:** the browser console may log `No HydrateFallback element
> provided to render during initial hydration` on the Phoenix subtab — this
> comes from Phoenix's own React Router bundle (vendor-streamdown chunk) during
> SPA hydration and is expected upstream noise (cosmetic, no functional impact);
> the arize-phoenix bump that resolves it is blocked by kestrel-sovereign's
> `fastapi` pin.

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
unaffected (Prometheus counters still fire locally). The span shape is the nested
doll **session ⊃ turn ⊃ tool ⊃ tool-start markers**, one trace per turn (the
session band is an attribute grouping — `kestrel.session_id` is stamped on every
span — not a trace). A session-marker root is exported lazily on the first
lifecycle event; each `UserPromptSubmit` starts a turn (a new trace root
`<agent> turn <n>`, tagged `kestrel.turn_id`/`kestrel.turn_index`); each
`PreToolUse` emits an instant `<tool> (started)` marker and each `PostToolUse` a
child `tool_span` (tool name, real duration, success) parented to the current
turn; `Stop` emits a `turn <n> summary` (the session stays live), and
`AgentTerminate`/teardown emits the true `session summary` aggregating turns.
`orchestrator` is the agent itself when self-driven, else inherited.

## Claude Code hook emitter

The same package ships a **`kestrel-obs-claude-hook`** console script so that
**Claude Code** sessions land in the fleet Observability Timeline exactly like
kestrel agents and talon runs. Claude Code's hooks system runs a shell command
per lifecycle event with a JSON payload on stdin; this script turns those events
into the identical span shape as the in-process emitter above — session ⊃ turn ⊃
tool ⊃ tool-start markers, one trace per turn — posted over OTLP/HTTP:

- `SessionStart` → an immediately-ended `AGENT` session-marker root
  (`kestrel.session_id` = the Claude session id, `kestrel.agent_name` =
  `claude-code`, `kestrel.orchestrator` = `$KESTREL_OBSERVABILITY_ORCHESTRATOR`
  else `Direct`).
- `UserPromptSubmit` → a labeled `claude-code turn <n>` root (a new trace).
- `PreToolUse` → an instant `<tool> (started)` marker under the current turn.
- `PostToolUse` / `PostToolUseFailure` → a completed `tool_span` under the current
  turn. `PostToolUse` fires after a tool **succeeds**; failed tools fire the
  separate `PostToolUseFailure` event (top-level `error` / `duration_ms`), which
  is recorded as a failed span. Duration prefers the payload's own `duration_ms`,
  else the gap to the paired `PreToolUse`.
- `Stop` → a `turn <n> summary` (the session stays live); `SessionEnd` (and a
  defensive staleness sweep) → the true `session summary` and state cleanup.

A `SessionStart` with `source` `compact`/`resume`/`fork` preserves the live
session (Claude Code reuses the `session_id`), so compaction never resets the
turn counter or duplicates turn ids.

Each hook invocation is its own process, so a tiny per-session state file
(`$KESTREL_OBS_CLAUDE_STATE_DIR`, else `$XDG_STATE_HOME/kestrel-obs-claude`, else
`$TMPDIR/kestrel-obs-claude/<session_id>.json`, written atomically) carries the
session/turn trace + span ids so spans across invocations share traces with no
daemon. The `openinference.project.name` (project = repo) is resolved from the
payload `cwd`'s git remote (`owner/repo`), else `$KESTREL_OTEL_PROJECT`, else
omitted, and cached per session. The script **always exits 0, prints nothing to
stdout** (Claude Code interprets `PreToolUse`/`Stop` stdout for gating), never
records the user prompt, and is an **instant no-op** — OpenTelemetry is never even
imported — when neither `OTEL_EXPORTER_OTLP_ENDPOINT` nor
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set.

### Wiring it into Claude Code

Hooks live in `~/.claude/hooks/` by convention; add a thin wrapper that pins the
endpoint and execs the console script (keeping the endpoint out of your global
env), matching the existing hook-directory layout:

```bash
# ~/.claude/hooks/obs-emit.sh
#!/usr/bin/env bash
exec env OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:6006 kestrel-obs-claude-hook
```

```bash
chmod +x ~/.claude/hooks/obs-emit.sh
```

Then register the wrapper on the lifecycle events in `~/.claude/settings.json`.
`settings.json` supports **multiple** hooks per event, so these are **added
alongside** any existing entries (e.g. a `PreToolUse` guard) — never replace or
reorder them. Note `PostToolUseFailure` alongside `PostToolUse`: failed tools
fire a **separate** event, so without it errored tool calls would go unrecorded:

```jsonc
{
  "hooks": {
    "SessionStart":       [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "UserPromptSubmit":   [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PreToolUse":         [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PostToolUse":        [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PostToolUseFailure": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "Stop":               [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "SessionEnd":         [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }]
  }
}
```

### Making the console script reachable

The script must be on `PATH` (or invoked by absolute path) from **any** cwd,
since Claude Code runs hooks from the project directory. Two options:

- **Host venv (absolute path).** If the host installs this package into a venv,
  point the wrapper at the absolute console-script path, e.g.
  `exec env OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:6006 /opt/kestrel/venv/bin/kestrel-obs-claude-hook`.
- **`uv tool install`.** `uv tool install kestrel-feature-observability` puts
  `kestrel-obs-claude-hook` on the uv tools `PATH` (`~/.local/bin`). Note that a
  `uv tool` venv is **isolated**: editing this package's source (or bumping a
  dependency) does **not** update an already-installed tool — re-run
  `uv tool install --reinstall kestrel-feature-observability` (or, for local
  development, `uv tool install --editable .` and reinstall after dependency
  changes) to pick up changes.

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
