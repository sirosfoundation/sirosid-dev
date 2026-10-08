// The centre pane: the environment under control (or "Start here", or a settings screen).
import { api, ApiError, isMissingEndpoint } from "./api.js";
import { expiry, ago, statusPill, isTransient, urlCards, isHttpsUrl, envLabel, nowSeconds } from "./format.js";
import { state, on, fail, guard, selectedInstance, loadInstances, loadConfigs, setCentre, setMobile } from "./store.js";
import { h, fill, pill, spinner, toast, confirmModal, copyButton, modal, field } from "./ui.js";
import { settingsScreen } from "./settings.js";
import { prefill, focusComposer, showExamples } from "./chat.js";
import { newEnvironmentModal, focusLibrary } from "./library.js";

const el = {};
let mounted = { id: null, tab: null, key: "" };      // what the tab body currently shows
let healthTimer = null;

const TABS = [
  ["overview", "Overview"], ["components", "Components", "health"], ["config", "Config", "config"],
  ["activity", "Activity", "activity"], ["credentials", "Credentials"],
];
const visibleTabs = () => TABS.filter(([, , f]) => !f || state.features[f] !== false);
const chatOn = () => !!state.chatStatus?.enabled;

export function mountEnv(root) {
  el.root = root;
  el.unsubs?.forEach((u) => u());
  el.unsubs = [on("select", () => render(true)), on("centre", () => render(true)), on("instances", () => render(false)),
    on("chat", () => { if (!selectedInstance() && state.centre === "env") render(true); })];
  mounted = { id: null, tab: null, key: "" };
  if (!el.visWired) {
    el.visWired = true;
    document.addEventListener("visibilitychange", () => { if (el.root?.isConnected && document.visibilityState === "visible" && mounted.tab === "components") loadHealth(); });
  }
  render(true);
}

/** Re-render the pane. `full` rebuilds everything; otherwise only the header and what depends on status. */
export function render(full) {
  clearTimeout(healthTimer);
  if (state.centre !== "env") {
    mounted = { id: null, tab: null, key: "" };
    return settingsScreen(state.centre, el.root, () => render(true), () => setCentre("env"));
  }
  const inst = selectedInstance();
  if (!inst) { mounted = { id: null, tab: null, key: "" }; return startHere(); }
  if (!visibleTabs().some(([t]) => t === state.envTab)) state.envTab = "overview";
  const key = `${inst.status}|${JSON.stringify(inst.urls || {})}|${inst.reconfigurable}`;
  if (full || mounted.id !== inst.id || !el.head || !el.root.contains(el.head)) {
    el.head = h("div", { class: "env-head" });
    el.tabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Environment views" });
    el.body = h("div", { class: "tab-body", role: "tabpanel", tabindex: "-1" });
    fill(el.root, h("div", { class: "env" }, el.head, el.tabs, el.body));
    mounted = { id: inst.id, tab: null, key: "" };
    probeFeatures(inst.id);
  }
  head(inst);
  tabs();
  if (mounted.tab !== state.envTab) { mounted.tab = state.envTab; mounted.key = key; body(inst); }
  else if (mounted.key !== key) {
    mounted.key = key;
    if (["overview", "credentials"].includes(state.envTab)) body(inst);         // status/URLs changed: those views depend on it
    else if (state.envTab === "config") configStatusNote(inst);
  }
  if (state.envTab === "components") scheduleHealth();
}

// ---- start here --------------------------------------------------------------------------------------

function startHere() {
  const lim = state.me?.limits || {};
  const card = (title, text, action, cls = "") => h("button", { class: "start-card " + cls, on: { click: action } }, h("span", { class: "start-title" }, title), h("span", { class: "start-text" }, text));
  fill(el.root, h("div", { class: "start" },
    h("h1", { class: "pane-title big", tabindex: "-1" }, "Start here"),
    h("p", { class: "lead" }, `An environment is your own complete SIROS ID test stack: a wallet, an issuer, a verifier and a test login. It is removed after ${lim.ttl_days || "a few"} days unless you keep it.`),
    h("div", { class: "start-cards" },
      chatOn()
        ? card("Describe what you need", "Tell the assistant in your own words; it sets the environment up.", () => focusComposer(), "accent")
        : card("Create one by hand", "Pick a template and a label; it is ready in a few minutes.", () => newEnvironmentModal({}), "accent"),
      card("Start from a template", "Ready-made starting points: standard stack, SIROS registry, DC API and more.", () => focusLibrary("templates")),
      chatOn() && card("Try an example", "Common requests, one click away.", () => showExamples())),
    state.instances.length > 0 && h("p", { class: "muted" }, `You have ${state.instances.length} environment${state.instances.length === 1 ? "" : "s"}: pick one in the library to manage it.`)));
}

// ---- header ------------------------------------------------------------------------------------------

function head(inst) {
  const p = statusPill(inst.status);
  const busy = isTransient(inst.status);
  const lim = state.me?.limits || {};
  const act = (label, fn, cls = "", disabled = busy) => h("button", { class: cls, disabled, on: { click: guard(fn) } }, label);
  const post = (path, body, msg) => async () => { await api("POST", `/api/instances/${encodeURIComponent(inst.id)}${path}`, body); if (msg) toast(msg); await loadInstances(); };
  const exp = expiry(inst, nowSeconds());
  fill(el.head,
    h("div", { class: "env-title-row" },
      h("div", { class: "env-title" },
        h("h1", { class: "pane-title", tabindex: "-1" }, envLabel(inst)),
        h("div", { class: "env-meta" }, pill(p), exp && h("span", { class: "muted" }, exp), h("span", { class: "muted id" }, inst.id))),
      h("div", { class: "env-actions" },
        inst.status === "stopped" ? act("Start", post("/start", undefined, "Starting")) : act("Stop", post("/stop", undefined, "Stopping"), "", busy || inst.status === "failed"),
        act("Reset data", async () => {
          if (!(await confirmModal({ title: `Reset ${envLabel(inst)}?`, text: "Every wallet account, credential and document in this environment is deleted, and the environment restarts empty. Its configuration and addresses stay the same.", confirmLabel: "Reset data", danger: true }))) return;
          await post("/reset", undefined, "Resetting: this takes a minute or two")();
        }, "", busy || inst.status !== "running"),
        lim.max_kept > 0 && act(inst.kept ? "Unkeep" : "Keep", post("/keep", { keep: !inst.kept }, inst.kept ? "No longer kept" : "Kept: it will not expire")),
        act("Destroy", async () => {
          if (!(await confirmModal({ title: `Destroy ${envLabel(inst)}?`, text: "The environment and all its data are deleted for good. This cannot be undone.", confirmLabel: "Destroy", danger: true }))) return;
          await api("DELETE", `/api/instances/${encodeURIComponent(inst.id)}`);
          toast("Destroying");
          await loadInstances();
        }, "danger", inst.status === "destroying"))),
    inst.status === "failed" && h("div", { class: "alert bad", role: "alert" }, h("b", {}, "This environment failed. "), inst.error || "No reason was given."),
    busy && h("div", { class: "alert busy" }, spinner(), " ", busyText(inst.status)));
}

const busyText = (s) => ({
  creating: "Being created: this takes a few minutes. This page updates by itself.",
  reconfiguring: "Restarting with the new configuration: this takes a few minutes.",
  resetting: "Deleting its data and restarting.",
}[s] || `${s[0].toUpperCase()}${s.slice(1)}…`);

function tabs() {
  const list = visibleTabs();
  fill(el.tabs, list.map(([id, label], i) => {
    const b = h("button", { role: "tab", id: `tab-${id}`, "aria-selected": String(id === state.envTab), tabindex: id === state.envTab ? "0" : "-1",
      "aria-controls": "env-tabpanel", class: "tab" }, label);
    b.addEventListener("click", () => { state.envTab = id; render(false); });
    b.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      state.envTab = list[(i + d + list.length) % list.length][0];
      render(false);
      el.tabs.querySelector(`#tab-${state.envTab}`)?.focus();
    });
    return b;
  }));
  el.body.id = "env-tabpanel";
  el.body.setAttribute("aria-labelledby", `tab-${state.envTab}`);
}

function body(inst) {
  clearTimeout(healthTimer);
  ({ overview, components, config, activity, credentials }[state.envTab] || overview)(inst);
}

// ---- feature probing: the newer endpoints may not exist on this server yet ------------------------------

let probed = false;
async function probeFeatures(id) {
  if (probed) return;
  probed = true;
  const enc = encodeURIComponent(id);
  const check = async (f, path) => {
    if (state.features[f] !== null) return;
    try { await api("GET", path); state.features[f] = true; }
    catch (e) { if (isMissingEndpoint(e)) state.features[f] = false; else state.features[f] = true; }
  };
  await Promise.all([check("health", `/api/instances/${enc}/health`), check("activity", `/api/instances/${enc}/activity`), check("config", `/api/instances/${enc}/config`)]);
  if (Object.values(state.features).some((v) => v === false) && el.root.contains(el.tabs)) { tabs(); if (!visibleTabs().some(([t]) => t === state.envTab)) { state.envTab = "overview"; render(true); } }
}

/** Mark an endpoint missing after the fact (a 404 with no API error code) and drop its tab. */
function missing(f) { state.features[f] = false; state.envTab = "overview"; render(true); }

// ---- overview ----------------------------------------------------------------------------------------

function overview(inst) {
  const cards = urlCards(inst.urls);
  const wallet = cards.find((c) => c.key === "wallet-frontend"), issuer = cards.find((c) => c.key === "vc-apigw"), verifier = cards.find((c) => c.key === "vc-verifier");
  fill(el.body,
    h("section", { class: "section", "aria-labelledby": "ov-urls" },
      h("h2", { id: "ov-urls" }, "Addresses"),
      cards.length ? h("div", { class: "url-grid" }, cards.map((c) => h("div", { class: "url-card" },
        h("div", { class: "url-role" }, c.role),
        c.hint && h("div", { class: "url-hint muted" }, c.hint),
        h("code", { class: "url" }, c.url),
        h("div", { class: "row" },
          isHttpsUrl(c.url) && h("a", { class: "button small primary", href: c.url, target: "_blank", rel: "noopener noreferrer", "aria-label": `Open ${c.role} (new tab)` }, "Open ↗"),
          copyButton(c.url, "Copy", `${c.role} address`)))))
        : h("p", { class: "muted" }, isTransient(inst.status) ? "The addresses appear here when the environment is running." : "This environment reports no public addresses.")),
    h("section", { class: "section", "aria-labelledby": "ov-start" },
      h("h2", { id: "ov-start" }, "Getting started with this environment"),
      h("ol", { class: "checklist" },
        h("li", {}, h("b", {}, "Open the wallet"), wallet ? " and sign up with a passkey." : " (once it is running) and sign up with a passkey."),
        h("li", {}, h("b", {}, "Get a credential: "), issuer ? "open the issuer, pick a credential type and log in at the test login with one of its test users." : "open the issuer, pick a credential type and log in at the test login."),
        h("li", {}, h("b", {}, "Present it: "), verifier ? "open the verifier and present the credential from your wallet." : "open the verifier and present the credential from your wallet."),
        h("li", {}, h("b", {}, "Native apps and SDKs: "), "point the app's backend URL at the Wallet API address."))),
    chatOn() && h("div", { class: "row" }, h("button", { on: { click: () => prefill(`In the environment ${envLabel(inst)} (${inst.id}), `) } }, "Ask the agent to change something")));
}

// ---- components (health) --------------------------------------------------------------------------------

function components() {
  el.health = h("div", {}, h("p", { class: "muted" }, "Checking…"));
  fill(el.body, h("section", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Components"), h("span", { class: "grow" }),
      h("button", { class: "small", on: { click: () => loadHealth() } }, "Refresh")),
    el.health));
  loadHealth();
}

function scheduleHealth() {
  clearTimeout(healthTimer);
  healthTimer = setTimeout(() => { if (state.envTab === "components" && state.centre === "env" && document.visibilityState === "visible") loadHealth(); }, 10000);
}

async function loadHealth() {
  const inst = selectedInstance();
  if (!inst || state.envTab !== "components" || !el.health) return;
  const target = el.health;
  try {
    const r = await api("GET", `/api/instances/${encodeURIComponent(inst.id)}/health`);
    if (target !== el.health || selectedInstance()?.id !== inst.id) return;
    const comps = r.components || [];
    fill(target,
      r.error && h("p", { class: "alert warn" }, r.error),
      comps.length ? h("table", { class: "health" },
        h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Component"), h("th", { scope: "col" }, "State"), h("th", { scope: "col" }, "Health"), h("th", { scope: "col" }, "Detail"))),
        h("tbody", {}, comps.map((c) => h("tr", {},
          h("td", {}, h("code", {}, c.name)), h("td", {}, c.state || "—"),
          h("td", {}, c.healthy === true ? h("span", { class: "ok" }, "✓ healthy") : c.healthy === false ? h("span", { class: "bad" }, "✗ unhealthy") : h("span", { class: "muted" }, "unknown")),
          h("td", { class: "muted" }, c.detail || "")))))
        : h("p", { class: "muted" }, "No components reported yet."),
      h("p", { class: "muted small" }, r.checked_at ? `Checked ${ago(r.checked_at, nowSeconds())}; refreshes every 10 seconds while this tab is open.` : "Refreshes every 10 seconds while this tab is open."));
  } catch (e) {
    if (isMissingEndpoint(e)) return missing("health");
    if (e instanceof ApiError && (e.status === 401 || e.status === 423)) return fail(e);
    fill(target, h("p", { class: "bad" }, `Could not check: ${e.message}`));
  }
  scheduleHealth();
}

// ---- config ------------------------------------------------------------------------------------------

function config(inst) {
  const doc = h("textarea", { id: "env-config", class: "code-editor", spellcheck: "false", "aria-describedby": "cfg-help", rows: "16" });
  const problems = h("div", { class: "problems", role: "alert" });
  const note = h("div", { class: "cfg-note" });
  el.cfgNote = note;
  const original = { text: "" };
  const parse = () => {
    try { const v = JSON.parse(doc.value || "{}"); if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("the config must be a JSON object"); return v; }
    catch (e) { showProblems([`Not valid JSON: ${e.message}`]); return null; }
  };
  const showProblems = (list) => fill(problems, list.length ? [h("p", { class: "problems-title" }, `${list.length} problem${list.length === 1 ? "" : "s"}`), h("ul", {}, list.map((p) => h("li", {}, typeof p === "string" ? p : `${p.path}: ${p.message}`)))] : []);
  const validate = async () => {
    const cfg = parse(); if (!cfg) return null;
    const { problems: p } = await api("POST", "/api/configs/validate", { config: cfg });
    showProblems(p || []);
    return (p || []).length ? null : cfg;
  };
  const validateBtn = h("button", { on: { click: guard(async () => { if (await validate()) { fill(problems, h("p", { class: "ok" }, "✓ Valid")); } }) } }, "Validate");
  const applyBtn = h("button", { class: "primary", on: { click: guard(async () => {
    const cfg = await validate(); if (!cfg) return;
    if (!(await confirmModal({ title: `Apply this config to ${envLabel(inst)}?`, confirmLabel: "Apply and restart",
      text: "The environment restarts with the new configuration and is unavailable for a few minutes while it does. Its addresses stay the same." }))) return;
    try {
      const r = await api("POST", `/api/instances/${encodeURIComponent(inst.id)}/reconfigure`, { config: cfg });
      if (r && r.id) { const i = state.instances.findIndex((x) => x.id === r.id); if (i >= 0) state.instances[i] = { ...state.instances[i], ...r }; }
      original.text = doc.value;
      toast("Reconfiguring: this takes a few minutes");
      await loadInstances();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && e.problems.length) return showProblems(e.problems);
      if (isMissingEndpoint(e)) return showProblems(["This server cannot reconfigure an environment in place yet."]);
      throw e;
    }
  }) } }, "Apply");
  const revert = h("button", { class: "link", on: { click: () => { doc.value = original.text; fill(problems); } } }, "Revert changes");
  const saveAs = h("button", { class: "link", on: { click: () => saveAsModal(parse) } }, "Save as a named config…");
  el.cfgButtons = { applyBtn };
  fill(el.body, h("section", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Configuration")),
    h("p", { id: "cfg-help", class: "muted" }, "What this environment runs, as JSON. Validate checks it against what your account may use; Apply restarts the environment with it."),
    note, doc, problems,
    h("div", { class: "row editor-actions" }, revert, saveAs, h("span", { class: "grow" }), validateBtn, applyBtn)));
  configStatusNote(inst);
  doc.value = "Loading…"; doc.disabled = true;
  api("GET", `/api/instances/${encodeURIComponent(inst.id)}/config`).then((r) => {
    original.text = JSON.stringify(r.config || {}, null, 2);
    doc.value = original.text; doc.disabled = false;
  }).catch((e) => {
    if (isMissingEndpoint(e)) return missing("config");
    doc.value = "";
    if (e instanceof ApiError && (e.status === 401 || e.status === 423)) return fail(e);
    showProblems([`Could not load the config: ${e.message}`]);
  });
}

function configStatusNote(inst) {
  if (!el.cfgNote || !el.cfgButtons) return;
  const busy = isTransient(inst.status);
  const cannot = inst.reconfigurable === false;
  el.cfgButtons.applyBtn.disabled = busy || cannot;
  fill(el.cfgNote,
    busy && h("p", { class: "alert busy" }, spinner(), ` The environment is ${inst.status}; Apply is available again when it is done.`),
    cannot && h("p", { class: "alert warn" }, "This environment cannot be reconfigured in place. Save this config and create a new environment from it instead."));
}

function saveAsModal(parse) {
  const cfg = parse(); if (!cfg) return;
  const name = h("input", { id: "save-name", maxlength: "64", autocomplete: "off" });
  const err = h("p", { class: "bad", role: "alert" });
  const save = h("button", { class: "primary" }, "Save");
  const m = modal({ title: "Save as a named config", body: [field("save-name", "Name", name, "Saved configs are listed under Library → Saved configs."), err], actions: [h("button", { on: { click: () => m.close() } }, "Cancel"), save] });
  save.addEventListener("click", async () => {
    const n = name.value.trim(); if (!n) { err.textContent = "Give it a name."; return; }
    try { await api("PUT", `/api/configs/${encodeURIComponent(n)}`, { config: cfg }); m.close(); toast("Saved"); loadConfigs().catch(fail); }
    catch (e) { if (e instanceof ApiError && (e.status === 401 || e.status === 423)) { m.close(); return fail(e); } err.textContent = e.problems?.length ? e.problems.map((p) => `${p.path}: ${p.message}`).join("; ") : e.message; }
  });
}

// ---- activity -----------------------------------------------------------------------------------------

function activity(inst) {
  const list = h("div", {}, h("p", { class: "muted" }, "Loading…"));
  fill(el.body, h("section", { class: "section" }, h("h2", {}, "Activity"), list));
  api("GET", `/api/instances/${encodeURIComponent(inst.id)}/activity`).then((r) => {
    const ev = [...(r.events || [])].sort((a, b) => (b.ts || 0) - (a.ts || 0));
    fill(list, ev.length ? h("ol", { class: "timeline" }, ev.map((x) => h("li", {},
      h("span", { class: "tl-time muted" }, x.ts ? new Date(x.ts * 1000).toLocaleString() : ""),
      h("span", { class: "tl-action" }, String(x.action || "")),
      x.detail && h("span", { class: "tl-detail muted" }, typeof x.detail === "string" ? x.detail : JSON.stringify(x.detail)))))
      : h("p", { class: "muted" }, "Nothing yet."));
  }).catch((e) => {
    if (isMissingEndpoint(e)) return missing("activity");
    if (e instanceof ApiError && (e.status === 401 || e.status === 423)) return fail(e);
    fill(list, h("p", { class: "bad" }, `Could not load the activity: ${e.message}`));
  });
}

// ---- credentials --------------------------------------------------------------------------------------

function credentials(inst) {
  const out = h("div", {});
  const ready = ["running", "stopped"].includes(inst.status);
  const reveal = h("button", { class: "primary", disabled: !ready }, "Reveal credentials");
  reveal.addEventListener("click", guard(async () => {
    const c = await api("GET", `/api/instances/${encodeURIComponent(inst.id)}/credentials`);
    reveal.hidden = true;
    fill(out,
      h("div", { class: "secret" }, h("div", { class: "url-role" }, "Wallet backend admin token"),
        h("code", { class: "url" }, c.admin_token || "(none)"),
        h("div", { class: "row" }, c.admin_token && copyButton(c.admin_token, "Copy", "admin token"),
          h("button", { class: "small", on: { click: () => { out.replaceChildren(); reveal.hidden = false; reveal.focus(); } } }, "Hide"))));
  }));
  fill(el.body, h("section", { class: "section" }, h("h2", {}, "Credentials"),
    h("p", { class: "alert warn" }, "The admin token gives full control of this environment's wallet backend. Do not paste it into chats, tickets or the assistant; the assistant cannot read it."),
    !ready && h("p", { class: "muted" }, `Available when the environment is running or stopped (it is ${inst.status}).`),
    reveal, out));
}

export { focusLibrary };
export const showEnvOnMobile = () => setMobile("env");
