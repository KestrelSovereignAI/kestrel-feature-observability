// Fleet observability — unified "Observability" console panel.
//
// The single top-level host panel for the whole observability domain. It renders
// an internal sub-nav (pill bar) and swaps the active sub-view — Swimlane, Runs,
// and room for more — into a shared content area. Each sub-view is a module that
// exports `mount(container)` (returning a handle with `destroy()`); the container
// mounts the active view and unmounts it on switch/teardown, so only one view is
// live at a time.
//
// Registered via HostFeature.get_ui_contributions() as the *single* module in
// UIContributions.modules; the view modules (swimlane.js, runs.js) are imported
// here, not separately registered as top-level panels. `capability: null` →
// host-always-on (sovereign #2460), so the nav tab always renders.
//
// Adding a future view is one entry in VIEWS below — no new top-level tab.

import { registerPanel } from "/js/ui-ext/panels.js";
import { mount as mountSwimlane } from "./swimlane.js";
import { mount as mountRuns } from "./runs.js";

// ── Sub-views ─────────────────────────────────────────────────
//
// Extensible registry: adding a view = one entry (id + label + mount).

const VIEWS = [
  { id: "swimlane", label: "Swimlane", mount: mountSwimlane },
  { id: "runs", label: "Runs", mount: mountRuns },
];

const STORAGE_KEY = "kestrel.observability.subtab";

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Persisted selection survives reloads. URL hash (#observability/<id>) wins so a
// link is shareable; localStorage is the fallback; else the first view.
function readPersistedViewId() {
  try {
    const hash = (typeof location !== "undefined" && location.hash) || "";
    const m = /#observability\/([\w-]+)/.exec(hash);
    if (m && VIEWS.some((v) => v.id === m[1])) return m[1];
  } catch {
    /* ignore */
  }
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && VIEWS.some((v) => v.id === stored)) return stored;
  } catch {
    /* ignore */
  }
  return VIEWS[0].id;
}

function persistViewId(id) {
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    /* ignore */
  }
  try {
    if (typeof location !== "undefined") {
      location.hash = `observability/${id}`;
    }
  } catch {
    /* ignore */
  }
}

// ── View / mount ──────────────────────────────────────────────

export function mount(container) {
  ensureStyles();

  let activeId = readPersistedViewId();
  let handle = null; // handle returned by the active view's mount()
  let destroyed = false;

  container.innerHTML = `
    <div class="obs-panel">
      <nav class="obs-subnav" role="tablist">
        ${VIEWS.map(
          (v) => `
          <button class="obs-subnav__tab" role="tab" data-view="${escapeHtml(v.id)}">
            ${escapeHtml(v.label)}
          </button>`
        ).join("")}
      </nav>
      <div class="obs-content" data-obs-content></div>
    </div>`;

  const contentEl = container.querySelector("[data-obs-content]");

  function unmountActive() {
    try {
      handle?.destroy?.();
    } catch {
      /* ignore */
    }
    handle = null;
    if (contentEl) contentEl.innerHTML = "";
  }

  function mountView(id) {
    const view = VIEWS.find((v) => v.id === id) || VIEWS[0];
    activeId = view.id;
    unmountActive();
    // Reflect the active tab.
    container.querySelectorAll("[data-view]").forEach((btn) => {
      btn.classList.toggle("obs-subnav__tab--active", btn.dataset.view === activeId);
    });
    handle = view.mount(contentEl);
  }

  function switchTo(id) {
    if (destroyed || id === activeId) return;
    persistViewId(id);
    mountView(id);
  }

  container.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => switchTo(btn.dataset.view));
  });

  mountView(activeId);

  return {
    destroy() {
      destroyed = true;
      unmountActive();
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
    .obs-subnav { display:flex; align-items:center; gap:4px; padding:6px 12px;
                  border-bottom:1px solid var(--color-border,#334155); }
    .obs-subnav__tab { background:transparent; color:var(--color-text-muted,#94a3b8);
                       border:1px solid transparent; border-radius:999px; padding:4px 14px;
                       cursor:pointer; font-size:13px; font-weight:600; }
    .obs-subnav__tab:hover { background:var(--color-surface,#1e293b); color:var(--color-text,#e2e8f0); }
    .obs-subnav__tab--active { background:var(--color-accent,#818cf8); color:#0b1120;
                               border-color:var(--color-accent,#818cf8); }
    .obs-content { flex:1; min-height:0; overflow:hidden; }
  `;
  document.head.appendChild(style);
  stylesInjected = true;
}

// ── Registration via the host ui-ext panel registry ──────────
//
// A single always-on top-level panel. `registerPanel` expects a `panelId` and a
// lazy `render(bodyEl)` callback; `mount(container)` fills the panel body and
// renders the sub-nav on first activation.

registerPanel({
  panelId: "observability",
  label: "Observability",
  render: mount,
});
