/**
 * Kestrel Sovereign Console — LLM Calls Panel (feature-owned)
 *
 * Shipped by the ObservabilityFeature via `get_ui_contributions()`. The boot
 * loader `import()`s this module from `/features/{slug}/static/llm-calls.js`
 * and it self-registers a panel through the ui-ext panel registry — exactly the
 * way the Spawn feature's `static/spawn.js` does. There is no entry for it in
 * core `index.html`/`app.js`; disabling the feature flips the `observability`
 * capability and removes the tab at runtime.
 *
 * The panel reads the feature's own query router:
 *   GET /api/observability/llm-calls   (paged, filterable list)
 *   GET /api/observability/llm-stats   (aggregate stats + latency percentiles)
 * — the same paths a downstream host (Frinz) already served, so its bespoke
 * "LLM Calls" pane can be deleted in favour of this one.
 *
 * Embedding-host constraints (see issue): no absolute asset paths beyond the
 * `/features/{slug}/static` mount, theme variables only (the embed remaps
 * them), and the table lives inside `.panel-content` and scrolls internally.
 */

import API from '/js/api.js';
import { registerPanel } from '/js/ui-ext/panels.js';
import bus from '/js/ui-ext/bus.js';

const PANEL_ID = 'llm-calls';
const CAPABILITY = 'observability';

// ============================================================================
// Panel body markup — filter bar, stats header, internally-scrolling table.
// ============================================================================
const PANEL_HTML = `
    <div class="row-between mb-4">
        <h2 class="m-0">LLM Calls</h2>
        <div class="row-center row-gap-lg">
            <label style="font-size: 0.8rem; color: var(--text-secondary);">
                Window
                <select id="llm-hours" style="
                    margin-left: 0.25rem;
                    padding: 0.2rem 0.4rem;
                    background: var(--bg-tertiary);
                    color: var(--text-primary);
                    border: 1px solid var(--border-color);
                    border-radius: 4px;
                    font-size: 0.8rem;
                ">
                    <option value="1">Last 1h</option>
                    <option value="6">Last 6h</option>
                    <option value="24" selected>Last 24h</option>
                    <option value="168">Last 7d</option>
                </select>
            </label>
            <label style="font-size: 0.8rem; color: var(--text-secondary);">
                Status
                <select id="llm-status" style="
                    margin-left: 0.25rem;
                    padding: 0.2rem 0.4rem;
                    background: var(--bg-tertiary);
                    color: var(--text-primary);
                    border: 1px solid var(--border-color);
                    border-radius: 4px;
                    font-size: 0.8rem;
                ">
                    <option value="">All</option>
                    <option value="true">Success</option>
                    <option value="false">Error</option>
                </select>
            </label>
            <input id="llm-model" type="text" placeholder="model…" style="
                padding: 0.2rem 0.5rem;
                background: var(--bg-tertiary);
                color: var(--text-primary);
                border: 1px solid var(--border-color);
                border-radius: 4px;
                font-size: 0.8rem;
                width: 9rem;
            " />
            <button id="llm-refresh" style="
                padding: 0.375rem 0.75rem;
                background: var(--bg-tertiary);
                border: 1px solid var(--border-color);
                border-radius: 4px;
                color: var(--text-primary);
                cursor: pointer;
                font-size: 0.85rem;
            ">&#x21BB; Refresh</button>
        </div>
    </div>

    <!-- Stats header -->
    <div id="llm-stats" style="
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
        gap: 0.75rem;
        margin-bottom: 1rem;
    "></div>

    <!-- Call table (scrolls internally so the panel-content never overflows) -->
    <div style="
        background: var(--bg-secondary);
        border: 1px solid var(--border-color);
        border-radius: 8px;
        overflow: hidden;
    ">
        <div id="llm-calls-list" style="max-height: 55vh; overflow-y: auto;">
            <div class="empty-state text-sm" style="padding: 2rem; text-align: center; color: var(--text-secondary);">
                Switch to this tab or click refresh to load LLM calls.
            </div>
        </div>
    </div>
`;

// ============================================================================
// Panel registration (lazy render on first activation)
// ============================================================================
function renderPanel(bodyEl) {
    if (!bodyEl) return;
    bodyEl.innerHTML = PANEL_HTML;

    bodyEl.querySelector('#llm-refresh')?.addEventListener('click', () => loadLLMData());
    bodyEl.querySelector('#llm-hours')?.addEventListener('change', () => loadLLMData());
    bodyEl.querySelector('#llm-status')?.addEventListener('change', () => loadLLMData());
    bodyEl.querySelector('#llm-model')?.addEventListener('change', () => loadLLMData());
}

registerPanel({
    panelId: PANEL_ID,
    label: 'LLM Calls',
    // Capability derived from the feature's enabled state (#2041). Missing key
    // defaults true (#879), so a host that never computes `observability` still
    // shows the panel.
    gate: () => API.hasCapability(CAPABILITY),
    render: renderPanel,
});

// Load on activation — the registry emits `panel:shown` after the body render.
bus.on('panel:shown', (payload) => {
    if (payload && payload.panelId === PANEL_ID) loadLLMData();
});

// ============================================================================
// Data loading
// ============================================================================
function currentFilters() {
    const hours = document.getElementById('llm-hours')?.value || '24';
    const statusRaw = document.getElementById('llm-status')?.value || '';
    const model = (document.getElementById('llm-model')?.value || '').trim();
    return { hours, statusRaw, model };
}

export async function loadLLMData() {
    // #879 deep-link defense — never fetch for a host that opted out.
    if (!API.hasCapability(CAPABILITY)) return;

    const { hours, statusRaw, model } = currentFilters();

    const callParams = new URLSearchParams({ limit: '100', hours_ago: hours });
    if (statusRaw !== '') callParams.append('success', statusRaw);
    if (model) callParams.append('model', model);

    const statParams = new URLSearchParams({ hours_ago: hours });

    try {
        const [callsData, statsData] = await Promise.all([
            API.request(`/api/observability/llm-calls?${callParams}`),
            API.request(`/api/observability/llm-stats?${statParams}`),
        ]);
        renderStats(statsData || {}, hours);
        renderCalls((callsData && callsData.calls) || []);
    } catch (e) {
        console.error('Failed to load LLM observability data:', e);
        renderCallsError(e && e.message ? e.message : String(e));
    }
}

// ============================================================================
// Rendering
// ============================================================================
function statCard(value, label, detail) {
    return `
        <div style="
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 0.75rem 1rem;
        ">
            <div style="font-size: 1.35rem; font-weight: 600; color: var(--text-primary);">${escapeHtml(String(value))}</div>
            <div style="font-size: 0.8rem; color: var(--text-secondary);">${escapeHtml(label)}</div>
            ${detail ? `<div style="font-size: 0.7rem; color: var(--text-tertiary);">${escapeHtml(detail)}</div>` : ''}
        </div>
    `;
}

function renderStats(stats, hours) {
    const container = document.getElementById('llm-stats');
    if (!container) return;

    const latency = stats.latency_ms || {};
    const totalTokens = (stats.total_input_tokens || 0) + (stats.total_output_tokens || 0);
    const successCount = stats.success_count != null
        ? stats.success_count
        : Math.round((stats.total_calls || 0) * (stats.success_rate || 0) / 100);

    container.innerHTML = [
        statCard(stats.total_calls || 0, 'Calls', `Last ${hours}h`),
        statCard(`${successCount} / ${stats.total_calls || 0}`, 'Succeeded', `${stats.success_rate || 0}%`),
        statCard(formatTokens(totalTokens), 'Tokens', `${formatTokens(stats.total_input_tokens || 0)} in / ${formatTokens(stats.total_output_tokens || 0)} out`),
        statCard(`${Math.round(latency.avg || stats.avg_duration_ms || 0)}ms`, 'Avg latency', 'Mean duration'),
        statCard(`${Math.round(latency.p95 || 0)}ms`, 'p95 latency', '95th percentile'),
    ].join('');
}

function renderCalls(calls) {
    const container = document.getElementById('llm-calls-list');
    if (!container) return;

    if (!calls.length) {
        container.innerHTML = `
            <div style="padding: 2rem; text-align: center; color: var(--text-secondary); font-size: 0.875rem;">
                No LLM calls found in the selected window.
            </div>
        `;
        return;
    }

    const rows = calls.map(call => {
        const time = call.timestamp ? formatTime(call.timestamp) : '';
        const tokensIn = call.input_tokens || 0;
        const tokensOut = call.output_tokens || 0;
        const duration = call.duration_ms != null ? `${call.duration_ms}ms` : '—';
        const statusColor = call.success ? 'var(--success)' : 'var(--error)';
        const statusText = call.success ? 'ok' : 'error';
        const model = call.model || 'unknown';
        return `
            <tr style="border-top: 1px solid var(--border-color);">
                <td style="padding: 0.4rem 0.75rem; font-size: 0.8rem; color: var(--text-secondary); white-space: nowrap;">${escapeHtml(time)}</td>
                <td style="padding: 0.4rem 0.75rem; font-size: 0.8rem; color: var(--text-primary);">${escapeHtml(model)}</td>
                <td style="padding: 0.4rem 0.75rem; font-size: 0.8rem; color: var(--text-secondary); text-align: right;">${tokensIn}</td>
                <td style="padding: 0.4rem 0.75rem; font-size: 0.8rem; color: var(--text-secondary); text-align: right;">${tokensOut}</td>
                <td style="padding: 0.4rem 0.75rem; font-size: 0.8rem; color: var(--text-secondary); text-align: right;">${escapeHtml(duration)}</td>
                <td style="padding: 0.4rem 0.75rem; font-size: 0.8rem;"><span style="color: ${statusColor}; font-weight: 600;">${statusText}</span></td>
            </tr>
        `;
    }).join('');

    container.innerHTML = `
        <table style="width: 100%; border-collapse: collapse;">
            <thead>
                <tr style="position: sticky; top: 0; background: var(--bg-tertiary);">
                    <th style="padding: 0.5rem 0.75rem; text-align: left; font-size: 0.75rem; color: var(--text-tertiary);">Time</th>
                    <th style="padding: 0.5rem 0.75rem; text-align: left; font-size: 0.75rem; color: var(--text-tertiary);">Model</th>
                    <th style="padding: 0.5rem 0.75rem; text-align: right; font-size: 0.75rem; color: var(--text-tertiary);">Tokens in</th>
                    <th style="padding: 0.5rem 0.75rem; text-align: right; font-size: 0.75rem; color: var(--text-tertiary);">Tokens out</th>
                    <th style="padding: 0.5rem 0.75rem; text-align: right; font-size: 0.75rem; color: var(--text-tertiary);">Latency</th>
                    <th style="padding: 0.5rem 0.75rem; text-align: left; font-size: 0.75rem; color: var(--text-tertiary);">Status</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

function renderCallsError(message) {
    const container = document.getElementById('llm-calls-list');
    if (!container) return;
    container.innerHTML = `
        <div style="padding: 2rem; text-align: center; color: var(--text-secondary);">
            <p>Unable to load LLM calls</p>
            <p style="font-size: 0.8rem; color: var(--text-tertiary);">${escapeHtml(message)}</p>
        </div>
    `;
}

// ============================================================================
// Helpers
// ============================================================================
function formatTokens(tokens) {
    tokens = tokens || 0;
    if (tokens >= 1000000) return (tokens / 1000000).toFixed(1) + 'M';
    if (tokens >= 1000) return (tokens / 1000).toFixed(1) + 'K';
    return String(tokens);
}

function formatTime(timestamp) {
    try {
        const date = new Date(timestamp);
        const now = new Date();
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch (e) {
        return String(timestamp);
    }
}

function escapeHtml(text) {
    if (text == null) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
