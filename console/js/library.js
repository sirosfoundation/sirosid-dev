// The right pane: the user's environments, the templates, and saved configs (advanced).
import { api, ApiError } from "./api.js";
import { expiry, statusPill, quotaLine, envLabel, ago, nowSeconds } from "./format.js";
import { state, on, fail, guard, select, loadInstances, loadConfigs, setMobile } from "./store.js";
import { h, fill, pill, toast, modal, confirmModal, field } from "./ui.js";
import { prefill } from "./chat.js";

const el = {};
const chatOn = () => !!state.chatStatus?.enabled;

export function mountLibrary(root) {
  el.root = root;
  el.envs = h("section", { class: "lib-section", "aria-labelledby": "lib-envs" });
  el.templates = h("section", { class: "lib-section", id: "lib-templates", "aria-labelledby": "lib-tpl", tabindex: "-1" });
  el.configs = h("details", { class: "lib-section advanced" });
  fill(root, el.envs, el.templates, el.configs);
  el.unsubs?.forEach((u) => u());
  el.configsLoaded = false;
  el.unsubs = [on("instances", paintEnvs), on("select", paintEnvs), on("centre", paintEnvs),
    on("templates", paintTemplates), on("chat", paintTemplates),
    on("configs", () => { el.configsLoaded = true; paintConfigs(); })];
  el.configs.addEventListener("toggle", () => { if (el.configs.open && !el.configsLoaded) { el.configsLoaded = true; loadConfigs().catch(fail); } });
  paintEnvs(); paintTemplates(); paintConfigs();
}

/** Bring the right pane into view (and on a phone, switch to it). */
export function focusLibrary(section = "templates") {
  setMobile("library");
  const target = section === "templates" ? el.templates : el.envs;
  target?.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  (target?.querySelector("button.primary") || target?.querySelector("button") || target)?.focus({ preventScroll: true });
}

function paintEnvs() {
  if (!el.envs) return;
  const lim = state.me?.limits || {};
  const full = lim.max_concurrent && state.instances.length >= lim.max_concurrent;
  fill(el.envs,
    h("div", { class: "section-head" }, h("h2", { id: "lib-envs" }, "Environments"), h("span", { class: "grow" }),
      h("button", { class: "small primary", disabled: !!full, title: full ? "You are at your limit: destroy one first" : "", on: { click: () => newEnvironmentModal({}) } }, "+ New environment")),
    h("p", { class: "muted small quota" }, quotaLine(state.instances, lim)),
    state.instances.length
      ? h("ul", { class: "env-list" }, state.instances.map((i) => h("li", {},
          h("button", { class: "env-card", "aria-current": i.id === state.selected && state.centre === "env" ? "true" : false,
            on: { click: () => { select(i.id); setMobile("env"); } } },
            h("span", { class: "env-card-top" }, h("span", { class: "env-card-name" }, envLabel(i)), pill(statusPill(i.status))),
            h("span", { class: "env-card-meta muted" }, [i.name ? i.id : "", expiry(i, nowSeconds())].filter(Boolean).join(" · "))))))
      : h("p", { class: "muted" }, "None yet. Create one from a template below" + (chatOn() ? ", or ask the assistant." : ".")));
}

function paintTemplates() {
  if (!el.templates) return;
  fill(el.templates,
    h("div", { class: "section-head" }, h("h2", { id: "lib-tpl" }, "Templates")),
    state.templates.length
      ? h("ul", { class: "tpl-list" }, state.templates.map((t) => h("li", { class: "tpl-card" },
          h("h3", {}, t.title),
          t.description && description(t.description),
          h("div", { class: "row" },
            h("button", { class: "small primary", on: { click: () => newEnvironmentModal({ template: t }) } }, "Create environment"),
            chatOn() && h("button", { class: "small", on: { click: () => prefill(`Create an environment from the ${t.title} template and `) } }, "Ask the agent to customise")))))
      : h("p", { class: "muted" }, "No templates are available."));
}

function paintConfigs() {
  if (!el.configs) return;
  const open = el.configs.open;
  fill(el.configs,
    h("summary", {}, "Saved configs (advanced)"),
    h("p", { class: "muted small" }, "Named configs you saved. Each environment carries its own config (its Config tab); these are only starting points."),
    !el.configsLoaded ? h("p", { class: "muted" }, "Loading…")
      : state.configs.length
        ? h("ul", { class: "cfg-list" }, state.configs.map((c) => h("li", {},
            h("div", { class: "cfg-name" }, h("code", {}, c.name), c.updated_at && h("span", { class: "muted small" }, ` saved ${ago(c.updated_at, nowSeconds())}`)),
            h("div", { class: "row" },
              h("button", { class: "small", on: { click: () => newEnvironmentModal({ configName: c.name }) } }, "Use for a new environment"),
              h("button", { class: "small danger", on: { click: guard(async () => {
                if (!(await confirmModal({ title: `Delete the config “${c.name}”?`, text: "Environments made from it keep running with their own copy.", confirmLabel: "Delete", danger: true }))) return;
                await api("DELETE", `/api/configs/${encodeURIComponent(c.name)}`);
                toast("Deleted"); await loadConfigs();
              }) } }, "Delete")))))
        : h("p", { class: "muted" }, "None saved."));
  el.configs.open = open;
}

/** A template's description, clamped to a few lines with a More/Less toggle when it is long. */
function description(text) {
  const p = h("p", { class: "muted" + (text.length > 160 ? " clamp" : "") }, text);
  if (text.length <= 160) return p;
  const more = h("button", { class: "link small more", "aria-expanded": "false" }, "More");
  more.addEventListener("click", () => {
    const open = p.classList.toggle("clamp") === false;
    more.textContent = open ? "Less" : "More";
    more.setAttribute("aria-expanded", String(open));
  });
  return [p, more];
}

/** The manual way to create an environment (works without the assistant). One of `template` (a template
 *  dict) or `configName` preselects the source; with neither, the user picks a template. */
export function newEnvironmentModal({ template = null, configName = null }) {
  const lim = state.me?.limits || {};
  const label = h("input", { id: "new-label", maxlength: "64", autocomplete: "off", placeholder: "e.g. issuance demo", "data-autofocus": true });
  const keep = h("input", { id: "new-keep", type: "checkbox" });
  const err = h("div", { class: "bad", role: "alert" });
  let source = h("p", {});
  let pick = null;
  if (template) source = h("p", {}, "Template: ", h("b", {}, template.title));
  else if (configName) source = h("p", {}, "Saved config: ", h("code", {}, configName));
  else {
    pick = h("select", { id: "new-tpl" }, state.templates.map((t) => h("option", { value: t.id }, t.title)));
    const desc = h("p", { class: "muted small" });
    const upd = () => { desc.textContent = state.templates.find((t) => t.id === pick.value)?.description || ""; };
    pick.addEventListener("change", upd);
    source = [field("new-tpl", "Template", pick), desc];
    setTimeout(upd, 0);
  }
  const create = h("button", { class: "primary" }, "Create environment");
  const m = modal({ title: "New environment", body: [
    source,
    field("new-label", "Label (optional)", label, "Shown in the library; the id is generated."),
    lim.max_kept > 0 && h("label", { class: "inline" }, keep, "Keep it (it does not expire; uses your keep allowance)"),
    h("p", { class: "muted small" }, `It is ready in a few minutes and removed after ${lim.ttl_days || "a few"} days unless kept.`),
    err], actions: [h("button", { on: { click: () => m.close() } }, "Cancel"), create] });
  create.addEventListener("click", async () => {
    const t = template || state.templates.find((x) => x.id === pick?.value);
    const body = { name: label.value.trim(), keep: keep.checked };
    if (configName) body.config_name = configName; else if (t) body.config = t.config; else { err.textContent = "Pick a template."; return; }
    create.disabled = true;
    try {
      const inst = await api("POST", "/api/instances", body);
      m.close();
      toast("Creating: this takes a few minutes");
      await loadInstances();
      if (inst?.id) { select(inst.id); setMobile("env"); }
    } catch (e) {
      create.disabled = false;
      if (e instanceof ApiError && (e.status === 401 || e.status === 423)) { m.close(); return fail(e); }
      fill(err, e instanceof ApiError && e.problems.length ? h("ul", {}, e.problems.map((p) => h("li", {}, typeof p === "string" ? p : `${p.path}: ${p.message}`))) : e.message);
    }
  });
  return m;
}
