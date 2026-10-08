// The console: sign-in pages, then the chat-first workspace (chat | environment | library).
// Everything is built with textContent/createElement: never HTML strings, so nothing the server
// returns (environment names, URLs, error text, the assistant's words) is ever parsed as markup.
import { api, ApiError } from "./api.js";
import { createChat } from "./chatlogic.js";
import { state, on, hooks, fail, guard, stopPolling, loadInstances, loadTemplates, loadExamples, loadChatStatus, setCentre, setMobile } from "./store.js";
import { $, h, fill, toast, popover, closePopover } from "./ui.js";
import { enroll, login } from "./keys.js";
import { authorizeId } from "./settings.js";
import { mountChat } from "./chat.js";
import { mountEnv } from "./env.js";
import { mountLibrary } from "./library.js";

const render = (...kids) => fill($("main"), ...kids);

async function relock() {
  if (!state.me?.unlocked) return;                       // already showing Locked: nothing to refresh, and no loop
  try { state.me = await api("GET", "/api/me"); } catch { return start(); }
  state.mainKey = null;
  show();
}

async function logout() {
  await api("POST", "/api/logout");
  resetSession();
  start();
}

function resetSession() {
  state.me = state.mainKey = state.container = null;
  state.chat = createChat();
  state.instances = []; state.configs = []; state.selected = null; state.centre = "env"; state.mobile = "chat";
}

hooks.start = () => start();
hooks.relock = () => relock();

// ---- signed-out pages ------------------------------------------------------------------------------

const supportsPasskeys = () => !!(window.PublicKeyCredential && navigator.credentials);
const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); await start(); } catch (e) { fail(e); } finally { btn.disabled = false; } };
const registerRoute = () => location.hash.slice(1).startsWith("/register");
const inviteFromHash = () => new URLSearchParams(location.hash.slice(1).split("?")[1] || "").get("invite") || "";
const goHome = () => { history.replaceState(null, "", location.pathname + location.search); authRoute(); };

let shell = null;                                         // the mounted workspace, or null

/** Nothing but the way in: no workspace, no account menu. */
function hideChrome() {
  stopPolling();
  closePopover();
  shell = null;
  document.body.classList.add("auth"); document.body.classList.remove("app");
  $("main").className = "container";
  $("account").hidden = true;
  $("tabbar").hidden = true; $("tabbar").replaceChildren();
}

function authRoute() {
  hideChrome();
  return registerRoute() ? registerScreen() : signInScreen();
}

function signInScreen() {
  const signIn = h("button", { class: "primary big" }, "Sign in with a passkey");
  const register = h("button", { class: "big", on: { click: () => { location.hash = "#/register"; } } }, "Register");
  signIn.addEventListener("click", () => busy(signIn, login));
  render(h("div", { class: "hero" },
    h("h2", {}, "SIROS ID Dev"),
    h("p", { class: "muted" }, "Invite-only development environments of the SIROS ID wallet stack."),
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
    h("p", { class: "muted" }, "You need an invite and a passkey that supports the PRF extension (a recent security key, or a platform passkey that supports it). The passkey also protects your saved configs and environment credentials."),
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

// ---- the workspace ---------------------------------------------------------------------------------

const PANES = [["chat", "Chat"], ["env", "Environment"], ["library", "Library"]];

function accountMenu() {
  const btn = $("account-btn"), menu = $("account-menu");
  $("account").hidden = false;
  fill(btn, h("span", { class: "avatar", "aria-hidden": "true" }, (state.me.name || "?").trim().slice(0, 1).toUpperCase()),
    h("span", { class: "account-name" }, state.me.name || "Account"), h("span", { class: "caret", "aria-hidden": "true" }, "▾"));
  btn.setAttribute("aria-label", `Account: ${state.me.name || ""}`);
  for (const x of menu.querySelectorAll("[data-settings]")) x.remove();
  const docs = $("docs-link");
  const items = [["passkeys", "Passkeys"], ["apps", "Connected apps"], ...(state.me.role === "admin" ? [["admin", "Admin invites"]] : [])];
  for (const [mode, label] of items) {
    menu.insertBefore(h("button", { role: "menuitem", "data-settings": mode, on: { click: () => { closePopover(); openSettings(mode); } } }, label), docs);
  }
  if (!btn.dataset.wired) {
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => popover(btn, menu));
    $("signout").addEventListener("click", guard(async () => { closePopover(); await logout(); }));
    docs.addEventListener("click", () => closePopover());
  }
}

function openSettings(mode) {
  setCentre(mode);
  setMobile("env");
  setTimeout(() => $("pane-env")?.querySelector(".pane-title")?.focus(), 50);
}

function tabbar() {
  const bar = $("tabbar");
  bar.hidden = false;
  fill(bar, PANES.map(([id, label]) => h("button", { class: "tabbar-btn", "data-pane": id, "aria-pressed": String(state.mobile === id),
    on: { click: () => setMobile(id) } }, label, h("span", { class: "badge", hidden: true }))));
}

function paintMobile() {
  if (!shell) return;
  $("main").className = `app-shell show-${state.mobile}`;
  for (const b of $("tabbar").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.pane === state.mobile));
}

/** Small badge on the Chat tab when an approval waits there and the chat is not visible. */
function paintBadges() {
  const pending = state.chat.items.filter((x) => x.kind === "confirm" && !x.answered).length;
  const b = $("tabbar").querySelector('[data-pane="chat"] .badge');
  if (!b) return;
  b.hidden = pending === 0;
  b.textContent = pending ? String(pending) : "";
  b.setAttribute("aria-label", pending ? `${pending} approval${pending === 1 ? "" : "s"} waiting` : "");
}

let wired = false;
function enterApp() {
  document.body.classList.remove("auth"); document.body.classList.add("app");
  accountMenu();
  if (authorizeId()) state.centre = "consent";
  else if (state.centre === "consent") state.centre = "env";
  if (!shell) {
    const chat = h("section", { id: "pane-chat", class: "pane pane-chat", "aria-label": "Assistant" });
    const env = h("section", { id: "pane-env", class: "pane pane-env", "aria-label": "Environment" });
    const lib = h("aside", { id: "pane-lib", class: "pane pane-lib", "aria-label": "Library: environments and templates" });
    render(chat, env, lib);
    shell = { chat, env, lib };
    tabbar();
    if (state.centre === "consent") state.mobile = "env";
    paintMobile();
    mountChat(chat); mountLibrary(lib); mountEnv(env);
    if (!wired) { wired = true; on("mobile", paintMobile); on("chat-painted", paintBadges); }
    Promise.all([loadInstances(), loadTemplates(), loadChatStatus(), loadExamples()]).catch(fail);
  } else {
    setCentre(state.centre);
  }
}

function show() {
  stopPolling();
  if (!state.me.unlocked) return unlockScreen();
  enterApp();
}

async function start() {
  try {
    state.me = await api("GET", "/api/me");
  } catch (e) {
    resetSession();
    if (!(e instanceof ApiError && e.status === 401)) toast(e.message, true);
    return authRoute();
  }
  if (state.mainKey && !state.me.unlocked) state.mainKey = null;
  show();
}

window.addEventListener("hashchange", () => {
  if (!state.me) return authRoute();
  if (!state.me.unlocked) return;
  if (authorizeId()) { state.mobile = "env"; paintMobile(); setCentre("consent"); }
  else if (state.centre === "consent") setCentre("env");
});
start();
