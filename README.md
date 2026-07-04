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

## Console panels

The feature contributes two ES modules to the Console via `get_ui_contributions()`,
each self-registering through the host `ui-ext` `registerPanel` registry and gated
on the `observability` capability:

- **LLM Calls** (`static/llm-calls.js`) — paged, filterable LLM-call table.
- **Fleet Timeline** (`static/timeline.js`) — the headline swimlane view. A left
  agent selector populated from `GET /agent-tree`, and a right timeline of lanes
  (per agent) + **nested sublanes** — talon jobs / subagents indented under their
  driver via the event lineage fields (`parent_agent`/`driven_by`/
  `parent_session_id`/`subagent_id`). Pause/Play, time-range (1m/5m/all), and
  status/tool/hook color modes; live updates via SSE when the host exposes an
  event stream, else polling of the subtree events endpoint.

`static/timeline.js` keeps its grouping logic (`buildLanes`/`nestLanes`) pure and
DOM-free at import time — panel registration and rendering run only in a browser —
so the nesting logic is unit-tested with node's built-in runner
(`node --test tests/timeline.nesting.test.mjs`), ported from the original
kestrel-claws `dashboard/tests/timeline.test.ts`. This swimlane supersedes the
kestrel-claws `dashboard/src/views/timeline.ts`, which should be retired (removed
or stubbed to point here) in a follow-up kestrel-claws PR.

## Privacy

The hook is observational — it never blocks, denies, or modifies. User-message content is **not** logged (only length); tool errors are truncated to 200 chars; exceptions in the hook are swallowed so they cannot affect agent operation.

## Dependencies

- `kestrel-sovereign-sdk>=0.14.1,<1` — base `Feature`, `tool`, `ToolCategory`, `Hook`, and shared `metrics` module
- Optional `[metrics]` extra → `kestrel-sovereign-sdk[metrics]` → `prometheus-client`

No runtime dependency on `kestrel-sovereign` itself; the feature accesses `agent.observability_store` via duck typing, so it works against any host that provides one.

## Development

```bash
uv pip install -e '.[test]'
uv run pytest
```

## License

Apache-2.0
