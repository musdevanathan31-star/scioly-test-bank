"""
Shared CSS/JS for every page across both Flask processes (`review_app.py`
and `admin_app.py`). Pure string constants, no side effects — safe for
either app to import without triggering the other's module-level startup
behavior (e.g. `review_app.py`'s job-recovery sweep).

Each app registers these as Jinja globals so templates can do
`{{ common_css|safe }}` / `{{ common_js|safe }}` regardless of which
process is rendering them.
"""

from __future__ import annotations

COMMON_CSS = """
:root{
  --bg:#fff; --bg-alt:#f7f7f8; --bg-card:#fff; --bg-input:#fff;
  --fg:#222; --fg-soft:#444; --muted:#888;
  --line:#e2e2e2; --line-soft:#f0f0f0;
  --accent:#0066cc; --ok:#1f8a4d; --warn:#b86b00; --bad:#c0392b;
  --hover:#f0f0f0; --row-hover:#fafbfc;
  --header-bg:#fff;
}
*{box-sizing:border-box}
body{font:14px -apple-system,system-ui,Segoe UI,Roboto,sans-serif;
  margin:0;color:var(--fg);background:var(--bg-alt)}
header{padding:10px 18px;background:var(--header-bg);
  border-bottom:1px solid var(--line);
  display:flex;gap:12px;align-items:center;position:sticky;top:0;z-index:10}
header h1{margin:0;font-size:15px;font-weight:600;flex:1;color:var(--fg)}
header a{color:var(--fg-soft)}
button{padding:5px 11px;border:1px solid var(--line);background:var(--bg-card);
  color:var(--fg);border-radius:4px;cursor:pointer;font-size:13px;line-height:1.4}
button:hover{background:var(--hover)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.primary:hover{filter:brightness(1.08)}
button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}
button.ghost{background:transparent;border:none;color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
a{color:var(--accent);text-decoration:none}
.muted{color:var(--muted);font-size:12px}

/* Badge semantics: four meanings (success/done, failure/error,
   in-progress/needs-attention, inactive/no-status), independent of
   whatever a given page historically called them. `.processed`/`.fresh`/
   `.edited` are kept as aliases of `.badge-ok`/`.badge-bad`/`.badge-warn`
   so existing templates keep working unmigrated; new/migrated markup
   should prefer the `.badge-*` names directly since the old names don't
   describe their meaning (`.fresh` is the *red/bad* one, confusingly). */
.badge{display:inline-block;padding:1px 8px;border-radius:10px;
  font-size:11px;font-weight:600;letter-spacing:.2px}
.badge.processed,.badge.badge-ok{background:#e3f4e9;color:var(--ok)}
.badge.fresh,.badge.badge-bad{background:#fde7e6;color:var(--bad)}
.badge.edited,.badge.badge-warn{background:#fff3d7;color:var(--warn)}
.badge.badge-neutral{background:#f0f0f0;color:var(--muted)}

.banner{padding:8px 18px;background:#fff8e1;border-bottom:1px solid #f3d870;
  font-size:13px;color:#5a4a00}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--line);
  border-top-color:var(--accent);border-radius:50%;
  animation:spin .8s linear infinite;vertical-align:middle}
@keyframes spin{100%{transform:rotate(360deg)}}
.skeleton{background:linear-gradient(90deg, var(--line-soft) 25%,
  var(--line) 50%, var(--line-soft) 75%);background-size:200% 100%;
  animation:skel 1.4s infinite;border-radius:4px;color:transparent}
@keyframes skel{0%{background-position:200% 0}100%{background-position:-200% 0}}
.skeleton-row{height:22px;margin:6px 0}

/* Toast log */
.toast-host{position:fixed;bottom:16px;right:16px;z-index:200;
  display:flex;flex-direction:column;gap:8px;max-width:340px}
.toast{background:var(--bg-card);color:var(--fg);border:1px solid var(--line);
  border-left:3px solid var(--accent);box-shadow:0 4px 14px rgba(0,0,0,.18);
  padding:8px 12px;border-radius:6px;font-size:13px;
  animation:toastin .22s ease;opacity:1;transition:opacity .25s}
.toast.err{border-left-color:var(--bad)}
.toast.warn{border-left-color:var(--warn)}
.toast.ok{border-left-color:var(--ok)}
.toast.fade{opacity:0}
@keyframes toastin{from{transform:translateX(20px);opacity:0}
  to{transform:translateX(0);opacity:1}}
.toast-history{position:fixed;bottom:16px;right:16px;z-index:201;
  background:var(--bg-card);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.25);padding:10px 14px;
  max-height:60vh;width:380px;overflow-y:auto;display:none}
.toast-history.on{display:block}
.toast-history h4{margin:0 0 6px 0;font-size:13px;color:var(--fg)}
.toast-history .item{font-size:12px;padding:5px 0;border-bottom:1px solid var(--line-soft);
  color:var(--fg-soft)}
.toast-history .item .ts{color:var(--muted);font-size:10px;margin-right:6px;
  font-variant-numeric:tabular-nums}
.toast-history .item.err{color:var(--bad)}
.toast-history .empty{color:var(--muted);font-size:12px;padding:6px 0}
.history-btn{background:transparent;border:1px solid var(--line);font-size:11px;
  color:var(--muted);padding:2px 7px;border-radius:10px;cursor:pointer}
.history-btn:hover{background:var(--hover);color:var(--fg-soft)}
/* Running Anthropic-spend badge. Polls /api/usage every 30s. */
.cost-badge{display:inline-block;background:#fff8e1;color:#7a5a00;
  border:1px solid #f3d870;font-size:11px;padding:2px 8px;border-radius:10px;
  font-weight:600;font-variant-numeric:tabular-nums;cursor:default}
.cost-badge.over{background:#fde7e6;color:#a02020;border-color:#f3a0a0}

/* Floating action bar (sticky bottom-right) */
.floating-actions{position:fixed;bottom:18px;right:18px;z-index:150;
  display:flex;gap:6px;background:var(--bg-card);border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;box-shadow:0 4px 18px rgba(0,0,0,.22)}
.floating-actions button{font-size:13px}

/* Job progress modal (jobs.py) — shared by every Tier-1 long-running action:
   PDF reprocess/upload-extract, scio.ly download/scrape, LLM generation,
   wiki scrape. See openJobProgress() in COMMON_JS. */
.job-progress-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
  z-index:500;align-items:center;justify-content:center;padding:24px}
.job-progress-modal .box{background:var(--bg-card);color:var(--fg);border-radius:8px;
  padding:18px 22px;width:620px;max-width:95vw;max-height:85vh;
  display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.45)}
.job-progress-modal .hdr{display:flex;justify-content:space-between;
  align-items:center;margin-bottom:8px}
.job-progress-modal .hdr h3{margin:0;font-size:15px}
.jp-phase{font-size:13px;color:var(--fg-soft);margin-bottom:8px;
  text-transform:capitalize}
.jp-bar-wrap{height:8px;background:var(--line-soft);border-radius:4px;
  overflow:hidden;margin-bottom:6px}
.jp-bar{height:100%;background:var(--accent);width:0%;
  transition:width .3s ease}
.jp-bar.failed{background:var(--bad)}
.jp-bar.cancelled{background:var(--warn)}
.jp-meta{font-size:11px;color:var(--muted);margin-bottom:10px;
  font-variant-numeric:tabular-nums}
.jp-console{background:#1b1b1f;color:#d8d8e0;font-family:ui-monospace,Consolas,monospace;
  font-size:12px;line-height:1.5;padding:10px 12px;border-radius:6px;
  flex:1;overflow-y:auto;min-height:160px;max-height:40vh;white-space:pre-wrap;
  word-break:break-word;margin:0 0 10px 0}
.jp-actions{display:flex;justify-content:flex-end;gap:8px}

/* Generic modal component, generalizing the hand-rolled overlays scattered
   across templates (snapshots/compare/diagram modals, edit-user/edit-event
   modals, password-confirm modals). z-index ladder, low to high (each one
   can be triggered from within a lower one, so they stack in this order):
   .dropdown-panel (60) < .side-drawer (120) < .floating-actions (150)
   < .toast-host (200) < .toast-history (201) < .modal-backdrop (300)
   < .job-progress-modal (500). */
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
  z-index:300;align-items:center;justify-content:center;padding:24px}
.modal-backdrop.on{display:flex}
.modal-box{background:var(--bg-card);color:var(--fg);border-radius:8px;
  padding:18px 22px;width:480px;max-width:95vw;max-height:90vh;overflow:auto;
  box-shadow:0 12px 40px rgba(0,0,0,.45)}
.modal-box.wide{width:620px}
.modal-box.narrow{width:340px}
.modal-box .hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.modal-box .hdr h3{margin:0;font-size:15px}
.modal-box .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}

/* Lightweight anchored dropdown — for non-blocking, non-destructive popups
   (an export menu, a multi-field upload form) where a full-screen modal
   backdrop would be heavier than the action warrants. The trigger button's
   container needs `position:relative` for the `top:100%` anchor to work. */
.dropdown-panel{display:none;position:absolute;top:100%;left:0;margin-top:4px;
  background:var(--bg-card);border:1px solid var(--line);border-radius:6px;
  box-shadow:0 6px 20px rgba(0,0,0,.15);padding:12px 14px;z-index:60;min-width:260px}
.dropdown-panel.on{display:block}

/* Clusters related buttons inside a `.toolbar`/`header` row with a thin
   divider, instead of one flat undifferentiated row of buttons. */
.toolbar-group{display:flex;align-items:center;gap:6px;padding:0 10px;
  border-right:1px solid var(--line-soft)}
.toolbar-group:last-of-type{border-right:none}
.toolbar-group .label{font-size:11px;color:var(--muted);margin-right:2px;
  text-transform:uppercase;letter-spacing:.3px}

/* Standardizes the dominant "label | input" form layout (160px label
   column) so pages don't each invent their own column width. */
.form-grid{display:grid;grid-template-columns:160px 1fr;gap:8px 14px;
  align-items:start;font-size:13px}

/* Page navigation (prev/next + page-number input) and a test/key/sheet
   target toggle — originally local to review.html, now shared since
   event_index.html's PDF preview drawer needs both too. */
.page-nav{display:flex;align-items:center;gap:6px}
.page-nav input{width:54px;text-align:center;padding:3px 4px;
  border:1px solid var(--line);border-radius:4px;font:inherit}
.target-toggle{display:flex;border:1px solid var(--line);border-radius:4px;overflow:hidden}
.target-toggle button{border:none;border-radius:0;padding:4px 10px;font-size:12px}
.target-toggle button.on{background:var(--accent);color:#fff}

/* Right-side slide-in drawer — mirrors review.html's left-side
   .outline-drawer (same transform/transition/`.on` toggle idiom), flipped
   to the right edge. Used by event_index.html's PDF preview panel. */
.side-drawer{position:fixed;top:0;bottom:0;right:0;width:520px;max-width:92vw;
  background:var(--bg-card);border-left:1px solid var(--line);
  overflow-y:auto;z-index:120;transform:translateX(100%);
  transition:transform .2s ease;box-shadow:-2px 0 16px rgba(0,0,0,.15)}
.side-drawer.on{transform:translateX(0)}

/* Navicon menu (templates/_user_badge.html) — the app's single consolidated
   navigation entry point, replacing the per-template scattered links it
   used to take to reach the same destinations. A .dropdown-panel variant
   with its own link/accordion styling. */
.nav-panel{min-width:230px}
.nav-identity{padding:6px 10px 8px;margin-bottom:4px;border-bottom:1px solid var(--line);
  font-size:12px;color:var(--fg-soft)}
.nav-panel .nav-link{display:block;padding:6px 10px;border-radius:4px;color:var(--fg);
  text-decoration:none;font-size:13px}
.nav-panel .nav-link:hover{background:var(--hover)}
.nav-group-toggle{display:block;width:100%;text-align:left;background:none;border:none;
  padding:6px 10px;font-size:13px;color:var(--fg);cursor:pointer;border-radius:4px}
.nav-group-toggle:hover{background:var(--hover)}
.nav-group-items{display:none;padding-left:14px}
.nav-group-items.on{display:block}
.nav-group-items a{display:block;padding:4px 8px;font-size:12px;color:var(--fg-soft);
  text-decoration:none;border-radius:4px}
.nav-group-items a:hover{background:var(--hover)}
"""

# Common JS helpers injected into every page. Provides:
#  - toast(msg, kind?)        — append to the toast host + record in history
#  - setStatus(msg, kind?)    — back-compat shim that pipes through toast()
#  - hotkey(combo, handler)   — `Ctrl+S`, `Esc`, `/`, etc.
#  - confirmModal(msg, opts?) — Promise-based replacement for window.confirm()
COMMON_JS = r"""
// ---- toast / history ---------------------------------------------------
(function(){
  if(document.getElementById("toast-host")) return;
  const host = document.createElement("div");
  host.id = "toast-host"; host.className = "toast-host";
  document.body.appendChild(host);
  const hist = document.createElement("div");
  hist.id = "toast-history"; hist.className = "toast-history";
  hist.innerHTML = '<h4>Recent messages</h4><div id="toast-history-items"></div>';
  document.body.appendChild(hist);
})();
window._toastLog = [];
// Default to TEXT semantics so server-supplied strings can't inject HTML.
// Callers that need rich markup (e.g. inline <b>) pass {html: true} —
// they're responsible for escaping any interpolated user data themselves.
window.toast = function(msg, kind, opts){
  if(!msg) return;
  const host = document.getElementById("toast-host");
  if(!host) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  if(opts && opts.html){
    el.innerHTML = msg;
  } else {
    el.textContent = msg;
  }
  host.appendChild(el);
  // record in history (keep last 30, plain-text)
  const txt = el.textContent.trim();
  if(txt){
    const now = new Date();
    const ts = now.toLocaleTimeString();
    window._toastLog.push({ts, msg: txt, kind: kind||""});
    if(window._toastLog.length > 30) window._toastLog.shift();
  }
  // auto-dismiss after 4.5s (errors stay 8s)
  const lifespan = (kind === "err") ? 8000 : 4500;
  setTimeout(() => { el.classList.add("fade"); }, lifespan - 250);
  setTimeout(() => { if(el.parentNode) el.parentNode.removeChild(el); }, lifespan);
};
function renderHistory(){
  const root = document.getElementById("toast-history-items");
  if(!root) return;
  if(!window._toastLog.length){
    root.innerHTML = '<div class="empty">No messages yet.</div>'; return;
  }
  root.innerHTML = window._toastLog.slice().reverse().map(e =>
    `<div class="item ${e.kind}"><span class="ts">${e.ts}</span>${
       (e.msg||"").replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`).join("");
}
window.toggleToastHistory = function(){
  const el = document.getElementById("toast-history");
  if(!el) return;
  const showing = el.classList.toggle("on");
  if(showing) renderHistory();
};
// Back-compat: the old setStatus() targeted #status. Keep that path working,
// but ALSO funnel into the toast log so the message is preserved.
//
// Default: TEXT semantics. Inline spinners and other markup need explicit
// opt-in via window.setStatusHtml(...) — auto-detection of "<" in msg as
// the HTML signal was tried but proved brittle (`<5` etc).
window.setStatus = function(msg, kind){
  const el = document.getElementById("status");
  if(el){
    el.textContent = msg || "";
    el.style.color = kind === "err" ? "var(--bad)" : "var(--muted)";
  }
  window.toast(msg, kind);
};
window.setStatusHtml = function(html, kind){
  const el = document.getElementById("status");
  if(el){
    el.innerHTML = html || "";
    el.style.color = kind === "err" ? "var(--bad)" : "var(--muted)";
  }
  window.toast(html, kind, {html: true});
};
// Tiny HTML-escape helper — used wherever a message wants to mix safe markup
// with untrusted server strings.
window.escHtml = function(s){
  return (s == null ? "" : String(s))
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
};
// ---- hotkey registration ----------------------------------------------
window._hotkeys = {};
window.hotkey = function(combo, handler, opts){
  window._hotkeys[combo.toLowerCase()] = {handler, opts: opts || {}};
};
document.addEventListener("keydown", function(e){
  // Ignore typing in inputs unless the hotkey is explicitly meta-modifier
  const tag = (e.target.tagName || "").toUpperCase();
  const inField = (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
                   || e.target.isContentEditable);
  const parts = [];
  if(e.ctrlKey || e.metaKey) parts.push("ctrl");
  if(e.shiftKey) parts.push("shift");
  if(e.altKey)   parts.push("alt");
  const k = (e.key || "").toLowerCase();
  parts.push(k === " " ? "space" : k);
  const combo = parts.join("+");
  const reg = window._hotkeys[combo];
  if(!reg) return;
  if(inField && !reg.opts.global) return;
  e.preventDefault();
  reg.handler(e);
});
// ---- Anthropic spend badge --------------------------------------------
// Polls /api/usage every 30s and updates the #cost-badge in the header. The
// goal isn't precise billing — it's an "uh oh" surface so an accidentally-
// running scrape becomes visible before the invoice arrives.
async function refreshCostBadge(){
  const el = document.getElementById("cost-badge");
  if(!el) return;
  // Only shown to a user with their own browser-local LLM key set — the
  // server has no visibility into who has a personal key (localStorage-only),
  // so this check has to happen client-side.
  if(!window.getLLMKeys || Object.keys(getLLMKeys()).length === 0){
    el.style.display = "none";
    return;
  }
  el.style.display = "";
  try {
    const r = await fetch(`${APP_ROOT}/api/usage`);
    const j = await r.json();
    const cost = Number(j.estimated_cost_usd || 0);
    el.textContent = "$" + cost.toFixed(cost >= 1 ? 2 : 3);
    el.title = (
      "Anthropic API spend this process\\n" +
      `Calls: ${j.calls}\\n` +
      `Input tokens: ${j.input_tokens.toLocaleString()}\\n` +
      `Output tokens: ${j.output_tokens.toLocaleString()}\\n` +
      `Rate: $${j.input_price_per_mtok}/MTok in, $${j.output_price_per_mtok}/MTok out`
    );
    el.classList.toggle("over", cost >= 5);
  } catch(e){ /* ignore — badge stays at the last known value */ }
}
refreshCostBadge();
setInterval(refreshCostBadge, 30000);
// ---- page title management --------------------------------------------
window._titleBase = document.title;
window.setPageTitlePrefix = function(prefix){
  document.title = (prefix ? prefix + " · " : "") + window._titleBase;
};

// ---- confirmModal: Promise-based replacement for window.confirm() ------
// Doesn't block the JS thread and matches the app's visual style. Usage:
//   if(!await confirmModal("Delete this?")) return;
//   if(!await confirmModal("Delete ALL questions?", {danger:true, confirmLabel:"Delete all"})) return;
//   if(!await confirmModal(longMultiLineMessage, {wide:true})) return;
// The enclosing function must be `async` — a missing `async` throws
// immediately and loudly in devtools on click, so this fails loud, not silent.
window.confirmModal = function(message, opts){
  opts = opts || {};
  let modal = document.getElementById("confirm_modal");
  if(!modal){
    modal = document.createElement("div");
    modal.id = "confirm_modal";
    modal.className = "modal-backdrop";
    modal.innerHTML = `
      <div class="modal-box narrow" id="confirm_modal_box">
        <p id="confirm_modal_msg" style="margin:0 0 4px 0;font-size:13px;white-space:pre-wrap"></p>
        <div class="actions">
          <button id="confirm_modal_cancel">Cancel</button>
          <button id="confirm_modal_ok" class="primary">Confirm</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  const boxEl = document.getElementById("confirm_modal_box");
  const msgEl = document.getElementById("confirm_modal_msg");
  const okBtn = document.getElementById("confirm_modal_ok");
  const cancelBtn = document.getElementById("confirm_modal_cancel");
  msgEl.textContent = message || "Are you sure?";
  boxEl.className = "modal-box " + (opts.wide ? "wide" : "narrow");
  okBtn.className = opts.danger ? "danger" : "primary";
  okBtn.textContent = opts.confirmLabel || "Confirm";
  modal.classList.add("on");

  return new Promise(resolve => {
    function finish(result){
      modal.classList.remove("on");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onOk(){ finish(true); }
    function onCancel(){ finish(false); }
    function onBackdrop(e){ if(e.target === modal) finish(false); }
    function onKey(e){ if(e.key === "Escape") finish(false); }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
};

// ---- LLM API key settings -----------------------------------------------
// Lets the user supply their OWN API keys for Anthropic, OpenAI, Gemini,
// DeepSeek, and Mistral. Keys live ONLY in this browser's localStorage —
// never written to any file or sent anywhere except as the X-LLM-Keys
// header on this app's own same-origin /api/ requests, so the backend can
// use them (with automatic fallback to the next provider if the current
// one is out of credits, rate-limited, or invalid) instead of the server's
// own .env key.
const LLM_KEYS_STORAGE = "llm_api_keys";
const LLM_PROVIDERS = [
  {id: "anthropic", label: "Anthropic (Claude)"},
  {id: "openai",    label: "OpenAI (GPT)"},
  {id: "gemini",    label: "Google Gemini"},
  {id: "deepseek",  label: "DeepSeek"},
  {id: "mistral",   label: "Mistral"},
];
window.getLLMKeys = function(){
  try {
    const raw = JSON.parse(localStorage.getItem(LLM_KEYS_STORAGE) || "{}");
    const out = {};
    for(const p of LLM_PROVIDERS) if(raw[p.id]) out[p.id] = raw[p.id];
    return out;
  } catch(e){ return {}; }
};
window.setLLMKeys = function(keys){
  localStorage.setItem(LLM_KEYS_STORAGE, JSON.stringify(keys || {}));
};
// The actual key-entry UI lives on the Settings page (templates/settings.html)
// as a plain page section now, not a floating button + modal injected on
// every page — only the storage layer above and the auto-attach below are
// shared chrome.

// Auto-attach saved LLM keys to every same-origin /api/ request, so a key
// entered once in Settings reaches every current AND future LLM-backed
// endpoint without each call site needing to remember to do it.
(function(){
  const origFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    init = init || {};
    try {
      const url = typeof input === "string" ? input
        : (input && input.url) || "";
      if(url.indexOf("/api/") !== -1){
        const keys = getLLMKeys();
        if(Object.keys(keys).length){
          const baseHeaders = init.headers
            || (input && typeof input !== "string" && input.headers)
            || {};
          const headers = new Headers(baseHeaders);
          headers.set("X-LLM-Keys", JSON.stringify(keys));
          init = Object.assign({}, init, {headers});
        }
      }
    } catch(e){ /* never let key-attachment break a request */ }
    return origFetch(input, init);
  };
})();

// Auto-attach the CSRF double-submit-cookie token (see _check_csrf in
// review_app.py) to every mutating request, so no call site needs to
// remember to do it. GET/HEAD requests are never checked server-side, so
// they're skipped here too.
(function(){
  const origFetch = window.fetch.bind(window);
  function getCsrfToken(){
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? m[1] : null;
  }
  window.fetch = function(input, init){
    init = init || {};
    try {
      const method = (init.method || (input && typeof input !== "string" && input.method) || "GET").toUpperCase();
      if(method !== "GET" && method !== "HEAD"){
        const token = getCsrfToken();
        if(token){
          const baseHeaders = init.headers
            || (input && typeof input !== "string" && input.headers)
            || {};
          const headers = new Headers(baseHeaders);
          headers.set("X-CSRF-Token", token);
          init = Object.assign({}, init, {headers});
        }
      }
    } catch(e){ /* never let CSRF-attachment break a request */ }
    return origFetch(input, init);
  };
})();

// ---- job progress modal (jobs.py) -------------------------------------
// Shared by every Tier-1 long-running action. Usage:
//   const handle = openJobProgress({eventSlug, jobId, title});
//   handle.onDone(job => { ...job.result or job.error... });
// Polls status (~1.5s) + log tail (only new lines, via ?after=) until the
// job reaches a terminal status, then calls the caller's onDone callback
// once. The Cancel button is shown only when the job payload's `can_cancel`
// flag is true — computed server-side (jobs.can_cancel), never re-derived
// client-side.
window.openJobProgress = function({eventSlug, jobId, title}){
  let modal = document.getElementById("job_progress_modal");
  if(!modal){
    modal = document.createElement("div");
    modal.id = "job_progress_modal";
    modal.className = "job-progress-modal";
    modal.innerHTML = `
      <div class="box">
        <div class="hdr">
          <h3 id="jp_title"></h3>
          <button id="jp_close">✕ Close</button>
        </div>
        <div class="jp-phase" id="jp_phase"></div>
        <div class="jp-bar-wrap"><div class="jp-bar" id="jp_bar"></div></div>
        <div class="jp-meta" id="jp_meta"></div>
        <pre class="jp-console" id="jp_console"></pre>
        <div class="jp-actions">
          <button class="danger" id="jp_cancel" style="display:none">Cancel job</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    document.getElementById("jp_close").addEventListener("click", () => {
      modal.style.display = "none";
      if(modal._jpStop) modal._jpStop();
    });
  }
  if(modal._jpStop) modal._jpStop();  // stop any previous job's polling first

  modal.style.display = "flex";
  const titleEl   = document.getElementById("jp_title");
  const phaseEl   = document.getElementById("jp_phase");
  const barEl     = document.getElementById("jp_bar");
  const metaEl    = document.getElementById("jp_meta");
  const consoleEl = document.getElementById("jp_console");
  const cancelBtn = document.getElementById("jp_cancel");
  titleEl.textContent = title || "Job progress";
  phaseEl.textContent = "queued";
  barEl.style.width = "0%";
  barEl.className = "jp-bar";
  metaEl.textContent = "";
  consoleEl.textContent = "";
  cancelBtn.style.display = "none";

  const TERMINAL = ["succeeded", "failed", "cancelled", "interrupted"];
  let logAfter = 0;
  let doneCallback = null;
  let stopped = false;
  let timer = null;

  cancelBtn.onclick = async () => {
    if(!await confirmModal("Cancel this job?", {danger:true, confirmLabel:"Cancel job"})) return;
    try {
      await fetch(`${APP_ROOT}/event/${eventSlug}/api/jobs/${jobId}/cancel`, {method: "POST"});
    } catch(e){ /* next poll will reflect whatever actually happened */ }
  };

  async function pollOnce(){
    if(stopped) return;
    let job;
    try {
      const r = await fetch(`${APP_ROOT}/event/${eventSlug}/api/jobs/${jobId}`);
      job = await r.json();
    } catch(e){
      if(!stopped) timer = setTimeout(pollOnce, 2000);
      return;
    }
    phaseEl.textContent = job.status + (job.phase ? " — " + job.phase : "");
    barEl.className = "jp-bar" + (job.status === "failed" ? " failed"
                      : job.status === "cancelled" ? " cancelled" : "");
    if(job.total){
      const pct = Math.min(100, Math.round(100 * job.done_count / job.total));
      barEl.style.width = pct + "%";
      metaEl.textContent = `${job.done_count} / ${job.total}`;
    } else {
      barEl.style.width = (job.status === "running") ? "100%" : "0%";
      metaEl.textContent = "";
    }
    cancelBtn.style.display = (job.can_cancel && !TERMINAL.includes(job.status)) ? "" : "none";

    try {
      const r2 = await fetch(`${APP_ROOT}/event/${eventSlug}/api/jobs/${jobId}/log?after=${logAfter}`);
      const lg = await r2.json();
      if(lg.lines && lg.lines.length){
        consoleEl.textContent += lg.lines.join("\n") + "\n";
        consoleEl.scrollTop = consoleEl.scrollHeight;
      }
      logAfter = lg.total || logAfter;
    } catch(e){ /* a missed log line isn't worth aborting the modal over */ }

    if(stopped) return;
    if(TERMINAL.includes(job.status)){
      cancelBtn.style.display = "none";
      if(doneCallback) doneCallback(job);
      return;
    }
    timer = setTimeout(pollOnce, 1500);
  }
  modal._jpStop = function(){
    stopped = true;
    if(timer) clearTimeout(timer);
  };
  stopped = false;
  pollOnce();

  return {
    onDone(cb){ doneCallback = cb; },
    close(){ modal._jpStop(); modal.style.display = "none"; },
  };
};
"""
