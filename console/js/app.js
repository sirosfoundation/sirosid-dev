// The console. Everything is built with textContent/createElement: never HTML strings, so nothing
// the server returns (instance names, URLs, error text) is ever parsed as markup.
import { api, ApiError } from "./api.js";
import * as C from "./container.js";
import { creationOptions, requestOptions, credentialToJSON, prfEnabled, prfFirst, randomChallenge } from "./webauthn.js";

const $ = (id) => document.getElementById(id);
const state = { me: null, mainKey: null, container: null, tab: "instances", poll: null };
const stopPolling = () => { clearTimeout(state.poll); state.poll = null; };

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "class") el.className = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 5000);
}
const fail = (e) => {
  if (e instanceof ApiError && e.status === 401 && state.me) {          // the session ended under us: back to the sign-in page, nothing else
    state.me = state.mainKey = state.container = null;
    return start();
  }
  if (e instanceof ApiError && e.status === 423 && state.me?.unlocked) return relock();   // the lock screen says it; no toast on top
  toast(e instanceof ApiError && e.problems.length ? e.problems.map((p) => `${p.path}: ${p.message}`).join("; ") : e.message || String(e), true);
};
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { fail(e); } };
const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");
const render = (...kids) => { const m = $("main"); m.replaceChildren(...kids.flat().filter((k) => k != null && k !== false)); };

// ---- passkey + key container ---------------------------------------------------------------

const saltOf = (options) => unbase(options.extensions.prf.eval.first);
const unbase = (s) => C.unb64u(s);

/** Ask the authenticator for the PRF output of ONE credential (a second touch after create). */
async function prfFor(credentialId, options) {
  const a = await navigator.credentials.get({ publicKey: {
    challenge: randomChallenge(), rpId: options.rp.id, userVerification: "required", timeout: 60000,
    allowCredentials: [{ type: "public-key", id: credentialId }],
    extensions: { prf: { eval: { first: saltOf(options) } } } } });
  const out = prfFirst(a);
  if (!out) throw new Error("this passkey did not return a PRF output, so it cannot protect your data");
  return out;
}

async function createPasskey(begin) {
  const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
  if (!prfEnabled(cred)) {
    throw new Error("this authenticator does not support the WebAuthn PRF extension, which protects your data. " +
                    "Use a different passkey (a recent security key, or a platform passkey that supports PRF).");
  }
  return cred;
}

const storeContainer = (container) => api("PUT", "/api/privatedata", { container: C.b64u(C.serialize(container)) });
const unlockServer = (mainKey) => api("POST", "/api/unlock", { main_key: C.b64u(mainKey) });

async function firstContainer(rawId, prf, options) {
  const { container, mainKey } = await C.createContainer({ credentialId: rawId, prfOutput: prf, prfSalt: saltOf(options) });
  await storeContainer(container);
  state.container = container;
  state.mainKey = mainKey;
  await unlockServer(mainKey);
}

async function enroll(invite, name, email) {
  const begin = await api("POST", "/api/enroll/begin", { invite, name, email });
  const cred = await createPasskey(begin);            // refuses non-PRF BEFORE the invite can be spent
  await api("POST", "/api/enroll/finish", { ceremony_id: begin.ceremony_id, credential: credentialToJSON(cred) });
  const prf = await prfFor(cred.rawId, begin.options);
  await firstContainer(new Uint8Array(cred.rawId), prf, begin.options);
}

async function login() {
  const begin = await api("POST", "/api/login/begin");
  const a = await navigator.credentials.get({ publicKey: requestOptions(begin.options) });
  const prf = prfFirst(a);
  await api("POST", "/api/login/finish", { ceremony_id: begin.ceremony_id, credential: credentialToJSON(a) });
  if (!prf) throw new Error("this passkey did not return a PRF output; it cannot unlock your data");
  const raw = new Uint8Array(a.rawId);
  const { container } = await api("GET", "/api/privatedata");
  if (!container) return firstContainer(raw, prf, begin.options);        // enrolment was interrupted before the container was stored
  state.container = C.parse(C.unb64u(container));
  state.mainKey = await C.openContainer(state.container, raw, prf);
  await unlockServer(state.mainKey);
}

async function addPasskey(label) {
  if (!state.mainKey) throw new Error("sign in again first: adding a passkey needs your unlocked key");
  const begin = await api("POST", "/api/passkeys/begin");
  const cred = await createPasskey(begin);
  const prf = await prfFor(cred.rawId, begin.options);
  // The container is stored FIRST: a failure after this leaves an unused entry (harmless), never a
  // registered passkey that cannot open the container.
  const next = await C.addPasskey(state.container, state.mainKey,
    { credentialId: new Uint8Array(cred.rawId), prfOutput: prf, prfSalt: saltOf(begin.options), transports: cred.response.getTransports?.() });
  await storeContainer(next);
  await api("POST", "/api/passkeys/finish", { ceremony_id: begin.ceremony_id, credential: credentialToJSON(cred), label });
  state.container = next;
}

async function removePasskey(id) {
  await api("DELETE", `/api/passkeys/${encodeURIComponent(id)}`);
  if (state.mainKey) {
    const next = await C.removePasskey(state.container, state.mainKey, C.unb64u(id));
    await storeContainer(next);
    state.container = next;
  }
}

/** The server says the key is gone (expired or dropped): show the Locked banner now, not at the next reload. */
async function relock() {
  if (!state.me?.unlocked) return;                       // already showing Locked: nothing to refresh, and no loop
  try { state.me = await api("GET", "/api/me"); } catch { return start(); }
  state.mainKey = null;
  show();
}

async function logout() {
  await api("POST", "/api/logout");
  state.me = state.mainKey = state.container = null;
  start();
}

// ---- screens ---------------------------------------------------------------------------------

const supportsPasskeys = () => !!(window.PublicKeyCredential && navigator.credentials);
const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); await start(); } catch (e) { fail(e); } finally { btn.disabled = false; } };
const registerRoute = () => location.hash.slice(1).startsWith("/register");
const inviteFromHash = () => new URLSearchParams(location.hash.slice(1).split("?")[1] || "").get("invite") || "";
const goHome = () => { history.replaceState(null, "", location.pathname + location.search); authRoute(); };

/** Nothing but the way in: no navigation, no account bar. */
function hideChrome() { $("nav").hidden = true; $("nav").replaceChildren(); $("who").replaceChildren(); }

function authRoute() {
  hideChrome();
  return registerRoute() ? registerScreen() : signInScreen();
}

/** The first page: two buttons. */
function signInScreen() {
  const signIn = h("button", { class: "primary big" }, "Sign in with a passkey");
  const register = h("button", { class: "big", on: { click: () => { location.hash = "#/register"; } } }, "Register");
  signIn.addEventListener("click", () => busy(signIn, login));
  render(h("div", { class: "hero" },
    h("h2", {}, "SIROS ID Dev"),
    h("p", { class: "muted" }, "Invite-only development instances of the SIROS ID wallet stack."),
    !supportsPasskeys() && h("p", { class: "bad" }, "This browser does not support passkeys."),
    h("div", { class: "stack" }, signIn, register)));
}

/** Registration, on its own page. An invite link (#/register?invite=...) fills the token in; the fragment never reaches the server. */
function registerScreen() {
  const invite = h("input", { id: "invite", autocomplete: "off", spellcheck: "false", value: inviteFromHash() });
  const name = h("input", { id: "name", autocomplete: "name" });
  const email = h("input", { id: "email", type: "email", autocomplete: "email" });
  const join = h("button", { class: "primary big" }, "Create account");
  join.addEventListener("click", () => busy(join, async () => {
    await enroll(invite.value.trim(), name.value.trim(), email.value.trim());
    history.replaceState(null, "", location.pathname + location.search);   // do not leave the invite in the address bar or history
  }));
  render(h("div", { class: "hero left" },
    h("h2", {}, "Register"),
    h("p", { class: "muted" }, "You need an invite and a passkey that supports the PRF extension (a recent security key, or a platform passkey that supports it). The passkey also protects your saved configs and instance credentials."),
    !supportsPasskeys() && h("p", { class: "bad" }, "This browser does not support passkeys."),
    h("label", { for: "invite" }, "Invite token"), invite,
    h("label", { for: "name" }, "Your name"), name,
    h("label", { for: "email" }, "Email (only if the invite names one)"), email,
    h("div", { class: "stack" }, join, h("button", { class: "big", on: { click: goHome } }, "Back to sign in"))));
}

/** Signed in but the key is gone (expired, or the server restarted): the same clean page, one way forward. */
function unlockScreen() {
  hideChrome();
  const unlock = h("button", { class: "primary big" }, "Unlock with your passkey");
  unlock.addEventListener("click", () => busy(unlock, login));
  render(h("div", { class: "hero" },
    h("h2", {}, "Sign in again"),
    h("p", { class: "muted" }, `${state.me.name}, your session needs your passkey to unlock your data.`),
    h("div", { class: "stack" }, unlock, h("button", { class: "big", on: { click: guard(logout) } }, "Sign out"))));
}

const NAMES = { instances: "Instances", configs: "Configs", apps: "Connected apps", passkeys: "Passkeys", admin: "Admin" };

function chrome() {
  const tabs = ["instances", "configs", "apps", "passkeys", ...(state.me.role === "admin" ? ["admin"] : [])];
  $("nav").hidden = false;
  $("nav").replaceChildren(...tabs.map((t) => h("button", { "aria-current": t === state.tab ? "page" : false, on: { click: () => { state.tab = t; show(); } } }, NAMES[t])),
    h("span", { class: "grow" }));
  const who = $("who");
  who.replaceChildren(`${state.me.name} · `, h("button", { on: { click: guard(logout) } }, "Sign out"));
}

async function show() {
  stopPolling();
  if (!state.me.unlocked) return unlockScreen();
  chrome();
  try {
    const pending = authorizeId();
    if (pending) return await consentScreen(pending);
    await ({ instances: instancesScreen, configs: configsScreen, apps: appsScreen, passkeys: passkeysScreen, admin: adminScreen })[state.tab]();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return start();
    fail(e);
  }
}

// ---- instances ---------------------------------------------------------------------------------

const TRANSIENT = new Set(["creating", "resetting", "starting", "stopping", "destroying"]);

async function instancesScreen() {
  const [{ instances }, { configs }] = await Promise.all([api("GET", "/api/instances"), api("GET", "/api/configs").catch(() => ({ configs: [] }))]);
  const lim = state.me.limits;
  const cfg = h("select", { id: "cfg" }, configs.map((c) => h("option", { value: c.name }, c.name)));
  const nm = h("input", { id: "iname", placeholder: "label (optional)" });
  const keep = h("input", { type: "checkbox", id: "keep" });
  const create = h("button", { class: "primary", disabled: configs.length === 0, on: { click: guard(async () => {
    await api("POST", "/api/instances", { config_name: cfg.value, name: nm.value, keep: keep.checked });
    toast("Deploying: this takes a few minutes");
    show();
  }) } }, "Create instance");
  render(
    h("p", { class: "muted" }, `Up to ${lim.max_concurrent} at once. Instances are removed after ${lim.ttl_days} days unless kept (${lim.max_kept} keep allowance).`),
    h("div", { class: "card" }, h("h2", {}, "New instance"),
      configs.length ? [h("label", { for: "cfg" }, "Config"), cfg] : h("p", {}, "Save a config first (Configs tab)."),
      h("label", { for: "iname" }, "Label"), nm,
      lim.max_kept > 0 && h("label", { class: "inline" }, keep, "Keep (does not expire)"), h("div", { class: "row end" }, create)),
    instances.length ? instances.map(instanceCard) : h("p", { class: "muted" }, "No instances yet."));
  stopPolling();
  if (instances.some((i) => TRANSIENT.has(i.status))) state.poll = setTimeout(() => instancesScreen().catch(fail), 8000);   // one-shot: each pass re-arms at most one timer
}

function instanceCard(i) {
  const act = (label, path, method = "POST", cls = "") => h("button", { class: cls, disabled: TRANSIENT.has(i.status),
    on: { click: guard(async () => { await api(method, `/api/instances/${i.id}${path}`); show(); }) } }, label);
  const urls = Object.entries(i.urls || {}).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, /^https:\/\//.test(v) ? h("a", { href: v, target: "_blank", rel: "noopener noreferrer" }, v) : v)));
  const creds = h("div", {});
  return h("div", { class: "card" },
    h("div", { class: "row" }, h("b", {}, i.name || i.id), h("span", { class: "pill" }, i.status), i.kept && h("span", { class: "pill" }, "kept"),
      h("span", { class: "muted" }, i.kept ? "" : `expires ${when(i.expires_at)}`)),
    i.error && h("p", { class: "bad" }, i.error),
    urls.length > 0 && h("table", {}, urls),
    h("div", { class: "row" },
      i.status === "stopped" ? act("Start", "/start") : act("Stop", "/stop"),
      act("Reset data", "/reset"), 
      state.me.limits.max_kept > 0 && h("button", { disabled: TRANSIENT.has(i.status), on: { click: guard(async () => { await api("POST", `/api/instances/${i.id}/keep`, { keep: !i.kept }); show(); }) } }, i.kept ? "Unkeep" : "Keep"),
      h("button", { disabled: TRANSIENT.has(i.status), on: { click: guard(async () => {
        const c = await api("GET", `/api/instances/${i.id}/credentials`);
        creds.replaceChildren(h("pre", {}, JSON.stringify(c, null, 2)));
      }) } }, "Credentials"),
      h("button", { class: "danger", on: { click: guard(async () => { if (confirm("Destroy this instance and its data?")) { await api("DELETE", `/api/instances/${i.id}`); show(); } }) } }, "Destroy")),
    creds);
}

// ---- configs -----------------------------------------------------------------------------------

async function configsScreen() {
  const [{ configs }, schema, { templates }] = await Promise.all([api("GET", "/api/configs"), api("GET", "/api/schema"), api("GET", "/api/templates")]);
  const name = h("input", { id: "cname", placeholder: "name" });
  const doc = h("textarea", { id: "cdoc", spellcheck: "false" }, "{}");
  const problems = h("div", { class: "bad" });
  const tplInfo = h("div", { class: "muted" });
  const tpl = h("select", { id: "ctpl", "aria-label": "Template" }, [h("option", { value: "" }, "Choose a template…"), ...templates.map((t) => h("option", { value: t.id }, t.title))]);
  tpl.addEventListener("change", () => {
    const t = templates.find((x) => x.id === tpl.value);
    tplInfo.replaceChildren(...(t ? [h("p", {}, t.description), ...t.hints.map((x) => h("p", { class: "muted" }, `Tip: ${x}`))] : []));
    if (!t) return;
    doc.value = JSON.stringify(t.config, null, 2);
    if (!name.value.trim()) name.value = t.id;
    problems.replaceChildren();
  });
  const check = async () => {
    const { problems: p } = await api("POST", "/api/configs/validate", { config: JSON.parse(doc.value || "{}") });
    problems.replaceChildren(...p.map((x) => h("div", {}, x)));
    return p.length === 0;
  };
  const keys = Object.entries(schema.properties).map(([k, v]) => h("tr", {}, h("td", {}, h("code", {}, k)), h("td", {}, v.type), h("td", { class: "muted" }, v.description)));
  render(
    h("div", { class: "card" }, h("h2", {}, "Saved configs"),
      configs.length ? h("table", {}, configs.map((c) => h("tr", {},
        h("td", {}, c.name), h("td", { class: "muted" }, when(c.updated_at)),
        h("td", { class: "row end" },
          h("button", { on: { click: guard(async () => { const r = await api("GET", `/api/configs/${encodeURIComponent(c.name)}`); name.value = c.name; doc.value = JSON.stringify(r.config, null, 2); }) } }, "Edit"),
          h("button", { class: "danger", on: { click: guard(async () => { if (confirm(`Delete ${c.name}?`)) { await api("DELETE", `/api/configs/${encodeURIComponent(c.name)}`); show(); } }) } }, "Delete"))))) : h("p", { class: "muted" }, "None yet.")),
    h("div", { class: "card" }, h("h2", {}, "Edit"),
      h("label", { for: "ctpl" }, "Start from a template"), tpl, tplInfo,
      h("label", { for: "cname" }, "Name"), name, h("label", { for: "cdoc" }, "Config (JSON)"), doc, problems,
      h("div", { class: "row end" },
        h("button", { on: { click: guard(async () => { if (await check()) toast("Valid"); }) } }, "Validate"),
        h("button", { class: "primary", on: { click: guard(async () => {
          if (!(await check())) return;
          await api("PUT", `/api/configs/${encodeURIComponent(name.value.trim())}`, { config: JSON.parse(doc.value) });
          toast("Saved"); show();
        }) } }, "Save"))),
    h("details", {}, h("summary", {}, "What can a config say?"), h("table", {}, keys)));
}

// ---- connected apps (OAuth / MCP) ----------------------------------------------------------------

const authorizeId = () => new URLSearchParams(location.hash.slice(1)).get("authorize");
const clearAuthorize = () => history.replaceState(null, "", location.pathname + location.search);

/** An application (an MCP client) asked, in the browser, for access. The server parked the request;
 *  approving needs this page to be unlocked, because the application is handed a handle on the key. */
async function consentScreen(id) {
  let info;
  try { info = await api("GET", `/api/oauth/pending/${encodeURIComponent(id)}`); }
  catch (e) { clearAuthorize(); fail(e); return show(); }
  const go = guard(async (verb) => { const { redirect } = await api("POST", `/api/oauth/${verb}`, { id }); clearAuthorize(); location.assign(redirect); });
  render(
    h("div", { class: "card" }, h("h2", {}, `Let “${info.client_name}” use your account?`),
      h("p", {}, "It will be able to create, stop, start, reset and destroy your SIROS ID Dev instances, manage your saved configs, and read your instances' credentials. It cannot see your passkeys, change your account, or use admin functions."),
      h("p", { class: "muted" }, `After you choose, your browser is sent to ${info.redirect_host}. Only continue if you started this from that application: anyone can register an application under any name.`),
      !state.me.unlocked && h("p", { class: "bad" }, "Your session is locked. Sign in again to approve."),
      h("div", { class: "row end" },
        h("button", { on: { click: () => go("deny") } }, "Deny"),
        h("button", { class: "primary", disabled: !state.me.unlocked, on: { click: () => go("approve") } }, "Allow"))));
}

async function appsScreen() {
  const { grants } = await api("GET", "/api/oauth/grants");
  const url = `${location.origin}/mcp`;
  render(
    h("div", { class: "card" }, h("h2", {}, "Use from an AI assistant"),
      h("p", {}, "Add this address as an MCP server; you will be asked here to allow it:"), h("pre", {}, url),
      h("p", { class: "muted" }, "Access ends after 8 hours, when you revoke it below, or when the server restarts.")),
    h("div", { class: "card" }, h("h2", {}, "Connected applications"),
      grants.length ? h("table", {}, grants.map((g) => h("tr", {}, h("td", {}, g.client_name),
        h("td", { class: "muted" }, `since ${when(g.created_at)}, until ${when(g.expires_at)}`),
        h("td", { class: "row end" }, h("button", { class: "danger", on: { click: guard(async () => { await api("DELETE", `/api/oauth/grants/${g.id}`); show(); }) } }, "Revoke")))))
        : h("p", { class: "muted" }, "None.")));
}

// ---- passkeys ----------------------------------------------------------------------------------

async function passkeysScreen() {
  const { passkeys } = await api("GET", "/api/passkeys");
  const label = h("input", { id: "plabel", placeholder: "label, e.g. YubiKey" });
  render(
    h("div", { class: "card" }, h("h2", {}, "Passkeys"),
      h("table", {}, passkeys.map((p) => h("tr", {}, h("td", {}, p.label), h("td", { class: "muted" }, `added ${when(p.created_at)}`),
        h("td", { class: "row end" }, passkeys.length > 1 && h("button", { class: "danger", on: { click: guard(async () => { await removePasskey(p.id); show(); }) } }, "Remove")))))),
    h("div", { class: "card" }, h("h2", {}, "Add a passkey"), h("p", { class: "muted" }, "Keep at least two, on different devices or keys (a device cannot add a second passkey to itself): there is no recovery if you lose your only passkey."),
      label, h("div", { class: "row end" }, h("button", { class: "primary", on: { click: guard(async () => { await addPasskey(label.value.trim()); toast("Added"); show(); }) } }, "Add"))));
}

// ---- admin -------------------------------------------------------------------------------------

async function adminScreen() {
  const [{ invites }, { instances }] = await Promise.all([api("GET", "/api/admin/invites"), api("GET", "/api/admin/instances")]);
  const caps = h("input", { id: "caps", placeholder: "capabilities, comma separated (custom_images, raw_values)" });
  const email = h("input", { id: "iemail", type: "email", placeholder: "email (optional, binds the invite)" });
  const conc = h("input", { id: "iconc", type: "number", min: "1", placeholder: "max concurrent (default)" });
  const kept = h("input", { id: "ikept", type: "number", min: "0", value: "0" });
  const out = h("pre", { hidden: true });
  render(
    h("div", { class: "card" }, h("h2", {}, "New invite"), h("label", { for: "iemail" }, "Email"), email, h("label", { for: "caps" }, "Capabilities"), caps,
      h("label", { for: "iconc" }, "Concurrent instances"), conc, h("label", { for: "ikept" }, "Keep allowance"), kept,
      h("div", { class: "row end" }, h("button", { class: "primary", on: { click: guard(async () => {
        const body = { email: email.value.trim(), capabilities: caps.value.split(",").map((s) => s.trim()).filter(Boolean), max_kept: Number(kept.value || 0) };
        if (conc.value) body.max_concurrent = Number(conc.value);
        const { token } = await api("POST", "/api/admin/invites", body);
        out.hidden = false; out.textContent = token;            // shown once; the server stores only a hash
      }) } }, "Create")), out, h("p", { class: "muted" }, "The token is shown once.")),
    h("div", { class: "card" }, h("h2", {}, "Invites"), h("table", {}, invites.map((v) => h("tr", {}, h("td", {}, h("code", {}, v.token_hash)), h("td", {}, v.email || ""), h("td", { class: "muted" }, v.revoked ? "revoked" : v.used_by ? "used" : "open"),
      h("td", { class: "row end" }, h("button", { class: "danger", on: { click: guard(async () => { await api("DELETE", `/api/admin/invites/${v.token_hash}`); show(); }) } }, "Revoke")))))),
    h("div", { class: "card" }, h("h2", {}, "All instances"), h("table", {}, instances.map((i) => h("tr", {}, h("td", {}, i.name || i.id), h("td", {}, i.status), h("td", { class: "muted" }, i.kept ? "kept" : when(i.expires_at)))))));
}

// ---- start -------------------------------------------------------------------------------------

async function start() {
  try {
    state.me = await api("GET", "/api/me");
  } catch (e) {
    state.me = null;
    if (!(e instanceof ApiError && e.status === 401)) toast(e.message, true);
    return authRoute();
  }
  if (state.mainKey && !state.me.unlocked) state.mainKey = null;
  show();
}

window.addEventListener("hashchange", () => { if (state.me) show(); else authRoute(); });
start();
