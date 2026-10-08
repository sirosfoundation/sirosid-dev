// The page's state and a tiny publish/subscribe bus. Lives in page memory only (no web storage).
import { api, ApiError, optional } from "./api.js";
import { createChat } from "./chatlogic.js";
import { isTransient } from "./format.js";
import { toast } from "./ui.js";

export const state = {
  me: null, mainKey: null, container: null,
  instances: [], templates: [], configs: [], examples: null,
  selected: null,                 // the environment under control (an id)
  centre: "env",                  // "env" | "passkeys" | "apps" | "admin" | "consent"
  envTab: "overview",
  mobile: "chat",                 // the visible pane under 1100px: chat | env | library
  features: { config: null, health: null, activity: null, examples: null },   // null unknown, false = this server lacks it
  chat: createChat(), chatStatus: null,
};

const subs = new Map();
export function on(topic, fn) { if (!subs.has(topic)) subs.set(topic, new Set()); subs.get(topic).add(fn); return () => subs.get(topic).delete(fn); }
export function emit(topic, ...a) { for (const fn of subs.get(topic) || []) { try { fn(...a); } catch (e) { console.error(e); } } }

// app.js installs these: start() (re-read /api/me and route), relock() (show the lock page).
export const hooks = { start: () => {}, relock: () => {} };

export function fail(e) {
  if (e instanceof ApiError && e.status === 401 && state.me) {           // the session ended under us: back to the sign-in page, nothing else
    state.me = state.mainKey = state.container = null;
    return hooks.start();
  }
  if (e instanceof ApiError && e.status === 423 && state.me?.unlocked) return hooks.relock();   // the lock screen says it; no toast on top
  toast(e instanceof ApiError && e.problems.length ? e.problems.map((p) => (typeof p === "string" ? p : `${p.path}: ${p.message}`)).join("; ") : e.message || String(e), true);
}
export const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { fail(e); } };

export const byId = (id) => state.instances.find((i) => i.id === id) || null;
export const selectedInstance = () => byId(state.selected);

// ---- data loading ------------------------------------------------------------------------------------

let pollTimer = null;
export const stopPolling = () => { clearTimeout(pollTimer); pollTimer = null; };

/** Re-read the environment list; while any is transient, poll again in ~4 s (one timer, re-armed per pass). */
export async function loadInstances() {
  const { instances } = await api("GET", "/api/instances");
  state.instances = instances || [];
  if (state.selected && !byId(state.selected)) state.selected = null;
  if (!state.selected && state.instances.length === 1) state.selected = state.instances[0].id;
  emit("instances");
  stopPolling();
  if (state.me && state.instances.some((i) => isTransient(i.status))) {
    pollTimer = setTimeout(() => { if (state.me) loadInstances().catch(fail); }, 4000);
  }
}

export async function loadTemplates() {
  const r = await api("GET", "/api/templates").catch((e) => { if (e instanceof ApiError && (e.status === 401 || e.status === 423)) throw e; return { templates: [] }; });
  state.templates = r.templates || [];
  emit("templates");
}

export async function loadConfigs() {
  const r = await api("GET", "/api/configs").catch((e) => { if (e instanceof ApiError && (e.status === 401 || e.status === 423)) throw e; return { configs: [] }; });
  state.configs = r.configs || [];
  emit("configs");
}

export async function loadExamples() {
  let r = null;
  try { r = await optional("/api/examples"); } catch { r = null; }
  state.features.examples = !!(r && Array.isArray(r.examples) && r.examples.length);
  state.examples = state.features.examples ? r.examples : null;
  emit("examples");
}

export async function loadChatStatus() {
  try { state.chatStatus = await api("GET", "/api/chat/status"); }
  catch (e) {
    if (e instanceof ApiError && (e.status === 401 || e.status === 423)) throw e;
    state.chatStatus = { enabled: false, models: [], usage: { used: 0, limit: 0 } };
  }
  emit("chat");
}

/** Refetch what the assistant says changed (`refresh` {what: [...]}); unknown or empty means the list. */
export async function refresh(what = []) {
  const w = new Set(what.length ? what : ["instances"]);
  const jobs = [];
  if (w.has("instances") || w.has("instance") || ![...w].some((x) => ["configs", "templates"].includes(x))) jobs.push(loadInstances());
  if (w.has("configs")) jobs.push(loadConfigs());
  if (w.has("templates")) jobs.push(loadTemplates());
  await Promise.all(jobs);
  emit("refreshed", [...w]);
}

/** Make an environment the one under control (centre pane). */
export function select(id, { show = true } = {}) {
  state.selected = id;
  if (show && state.centre !== "consent") state.centre = "env";
  state.envTab = "overview";
  emit("select");
}

export function setCentre(mode) { state.centre = mode; emit("centre"); }
export function setMobile(pane) { state.mobile = pane; emit("mobile"); }
