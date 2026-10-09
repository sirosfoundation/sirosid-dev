// Settings mode of the centre pane: passkeys, connected apps (OAuth/MCP), admin invites, and the
// OAuth consent page (#authorize=<id>). The same screens as before the chat-first layout, rendered
// into the centre pane instead of the whole page.
import { api } from "./api.js";
import { state, guard, fail } from "./store.js";
import { h, fill, toast, confirmModal } from "./ui.js";
import { addPasskey, removePasskey } from "./keys.js";

const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");

export const SETTINGS = { passkeys: "Passkeys", apps: "Connected apps", admin: "Admin invites", consent: "Connect an application" };

export const authorizeId = () => new URLSearchParams(location.hash.slice(1)).get("authorize");
const clearAuthorize = () => history.replaceState(null, "", location.pathname + location.search);

/** Render one settings screen into `root`; `again()` re-renders it (after a change), `back()` leaves settings. */
export async function settingsScreen(mode, root, again, back) {
  const screens = { passkeys: passkeysScreen, apps: appsScreen, admin: adminScreen, consent: consentScreen };
  const body = h("div", { class: "settings-body" }, h("p", { class: "muted" }, "Loading…"));
  fill(root, h("div", { class: "settings" },
    mode !== "consent" && h("button", { class: "link back", on: { click: back } }, "← Back to environment"),
    h("h1", { class: "pane-title", tabindex: "-1" }, SETTINGS[mode] || "Settings"), body));
  try { await screens[mode]((...kids) => fill(body, ...kids), again, back); }
  catch (e) { fill(body, h("p", { class: "bad" }, "This page could not be loaded.")); fail(e); }
}

/** An application (an MCP client) asked, in the browser, for access. The server parked the request;
 *  approving needs this page to be unlocked, because the application is handed a handle on the key. */
async function consentScreen(render, again, back) {
  const id = authorizeId();
  let info;
  try { info = await api("GET", `/api/oauth/pending/${encodeURIComponent(id)}`); }
  catch (e) { clearAuthorize(); fail(e); return back(); }
  const go = guard(async (verb) => { const { redirect } = await api("POST", `/api/oauth/${verb}`, { id }); clearAuthorize(); location.assign(redirect); });
  render(
    h("div", { class: "card" }, h("h2", {}, `Let “${info.client_name}” use your account?`),
      h("p", {}, "It will be able to create, stop, start, reset and destroy your SIROS ID Dev environments, manage your saved configs, and read your environments' credentials. It cannot see your passkeys, change your account, or use admin functions."),
      h("p", { class: "muted" }, `After you choose, your browser is sent to ${info.redirect_host}. Only continue if you started this from that application: anyone can register an application under any name.`),
      !state.me.unlocked && h("p", { class: "bad" }, "Your session is locked. Sign in again to approve."),
      h("div", { class: "row end" },
        h("button", { on: { click: () => go("deny") } }, "Deny"),
        h("button", { class: "primary", disabled: !state.me.unlocked, on: { click: () => go("approve") } }, "Allow"))));
}

async function appsScreen(render, again) {
  const { grants } = await api("GET", "/api/oauth/grants");
  const url = `${location.origin}/mcp`;
  render(
    h("div", { class: "card" }, h("h2", {}, "Use from an AI assistant"),
      h("p", {}, "Add this address as an MCP server; you will be asked here to allow it:"), h("pre", {}, url),
      h("p", { class: "muted" }, "Access ends after 8 hours, when you revoke it below, or when the server restarts.")),
    h("div", { class: "card" }, h("h2", {}, "Connected applications"),
      grants.length ? h("table", {}, grants.map((g) => h("tr", {}, h("td", {}, g.client_name),
        h("td", { class: "muted" }, `since ${when(g.created_at)}, until ${when(g.expires_at)}`),
        h("td", { class: "row" }, h("button", { class: "danger", on: { click: guard(async () => {
          if (!(await confirmModal({ title: "Revoke access?", text: `${g.client_name} loses access to your account at once.`, confirmLabel: "Revoke", danger: true }))) return;
          await api("DELETE", `/api/oauth/grants/${encodeURIComponent(g.id)}`); again();
        }) } }, "Revoke")))))
        : h("p", { class: "muted" }, "None.")));
}

async function passkeysScreen(render, again) {
  const { passkeys } = await api("GET", "/api/passkeys");
  const label = h("input", { id: "plabel", placeholder: "label, e.g. YubiKey" });
  render(
    h("div", { class: "card" }, h("h2", {}, "Your passkeys"),
      h("table", {}, passkeys.map((p) => h("tr", {}, h("td", {}, p.label), h("td", { class: "muted" }, `added ${when(p.created_at)}`),
        h("td", { class: "row" }, passkeys.length > 1 && h("button", { class: "danger", on: { click: guard(async () => {
          if (!(await confirmModal({ title: "Remove this passkey?", text: `“${p.label}” will no longer sign you in or unlock your data.`, confirmLabel: "Remove", danger: true }))) return;
          await removePasskey(p.id); again();
        }) } }, "Remove")))))),
    h("div", { class: "card" }, h("h2", {}, "Add a passkey"), h("p", { class: "muted" }, "Keep at least two, on different devices or keys (a device cannot add a second passkey to itself): there is no recovery if you lose your only passkey."),
      h("label", { for: "plabel" }, "Label"), label,
      h("div", { class: "row end" }, h("button", { class: "primary", on: { click: guard(async () => { await addPasskey(label.value.trim()); toast("Added"); again(); }) } }, "Add"))));
}

async function adminScreen(render, again) {
  const [{ invites }, { instances }] = await Promise.all([api("GET", "/api/admin/invites"), api("GET", "/api/admin/instances")]);
  const caps = h("input", { id: "caps", placeholder: "custom_images, raw_values" });
  const email = h("input", { id: "iemail", type: "email", placeholder: "optional, binds the invite" });
  const conc = h("input", { id: "iconc", type: "number", min: "1", placeholder: "default" });
  const kept = h("input", { id: "ikept", type: "number", min: "0", value: "0" });
  const out = h("pre", { hidden: true });
  render(
    h("div", { class: "card" }, h("h2", {}, "New invite"),
      h("label", { for: "iemail" }, "Email"), email, h("label", { for: "caps" }, "Capabilities (comma separated)"), caps,
      h("label", { for: "iconc" }, "Concurrent environments"), conc, h("label", { for: "ikept" }, "Keep allowance"), kept,
      h("div", { class: "row end" }, h("button", { class: "primary", on: { click: guard(async () => {
        const body = { email: email.value.trim(), capabilities: caps.value.split(",").map((s) => s.trim()).filter(Boolean), max_kept: Number(kept.value || 0) };
        if (conc.value) body.max_concurrent = Number(conc.value);
        const { token } = await api("POST", "/api/admin/invites", body);
        out.hidden = false; out.textContent = token;            // shown once; the server stores only a hash
      }) } }, "Create")), out, h("p", { class: "muted" }, "The token is shown once.")),
    h("div", { class: "card" }, h("h2", {}, "Invites"), invites.length ? h("table", {}, invites.map((v) => h("tr", {}, h("td", {}, h("code", {}, v.token_hash)), h("td", {}, v.email || ""), h("td", { class: "muted" }, v.revoked ? "revoked" : v.used_by ? "used" : "open"),
      h("td", { class: "row" }, h("button", { class: "danger", on: { click: guard(async () => { await api("DELETE", `/api/admin/invites/${encodeURIComponent(v.token_hash)}`); again(); }) } }, "Revoke"))))) : h("p", { class: "muted" }, "None.")),
    h("div", { class: "card" }, h("h2", {}, "All environments"), instances.length ? h("table", {}, instances.map((i) => h("tr", {}, h("td", {}, i.name || i.id), h("td", {}, i.status), h("td", { class: "muted" }, i.kept ? "kept" : when(i.expires_at))))) : h("p", { class: "muted" }, "None.")));
}
