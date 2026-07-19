// Fleet observability — "Observability" console panel.
//
// Part of the OTel pivot (#32): a single top-level host panel with a two-item
// sub-nav (#46, reintroducing the pre-#37 subtab container pattern minimally):
//
//   Navigator (default) — the hierarchical fleet drill-down
//     (Tenant → Fleet → Agent → Subagent → Session → Turn → Events) rendered
//     kestrel-native over Phoenix's GraphQL (see ./navigator.js).
//   Phoenix — the curated same-origin Phoenix embed (unchanged from #41),
//     which the navigator's per-trace "open in Phoenix" links deep-link into.
//
// Selection persists (URL hash `#observability/<id>` wins so links are
// shareable; localStorage is the fallback; else Navigator).
//
// ── Phoenix embed subtab ──
// On mount we probe availability by minting an embed session:
//   POST /api/host/phoenix/session  (authenticated via the console's standard
//   header auth) sets an HttpOnly cookie scoped to `/phoenix` — no credentials
//   ever appear in a URL. On success we mount a full-bleed
//   `<iframe src="/phoenix/">` (the cookie authenticates it; same-origin, no
//   CSP work). On 503/failure we render a friendly notice instead of a broken
//   frame.
//
// Curated embed (obs#41): Phoenix ships its full product surface (Playground,
// Prompts, Datasets, Experiments, support/docs/upsell links) — noise for the
// Kestrel observability use-case. Because the iframe is SAME-ORIGIN (host proxy)
// we curate it from the wrapper:
//   1. Deep-link the iframe to the traces view of the `kestrel-fleet` project
//      (the project the per-agent hook stamps via `openinference.project.name`,
//      see tracing.py `DEFAULT_OTEL_PROJECT`). The name→ID lookup goes through
//      the same-origin GraphQL API (authenticated by the embed cookie). Two
//      independent safety nets fall back to the bare `/phoenix/` entry: a lookup
//      failure (network / non-200 / missing project / malformed payload) picks
//      the bare src up front, and — because Phoenix is an SPA that answers HTTP
//      200 for *any* path — a structurally-valid-but-wrong route can't 404, so
//      the iframe load handler also detects Phoenix's in-app "not found" and
//      resets the src to the bare entry.
//   2. After load, inject a hide-stylesheet + a text/href pass into the iframe
//      document that drops the non-observability nav modules, keeping
//      Projects / Traces / Sessions. A MutationObserver on the nav re-applies it
//      across Phoenix's SPA navigations. Selectors are href/aria/text-based so
//      they survive Phoenix's hashed CSS-module class names (pinned 17.7.x).
// Curation is best-effort and NON-FATAL: any error leaves a plain uncurated
// embed rather than a broken panel. Set `localStorage kestrel.observability.
// curated = 0` to disable curation (and deep-linking) for debugging.
//
// The old Swimlane/Runs sub-views and the custom event store they read from are
// gone: the emitters emit OTel only (hook 0.11.0 + talon#69) and the store/routes
// were retired in the store-deprecation issue.
//
// Registered via HostFeature.get_ui_contributions() as the single module in
// UIContributions.modules (navigator.js is imported here, not separately
// registered). `capability: null` → host-always-on (sovereign #2460), so the
// nav tab always renders.

import { registerPanel } from "/js/ui-ext/panels.js";
import API from "/js/api.js";
import { mount as mountNavigator } from "./navigator.js";

const PHOENIX_SESSION_PATH = "/api/host/phoenix/session";
const PHOENIX_URL = "/phoenix/";
const PHOENIX_GRAPHQL_URL = "/phoenix/graphql";

// Default Phoenix project the embed deep-links to. MUST match the emitter's
// default in tracing.py (`DEFAULT_OTEL_PROJECT` == "kestrel-fleet") so the
// curated panel opens the very project the per-agent hook populates. Overridable
// per mount via a `data-project` attribute on the panel container.
const DEFAULT_PROJECT = "kestrel-fleet";

// localStorage switch — `kestrel.observability.curated = 0` disables curation
// and deep-linking (debug: see the full uncurated Phoenix surface). Any other
// value (or unset) keeps curation on.
const CURATED_FLAG = "kestrel.observability.curated";

// Non-observability nav modules to hide. Matched by href substring (survives
// hashed classes) AND by exact visible text (some links carry no stable href).
// Projects / Traces / Sessions are intentionally NOT listed → kept.
const HIDE_HREFS = ["/playground", "/prompts", "/datasets", "/experiments"];
const HIDE_TEXTS = new Set([
  "playground",
  "prompts",
  "datasets",
  "experiments",
  "support",
  "docs",
  "documentation",
]);

// ── Curation helpers ──────────────────────────────────────────

function curationEnabled() {
  try {
    return window.localStorage.getItem(CURATED_FLAG) !== "0";
  } catch (_e) {
    return true; // storage blocked → default to curated
  }
}

function projectName(container) {
  const fromData = container && container.dataset && container.dataset.project;
  const trimmed = (fromData || "").trim();
  return trimmed || DEFAULT_PROJECT;
}

// Resolve the project name → its traces route via the same-origin GraphQL API
// (Phoenix 17.7.x routes projects by internal ID, not name; the embed cookie
// authenticates the request through the host proxy). Returns the deep-link URL,
// or throws on any failure so the caller can fall back to the bare entry.
async function resolveProjectTracesUrl(name) {
  const query =
    "query KestrelProjects { projects(first: 1000) { edges { node { id name } } } }";
  const resp = await fetch(PHOENIX_GRAPHQL_URL, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!resp.ok) throw new Error(`phoenix graphql HTTP ${resp.status}`);
  const payload = await resp.json();
  const edges =
    (payload && payload.data && payload.data.projects && payload.data.projects.edges) || [];
  const match = edges.find((e) => e && e.node && e.node.name === name);
  if (!match) throw new Error(`phoenix project not found: ${name}`);
  // Phoenix (17.7.x) routes projects by their Relay global node ID; the bare
  // project route lands on the project's default (traces/spans) view. This route
  // *shape* is the one external assumption we can't check from GraphQL — if it's
  // wrong Phoenix renders its in-app not-found (still HTTP 200), which the iframe
  // load handler detects and falls back from. Keep this a plain URL build (no
  // fetch): validation happens at render time, not here.
  return `${PHOENIX_URL}projects/${encodeURIComponent(match.node.id)}`;
}

// Choose the iframe src: the deep-linked traces route when curating, else the
// bare Phoenix entry. This handles *name→ID resolution* failures ONLY — network,
// non-200, missing project, or malformed payload each degrade to `/phoenix/`. It
// does NOT (and cannot) catch a resolved-but-wrong route: Phoenix serves its SPA
// shell with HTTP 200 for any path, so there's no status to inspect here. That
// case is caught later by the iframe load handler (see `deepLinkNotFound`).
async function chooseSrc(name, curate) {
  if (!curate) return PHOENIX_URL;
  try {
    return await resolveProjectTracesUrl(name);
  } catch (_e) {
    return PHOENIX_URL;
  }
}

// Runtime safety net for a resolved-but-wrong deep-link route. Phoenix answers
// HTTP 200 for every path (SPA shell), so a bad `/phoenix/projects/{id}` route
// renders its in-app "not found" rather than a 404 we could catch at fetch time.
// We can read the frame same-origin (same access curation uses), so detect that
// state and let the caller reset the src to the known-good bare entry.
//
// Deliberately conservative — match only the router-level not-found phrase,
// never an empty-data state ("No traces found") or a `404` that legitimately
// appears in trace/status content. A false negative is harmless (we just leave
// the deep link, no worse than before); a false positive would nuke a valid
// view, so we bias hard toward not firing.
function deepLinkNotFound(doc) {
  if (!doc || !doc.body) return false;
  const text = (doc.body.textContent || "").toLowerCase();
  return text.includes("page not found");
}

// The Phoenix side-nav (a <nav> in 17.7.x). Text/external hiding is scoped here
// so it can't touch legitimate external/action links inside trace content.
function navRoot(doc) {
  return doc.querySelector("nav");
}

// Hide a matched nav element — prefer hiding its list-item/nav-item wrapper so
// the whole row disappears, falling back to the element itself.
function hideNavItem(el) {
  const wrapper = el.closest("li, [role='listitem'], [role='menuitem']") || el;
  wrapper.style.setProperty("display", "none", "important");
}

// Inject the href-based hide-stylesheet once. It lives in <head>, which Phoenix's
// SPA router leaves untouched, so href-matched modules stay hidden across
// navigations without re-injection.
//
// Two rules per module, kept SEPARATE on purpose:
//   1. `a[href*=…]` — hides the link itself. The baseline: works in every
//      browser and is the fallback where `:has()` is unsupported.
//   2. `nav …:has(a[href*=…])` — hides the whole nav row wrapper (li/menuitem)
//      so it collapses instead of leaving an empty gap, mirroring the JS
//      `hideNavItem()` pass. Scoped to `nav` so `:has()` can't reach into trace
//      content. Kept in its own rule (not comma-joined with rule 1) because a
//      browser that can't parse `:has()` would otherwise drop the entire
//      selector list — including the baseline anchor rule.
function ensureHideStylesheet(doc) {
  if (doc.getElementById("kestrel-obs-curation")) return;
  const style = doc.createElement("style");
  style.id = "kestrel-obs-curation";
  const anchorSel = HIDE_HREFS.map((h) => `a[href*="${h}"]`).join(",\n");
  const wrapperSel = HIDE_HREFS.map(
    (h) => `nav :where(li, [role="listitem"], [role="menuitem"]):has(a[href*="${h}"])`,
  ).join(",\n");
  style.textContent =
    `${anchorSel} { display: none !important; }\n` +
    `${wrapperSel} { display: none !important; }`;
  (doc.head || doc.documentElement).appendChild(style);
}

// Text/external pass for links CSS can't target (support/docs/upsell carry no
// stable in-app href). Scoped to the nav so it never hides legitimate external
// or action links inside trace content; idempotent (re-hiding is a no-op). When
// no <nav> is found we fall back to a text-only sweep of the document (never the
// broad external-link match, which is only safe scoped to the nav).
function hideByTextAndExternal(doc) {
  const nav = navRoot(doc);
  const root = nav || doc;
  const matchExternal = Boolean(nav);
  root.querySelectorAll("a").forEach((a) => {
    const href = a.getAttribute("href") || "";
    const text = (a.textContent || "").trim().toLowerCase();
    const isExternal =
      matchExternal &&
      /^https?:\/\//i.test(href) &&
      !href.includes(doc.location.host);
    if (isExternal || HIDE_TEXTS.has(text)) {
      hideNavItem(a);
    }
  });
}

// Apply the full curation pass to the iframe document.
function curateDocument(doc) {
  ensureHideStylesheet(doc);
  hideByTextAndExternal(doc);
}

// Watch the nav (or body) and re-apply curation across SPA navigations, where
// Phoenix re-renders nav items React drops our inline `display:none`. Debounced
// via the iframe's own rAF so bursts coalesce. Returns the observer (for
// teardown) or null when unsupported.
function observeNav(doc, apply) {
  const win = doc.defaultView;
  const Observer = win && win.MutationObserver;
  if (typeof Observer !== "function") return null;
  const target = doc.querySelector("nav") || doc.body;
  if (!target) return null;

  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    const run = () => {
      scheduled = false;
      try {
        apply(doc);
      } catch (_e) {
        /* non-fatal: leave the embed as-is */
      }
    };
    if (typeof win.requestAnimationFrame === "function") {
      win.requestAnimationFrame(run);
    } else {
      run();
    }
  };

  const observer = new Observer(schedule);
  observer.observe(target, { childList: true, subtree: true });
  return observer;
}

// ── Phoenix embed sub-view ────────────────────────────────────
//
// The curated embed, unchanged in behavior (#41) beyond one wiring point for
// the navigator (#46): `opts.traceUrl` overrides the project deep-link so
// "open in Phoenix" lands on a specific trace. The in-app-not-found fallback
// applies to that URL exactly as it does to the project deep-link.

function mountPhoenix(container, opts = {}) {
  ensureStyles();

  let destroyed = false;
  let observer = null;

  const curate = curationEnabled();
  const project = ((opts.project || "").trim()) || DEFAULT_PROJECT;
  const traceUrl = opts.traceUrl || null;

  container.innerHTML = `<div class="obs-embed"><div class="obs-notice">Connecting to Phoenix…</div></div>`;
  const panelEl = container.querySelector(".obs-embed");

  function curateIframe(iframe) {
    // Best-effort, non-fatal: same-origin access can throw (cross-origin edge,
    // detached frame) → any failure leaves the plain uncurated embed.
    if (destroyed || !curate) return;
    let doc;
    try {
      doc = iframe.contentDocument;
    } catch (_e) {
      return; // cross-origin / inaccessible → uncurated embed
    }
    if (!doc) return;
    try {
      curateDocument(doc);
      if (!observer) observer = observeNav(doc, curateDocument);
    } catch (_e) {
      /* non-fatal: uncurated embed */
    }
  }

  async function renderIframe() {
    if (destroyed || !panelEl) return;
    // Full-bleed embed. The embed cookie set by the session mint authenticates
    // it; same-origin `/phoenix/` needs no credentials in the URL.
    const iframe = document.createElement("iframe");
    iframe.className = "obs-frame";
    iframe.setAttribute("title", "Phoenix");
    // Fires at most once: the deep-link → bare-entry fallback is one-way, and the
    // guard stops a pathological not-found on the bare entry from looping.
    let fellBack = false;
    // Re-curate on every load (initial navigation to the deep link, and any
    // full-frame reload); SPA route changes are handled by the MutationObserver.
    iframe.addEventListener("load", () => {
      if (destroyed) return;
      // A resolved-but-wrong deep-link route renders Phoenix's in-app not-found
      // (HTTP 200 — no fetch error to catch up front). Detect it same-origin and
      // fall back once to the known-good bare entry, whose own load then curates.
      // `iframe.src` is absolute here, so a non-bare src means we deep-linked.
      if (!fellBack && curate && !iframe.src.endsWith(PHOENIX_URL)) {
        let doc = null;
        try {
          doc = iframe.contentDocument;
        } catch (_e) {
          doc = null; // cross-origin / inaccessible → can't tell; leave as-is
        }
        if (doc && deepLinkNotFound(doc)) {
          fellBack = true;
          iframe.src = PHOENIX_URL; // its load event curates the bare embed
          return;
        }
      }
      curateIframe(iframe);
    });

    // A navigator-provided trace deep-link (#46) wins over project resolution;
    // it is subject to the same load-time not-found fallback above.
    const src = traceUrl || (await chooseSrc(project, curate));
    if (destroyed || !panelEl) return;
    iframe.src = src;
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
      if (observer) {
        try {
          observer.disconnect();
        } catch (_e) {
          /* ignore */
        }
        observer = null;
      }
    },
  };
}

// ── Sub-views (#46: subtab container, reintroduced minimally) ─
//
// Extensible registry: adding a view = one entry (id + label + mount). Each
// view module exports `mount(container, opts)` returning a handle with
// `destroy()`; the container mounts the active view and unmounts it on
// switch/teardown, so only one view is live at a time. Navigator is first →
// the default on a fresh console load.

const VIEWS = [
  { id: "navigator", label: "Navigator", mount: mountNavigator },
  { id: "phoenix", label: "Phoenix", mount: mountPhoenix },
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
// link is shareable; localStorage is the fallback; else the first view
// (Navigator).
function readPersistedViewId() {
  try {
    const hash = (typeof location !== "undefined" && location.hash) || "";
    const m = /#observability\/([\w-]+)/.exec(hash);
    if (m && VIEWS.some((v) => v.id === m[1])) return m[1];
  } catch (_e) {
    /* ignore */
  }
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && VIEWS.some((v) => v.id === stored)) return stored;
  } catch (_e) {
    /* ignore */
  }
  return VIEWS[0].id;
}

function persistViewId(id) {
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch (_e) {
    /* ignore */
  }
  try {
    if (typeof location !== "undefined") {
      location.hash = `observability/${id}`;
    }
  } catch (_e) {
    /* ignore */
  }
}

// ── View / mount ──────────────────────────────────────────────

export function mount(container) {
  ensureStyles();

  const project = projectName(container);
  let activeId = readPersistedViewId();
  let handle = null; // handle returned by the active view's mount()
  let destroyed = false;
  let pendingTraceUrl = null; // set by the navigator's "open in Phoenix" (#46)

  container.innerHTML = `
    <div class="obs-panel">
      <nav class="obs-subnav" role="tablist">
        ${VIEWS.map(
          (v) => `
          <button type="button" class="obs-subnav__tab" role="tab" data-view="${escapeHtml(v.id)}">
            ${escapeHtml(v.label)}
          </button>`,
        ).join("")}
      </nav>
      <div class="obs-content" data-obs-content></div>
    </div>`;

  const contentEl = container.querySelector("[data-obs-content]");

  function unmountActive() {
    try {
      handle?.destroy?.();
    } catch (_e) {
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
    const opts = { project };
    if (view.id === "phoenix") {
      // One-shot: a pending navigator deep-link rides along on this mount only.
      opts.traceUrl = pendingTraceUrl;
      pendingTraceUrl = null;
    } else if (view.id === "navigator") {
      opts.openTrace = openTrace;
    }
    handle = view.mount(contentEl, opts);
  }

  function switchTo(id) {
    if (destroyed || id === activeId) return;
    persistViewId(id);
    mountView(id);
  }

  // Navigator → Phoenix trace deep-link (#46): land the Phoenix subtab on the
  // exact trace the navigator row points at.
  function openTrace(traceUrl) {
    if (destroyed || !traceUrl) return;
    pendingTraceUrl = traceUrl;
    if (activeId === "phoenix") {
      mountView("phoenix"); // already on Phoenix → remount onto the trace
    } else {
      switchTo("phoenix");
    }
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
    .obs-embed { display:flex; flex-direction:column; height:100%; }
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
// renders the sub-nav (Navigator | Phoenix) on first activation.

registerPanel({
  panelId: "observability",
  label: "Observability",
  render: mount,
});
