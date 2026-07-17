// Fleet observability — "Observability" console panel (embedded Phoenix).
//
// Part of the OTel-native pivot (#32): the console no longer renders traces
// itself. The Observability tab is now a thin embed of the self-hosted Phoenix
// UI that the host serves same-origin at `/phoenix/`.
//
// On mount we probe availability by minting an embed session:
//   POST /api/host/phoenix/session  (authenticated via the console's standard
//   header auth) sets an HttpOnly cookie scoped to `/phoenix` — no credentials
//   ever appear in a URL. On success we mount a full-bleed
//   `<iframe src="/phoenix/">` (the cookie authenticates it; same-origin, no
//   CSP work). On 503/failure we render a friendly notice instead of a broken
//   frame.
//
// The old Swimlane/Runs sub-views and the custom event store they read from are
// gone: the emitters emit OTel only (hook 0.11.0 + talon#69) and the store/routes
// were retired in the store-deprecation issue. This embed is the only UI module.
//
// Registered via HostFeature.get_ui_contributions() as the single module in
// UIContributions.modules. `capability: null` → host-always-on (sovereign
// #2460), so the nav tab always renders.

import { registerPanel } from "/js/ui-ext/panels.js";
import API from "/js/api.js";

const PHOENIX_SESSION_PATH = "/api/host/phoenix/session";
const PHOENIX_URL = "/phoenix/";

// ── View / mount ──────────────────────────────────────────────

export function mount(container) {
  ensureStyles();

  let destroyed = false;

  container.innerHTML = `<div class="obs-panel"><div class="obs-notice">Connecting to Phoenix…</div></div>`;
  const panelEl = container.querySelector(".obs-panel");

  function renderIframe() {
    if (destroyed || !panelEl) return;
    // Full-bleed embed. The embed cookie set by the session mint authenticates
    // it; same-origin `/phoenix/` needs no credentials in the URL.
    const iframe = document.createElement("iframe");
    iframe.className = "obs-frame";
    iframe.src = PHOENIX_URL;
    iframe.setAttribute("title", "Phoenix");
    panelEl.replaceChildren(iframe);
  }

  function renderNotice() {
    if (destroyed || !panelEl) return;
    panelEl.innerHTML = `
      <div class="obs-notice">
        <div class="obs-notice__title">Phoenix is not running on this host</div>
        <div class="obs-notice__body">
          Install <code>kestrel-sovereign[phoenix]</code> and restart, or set
          <code>KESTREL_PHOENIX_ENABLED=1</code>.
        </div>
      </div>`;
  }

  // Probe availability by minting an embed session (sets the HttpOnly `/phoenix`
  // cookie). Success → embed the UI; any failure → friendly notice.
  API.requestHost(PHOENIX_SESSION_PATH, { method: "POST" })
    .then(() => renderIframe())
    .catch(() => renderNotice());

  return {
    destroy() {
      destroyed = true;
    },
  };
}

export default { id: "observability", title: "Observability", capability: null, mount };

// ── Styles (scoped, theme-aware) ──────────────────────────────

let stylesInjected = false;
function ensureStyles() {
  if (stylesInjected || typeof document === "undefined") return;
  const style = document.createElement("style");
  style.setAttribute("data-observability", "");
  style.textContent = `
    .obs-panel { display:flex; flex-direction:column; height:100%; color:var(--color-text,#e2e8f0); }
    .obs-frame { flex:1; min-height:0; width:100%; border:0; }
    .obs-notice { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
                  gap:8px; padding:24px; text-align:center; color:var(--color-text-muted,#94a3b8); }
    .obs-notice__title { font-size:15px; font-weight:600; color:var(--color-text,#e2e8f0); }
    .obs-notice__body { max-width:520px; line-height:1.5; }
    .obs-notice code { font-family:ui-monospace,monospace; background:var(--color-surface,#1e293b);
                       border:1px solid var(--color-border,#334155); border-radius:4px; padding:1px 5px; }
  `;
  document.head.appendChild(style);
  stylesInjected = true;
}

// ── Registration via the host ui-ext panel registry ──────────
//
// A single always-on top-level panel. `registerPanel` expects a `panelId` and a
// lazy `render(bodyEl)` callback; `mount(container)` fills the panel body and
// embeds Phoenix (or the notice) on first activation.

registerPanel({
  panelId: "observability",
  label: "Observability",
  render: mount,
});
