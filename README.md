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
  three-view sub-nav: **Timeline** (default) answers when activity happened with
  a temporal overview and compact span detail — its lanes are keyed by the
  (`kestrel.agent_name`, `kestrel.orchestrator`) pair, so a run launched by an
  agent nests under that agent's lane (`Emma/talon` under Emma), a run whose
  launcher has no lane in that project stays top-level under its own label
  (`claude-code/talon`), and unattributed runs keep the plain lane. Talon names a
  stage span for the stage itself, so the bar, its `(started)` marker and the
  worker sub-lane all read that stage as prose — `Implement`,
  `Implement (started)`, `Completion check`, and a gutter of `talon/Implement`
  whose agent segment is never re-cased. It is display-only and keys on the
  span's *name*: `kestrel.stage` is stamped on every span nested inside a stage
  (`command_execution`, `Bash`, `ci`, `self-review`), none of which is named for
  its stage, so those are left exactly as emitted — as is the `kestrel.stage`
  value itself, which the lanes are keyed off. Nothing keys on the span kind:
  the producer picks that per stage (`LLM` for implement/review, `AGENT` for
  coordinate, `CHAIN` for gate). A run or turn whose root started *before* the
  visible window still gets its band: every page Phoenix serves is windowed on
  `startTime`, so panning back — or opening the panel mid-run — would otherwise
  leave the children as loose ticks under no parent. The Timeline backfills the
  missing parents by **exact OTel span id** (Phoenix's `span_id in […]` filter
  condition, which needs no time window at all), hopping up until it reaches a
  parentless root. It resolves **on demand** — the initial load and a settled
  pan/zoom each owe exactly one resolve, which covers the paused and panned views
  the live poll deliberately skips — never on a retry timer. An owed resolve is
  spent only on a **settled ingestion**, the same gate the memory cap prunes on:
  while a paged walk is in flight or truncated, "the parent has not been fetched
  yet" is indistinguishable from "this span is orphaned", so resolving there
  would ask Phoenix for spans already on their way. Depth and breadth are bounded
  independently: a fixed number of hops from any loaded span, carried with each
  id so a wide graph cannot buy itself extra depth, and a fixed number of ids per
  pass, the surplus carried to the next pass rather than dropped. A parent that
  never comes back (never exported, aged out of Phoenix) is simply left orphaned
  at its best-known depth rather than chased.
  **Navigator** answers where it
  fits with the hierarchical Tenant → Fleet → Agent → Subagent → Session → Turn
  → Events tree and a persistent inspector for the selected Turn/Event span;
  and **Phoenix** provides exhaustive trace forensics in the curated thin embed.
  Timeline and Navigator links preserve the exact OTel span ID in both
  directions, visibly highlight it, and report an honest containing-Turn or
  no-highlight fallback when that exact span is unavailable. All three are pure
  read-models over Phoenix's GraphQL through the same-origin
  `/phoenix/graphql` proxy (no store or new host routes). The HostFeature lives
  in the `kestrel_feature_observability.fleet` subpackage. Since the custom
  store/entities were retired, `fleet/feature.py` imports only the
  `HostFeature`/`UIContributions` contract from `kestrel_sdk`, so the host role
  is gated by the **SDK version**, not by an extra-only importable module: the
  package declares the SDK floor-only (`>=0.38.1`) across every install role — the
  host pins the SDK minor, so this package must not add a second ceiling (#99). The import/entry point
  stays **guarded** — if the
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
session band is an attribute grouping — OpenInference `session.id` and the
backward-compatible `kestrel.session_id` are stamped with the same value on
every span — not a trace). A session-marker root is exported lazily on the
first lifecycle event; each `UserPromptSubmit` starts a turn (a new trace root
`<agent> turn <n>`, tagged `kestrel.turn_id`/`kestrel.turn_index`); each
`PreToolUse` emits an instant `<tool> (started)` marker and each `PostToolUse` a
child `tool_span` (tool name, real duration, success,
`kestrel.tool_outcome=completed`) parented to the current turn; `Stop`
reconciles the turn's unfinished tools and emits a `turn <n> summary` (the
session stays live), and `AgentTerminate`/teardown emits the true `session
summary` aggregating turns. For in-process Kestrel agents,
`kestrel.orchestrator` is a diagnostic projection of the canonical driving
parent: the previous `CausationFrame` agent DID for driven work, the verified
lineage DID only for a genuine lineage-root turn, this agent's DID for a root
with no predecessor, and absent when the causal answer is unknown. The value is
never authority. Timeline placement resolves that stable DID through
`kestrel.agent_did`; Navigator displays the same raw projection.

The `turn <n> summary` carries the per-turn stats — `kestrel.tool_count`,
`kestrel.error_count`, `kestrel.success_ratio`, `kestrel.denied_count`,
`kestrel.incomplete_count`, and `kestrel.duration_ms` (the go-forward unified
key; the legacy `kestrel.turn_duration_ms` is still emitted alongside for
back-compat) — and the `session summary` aggregates them across the session
(`kestrel.turn_count` plus the same totals, with `kestrel.duration_ms` alongside
the legacy `kestrel.session_duration_ms`). By default the turn root carries no
prompt text; setting `KESTREL_OTEL_CAPTURE_PROMPTS=1` opts in to stamping the
turn's user prompt on its root span as `input.value`, truncated to
`KESTREL_OTEL_MAX_IO_CHARS` (default `20000`) characters.

### Tool outcomes

Every tool call ends in exactly **one** terminal span, stamped with
`kestrel.tool_outcome`:

- `completed` — the tool ran: the `PostToolUse` (Claude Code hook: also
  `PostToolUseFailure`) `tool_span`, with its real duration and `tool.success`.
- `denied` — Claude Code's `PermissionDenied` event fired, i.e. the auto-mode
  permission classifier refused the call. Also carries
  `tool.denied_by="classifier"`. Claude Code hook only.
- `incomplete` — the tool never completed and no deny event fired: a
  `PreToolUse` guard, a permission rule, or an aborted turn. Produced by
  turn-end reconciliation.

A `denied`/`incomplete` span is a **zero-duration point span at the recorded
start**, carrying `tool.success=false`: the tool never ran, so its span never
claims runtime it did not have. It still pairs with its `<tool> (started)`
marker, so a refused tool renders as one visible terminal stub instead of an
orphaned open-ended band.

Turn-end reconciliation is the backbone and does not depend on a deny event
being available: on `Stop` — and, for a turn interrupted before its `Stop`, on
the next `UserPromptSubmit` or session close — every still-pending tool start
becomes a terminal `incomplete` span emitted into **its own** original turn,
never re-attributed to the current one. An interrupted turn also gets a
retroactive `turn <n> summary`, so every turn is summarized exactly once. The
Claude Code hook's 30-minute pending-start TTL sweep remains only as a backstop
for a session abandoned entirely.

"Exactly one terminal span" holds even when the events arrive out of order.
Claude Code runs each hook event as its own process, so the per-session lock
serializes state writes but guarantees nothing about lifecycle ordering: a
`PostToolUse` can land *after* `Stop` already reconciled its call. Reconciling —
and `PermissionDenied` — therefore leave a **tombstone** for the call (keyed by
`tool_use_id`, else the tool name; bounded by the same TTL/cap as the pending
starts, and by tool name in-process). A terminal event that finds no pending
start but does find a tombstone claims it and emits **nothing**: an exported
OTel span cannot be retroactively re-labeled `incomplete` → `completed`, so the
span already exported stands and the late duplicate is dropped rather than
double-counted and mis-parented into whatever turn is live by then. For the same
reason a completion is always parented, attributed and counted against the turn
stamped on its own `PreToolUse` start, never the current turn.

`kestrel.tool_count`/`kestrel.error_count`/`kestrel.success_ratio` stay over
**executed** tools only — a refusal is not an agent error — so the refusals ride
on the two additive keys `kestrel.denied_count` and `kestrel.incomplete_count`,
present on both the `turn <n> summary` and the `session summary`, and existing
dashboards keep their exact semantics. The fleet Timeline paints a
`denied`/`incomplete` span as a fixed-width labeled stub (`<tool> · denied`)
rather than the unreadable 2px tick its true extent implies — a refusal is an
event operators must see — while the span data stays honestly zero-duration.

The in-process emitter produces only `completed` and `incomplete`: the SDK's
`HookInput` has no per-call id (so pending starts pair by tool **name**, LIFO)
and no deny event, which makes a refusal indistinguishable from an abort. The
Claude Code hook has both — `tool_use_id` for exact pairing of concurrent
same-name calls, and `PermissionDenied` for the explicit `denied` label.

### Turn outcomes

How a turn *ended* is computed once, by the host (kestrel-sovereign#3159), and
delivered to this emitter — the SDK `Stop` hook only means "finished
responding", and the host's strict-audit cancel paths skip it entirely. When the
host exposes `agent.add_turn_outcome_listener` (found duck-typed, like
`get_current_turn_id`), `ObservabilityFeature.initialize()` registers the hook's
listener and `shutdown()` removes it. For a turn with a canonical
`kestrel.turn_id`, `Stop` then leaves the turn open and the listener — called
once per turn on **every** exit — reconciles it and emits its `turn <n>
summary` stamped with `kestrel.turn.outcome`: `completed`, `failed`, `stopped`,
`disconnected` or `interrupted`. A host without the seam, or a turn without a
canonical address, is closed by `Stop` exactly as before (no outcome). The
outcome is the only lifecycle fact a span carries: a Stop receipt's reason and
actor are never copied onto any span.

## Stop, Hold and Resume in the fleet views

The Timeline and Navigator render the host's durable governance receipts as
lifecycle events (#118), through one shared read-model
(`fleet/static/lifecycle.js`) so both views state the same lifecycle with the
same receipt correlation:

- **Sources.** On the same poll as the spans: `GET /api/host/stop/receipts` and
  `GET /api/host/hold/receipts` (sovereign-gated; paged by passing each page's
  `next_cursor` back verbatim, and a caught-up feed re-reads its tail page from
  the cursor issued for it, so a receipt committed later is found and can never
  land behind a page already read), plus `GET /api/host/hold` for the current
  latches. `feed_seq` is opaque: it is never parsed, compared or validated as a
  number, so a 64-bit position a browser cannot hold exactly still renders as a
  normal receipt. Receipts are deduplicated by `receipt_id` and ordered by
  `occurred_at`. An unreadable or refused feed is shown as a notice, never as "nothing
  happened". A poll reads at most five pages per feed; a deeper history keeps
  draining on its own (no Live or Refresh needed) under a "loading older
  history" notice until it is caught up. A page that fails to read keeps that
  backlog: the feed shows as unavailable and is retried on its own, with a
  capped doubling delay, until a read succeeds.
- **Phoenix down.** The receipts and latches are the host's, so they render
  without Phoenix: the "Phoenix is not running" notice sits above the view,
  and a held agent keeps its lane and node while the spans are unavailable.
- **Turn outcomes in the Navigator.** The Turn level reads the `turn <n>
  summary` of exactly the roots it has loaded (`parent_id in [...]`), so an
  older Turn paged in with "Load more" shows the same outcome the Timeline does
  before its trace is opened.
- **Matching — exact identity only.** A receipt is attached to a turn only
  when it names that turn: a turn-scope Stop joins by exact `(trace_id,
  span_id)` (and every span sharing that turn's `kestrel.turn_id`), and its
  actor, reason, scope and outcomes are inspectable on the turn. A tool-call
  Stop may name its tool span, but it stopped that call, not the turn: it never
  classifies the turn and stays inspectable as an event on its agent. Agent- and
  host-scope Stops carry only a DID and a time, and are written after the
  Stop's effects complete — possibly after the turn ended — so they are **lane
  events**: each sits on its agent's Timeline lane / Navigator node at
  `occurred_at` (`target_agent_id` for agent scope, each outcome's own agent
  for a host fan-out) and is never joined to a turn by time. Every Stop keeps
  its lane mark at `occurred_at`, unconditionally: an exact turn-scope Stop is
  shown on its turn's bar *and* on the lane — the turn's state and the time of
  the act are two facts, and nothing else drawn ever hides the mark. Only a `did:`
  identity is placed; anything else (core's `host` for an empty host Stop,
  `unresolved`, any future placeholder) or no identity renders as **agent not
  recorded** — the agent is never inferred.
- **Turn states.** A turn's stopped state comes from its own span
  (`kestrel.turn.outcome=stopped`) or an exact receipt whose disposition is
  `stopped` → **stopped**; the exact receipt, when there is one, is inspectable
  (actor, reason, scope, time, every per-target outcome including
  `unreachable`). A stopped turn with no exact receipt still shows **stopped**;
  the Stop event on its agent's lane is where the why is. `already_complete`
  keeps the turn's own outcome and adds a "Stop arrived after completion"
  marker. `disconnected`, `interrupted` and `failed` render distinctly; only
  `failed` is error-red.
- **Holds.** A held agent always has a visible resting state — a Timeline lane
  and a Navigator node even with no spans. Host and agent latches are drawn
  independently. A Resume is its own event, linked to the Hold it ended, and
  never edits that Hold.
- **Convergence.** The picture is a pure function of (spans, receipts, latches):
  arrival order, duplicates, late receipts and a reload yield the same model.
  An open Timeline popover is a view of that model: it is re-rendered for the
  item it shows after every update, and closes if the item no longer exists.
- **Unknown data.** A `schema_version` this build does not know renders as
  "unrecognized receipt" and never reclassifies a turn.

## Claude Code hook emitter

The same package ships a **`kestrel-obs-claude-hook`** console script so that
**Claude Code** sessions land in the fleet Observability Timeline exactly like
kestrel agents and talon runs. Claude Code's hooks system runs a shell command
per lifecycle event with a JSON payload on stdin; this script turns those events
into the identical span shape as the in-process emitter above — session ⊃ turn ⊃
tool ⊃ tool-start markers, one trace per turn — posted over OTLP/HTTP:

- `SessionStart` → an immediately-ended `AGENT` session-marker root
  (`session.id` and `kestrel.session_id` = the Claude session id,
  `kestrel.agent_name` = `claude-code`, `kestrel.orchestrator` =
  `$KESTREL_OBSERVABILITY_ORCHESTRATOR` else `Direct`). Both session attributes
  are retained on every span the hook emits.
- `UserPromptSubmit` → a labeled `claude-code turn <n>` root (a new trace). By
  default the root carries no prompt text; setting `KESTREL_OTEL_CAPTURE_PROMPTS=1`
  opts in to stamping the payload's `prompt` on the turn root as `input.value`,
  truncated to `KESTREL_OTEL_MAX_IO_CHARS` (default `20000`) characters.
- `PreToolUse` → an instant `<tool> (started)` marker under the current turn,
  and the start is recorded as pending — keyed by `tool_use_id`, which is also
  stamped as `tool.call_id` on the marker and on its terminal span so the
  Timeline pairs concurrent same-name calls one-to-one. `PreToolUse` fires
  *before* the permission check, so a tool attempt is visible the moment it
  starts, even one about to be refused.
- `PostToolUse` / `PostToolUseFailure` → a completed `tool_span`
  (`kestrel.tool_outcome=completed`) under the current turn. `PostToolUse` fires
  after a tool **succeeds**; failed tools fire the separate `PostToolUseFailure`
  event (top-level `error` / `duration_ms`), which is recorded as a failed span.
  Duration prefers the payload's own `duration_ms`, else the gap to the paired
  `PreToolUse`.
- `PermissionDenied` → a terminal `tool_span` with `kestrel.tool_outcome=denied`
  and `tool.denied_by="classifier"`, paired to its marker by `tool_use_id` and
  anchored zero-duration at the recorded start (see **Tool outcomes** above).
  Reconciliation would catch the call at `Stop` anyway; this event only upgrades
  the label from `incomplete` to `denied` and emits it sooner.
- `Stop` → turn-end reconciliation, then a `turn <n> summary` (the session stays
  live); a turn interrupted before its `Stop` is reconciled and summarized
  retroactively at the next `UserPromptSubmit` / `SessionEnd`. `SessionEnd` (and
  a defensive staleness sweep) → the true `session summary` and state cleanup.
  The turn summary carries `kestrel.tool_count`, `kestrel.error_count`,
  `kestrel.success_ratio`, `kestrel.denied_count`, `kestrel.incomplete_count`,
  and `kestrel.duration_ms` (with the legacy `kestrel.turn_duration_ms`
  alongside); the session summary aggregates them (`kestrel.turn_count` plus the
  same totals, `kestrel.duration_ms` alongside the legacy
  `kestrel.session_duration_ms`).

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
fire a **separate** event, so without it errored tool calls would go unrecorded.
`PermissionDenied` is likewise its own event: without it a classifier-refused
tool is still recorded, but only as `incomplete` at turn end instead of `denied`:

```jsonc
{
  "hooks": {
    "SessionStart":       [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "UserPromptSubmit":   [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PreToolUse":         [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PostToolUse":        [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PostToolUseFailure": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "PermissionDenied":   [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "Stop":               [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
    "SubagentStop":       [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/obs-emit.sh" }] }],
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

Stop and Hold receipt reasons and actors are operator-written and
sovereign-gated: the fleet views render them escaped and clipped, and never copy
them into a span, the persisted view state, a URL or a log line.

Prompt capture is strictly opt-in: setting `KESTREL_OTEL_CAPTURE_PROMPTS=1` (off by default) stamps the turn's user prompt on the turn-root span as `input.value`, truncated to `KESTREL_OTEL_MAX_IO_CHARS` (default `20000`) characters — nothing changes unless an operator explicitly enables it at their own wiring point.

## Dependencies

- `kestrel-sovereign-sdk>=0.38.1,<1` — base `Feature`, `Hook`, shared `metrics`, and Two Axes contract
- `httpx>=0.27.0` — lightweight HTTP client (OTLP/HTTP export transport)
- `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` +
  `openinference-semantic-conventions` — the OTel span builders + OTLP export
- Optional `[metrics]` extra → `kestrel-sovereign-sdk[metrics]` → `prometheus-client`
- Optional `[fleet]` extra → `kestrel-sovereign-sdk>=0.38.1,<1` (the HostFeature
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
