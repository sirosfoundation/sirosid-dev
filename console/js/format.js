// Pure formatting: time, expiry, status pills, URL roles and tool arguments in plain words.
// No DOM, no fetch: node --test imports this module directly (console/test/format.test.mjs).

/** Statuses during which the service is busy with an environment: actions wait, the page polls. */
export const TRANSIENT = new Set(["creating", "resetting", "reconfiguring", "starting", "stopping", "destroying"]);
export const isTransient = (status) => TRANSIENT.has(String(status));

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** status -> {label, tone, spinner}; tone is one of ok | busy | idle | bad (a CSS modifier). */
export function statusPill(status) {
  const s = String(status || "unknown");
  if (s === "running") return { label: "Running", tone: "ok", spinner: false };
  if (TRANSIENT.has(s)) return { label: cap(s), tone: "busy", spinner: true };
  if (s === "stopped") return { label: "Stopped", tone: "idle", spinner: false };
  if (s === "failed") return { label: "Failed", tone: "bad", spinner: false };
  return { label: cap(s), tone: "idle", spinner: false };
}

/** Seconds -> the two most significant units: "2d 3h", "3h 5m", "12m", "less than a minute". */
export function duration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return hr ? `${d}d ${hr}h` : `${d}d`;
  if (hr > 0) return m ? `${hr}h ${m}m` : `${hr}h`;
  if (m > 0) return `${m}m`;
  return "less than a minute";
}

/** "kept" | "expires in 2d 3h" | "expired" | "" (no expiry known). Times are epoch seconds. */
export function expiry(inst, now) {
  if (!inst) return "";
  if (inst.kept) return "kept";
  if (!inst.expires_at) return "";
  const left = inst.expires_at - now;
  return left <= 0 ? "expired" : `expires in ${duration(left)}`;
}

/** Epoch seconds -> "just now" | "5m ago" | "3h 2m ago" | "2d ago". */
export function ago(ts, now) {
  if (!ts) return "";
  const d = now - ts;
  return d < 60 ? "just now" : `${duration(d)} ago`;
}

export const nowSeconds = () => Date.now() / 1000;

// The public URL keys an instance reports, in the order a newcomer needs them.
const ROLES = [
  ["wallet-frontend", "Wallet", "The web wallet: sign up with a passkey and hold credentials."],
  ["vc-apigw", "Issuer", "Issues test credentials to the wallet (OpenID4VCI)."],
  ["vc-verifier", "Verifier", "Asks the wallet to present credentials (OpenID4VP)."],
  ["vc-registry", "Registry", "Credential type metadata the issuer and wallet agree on."],
  ["mini-oidc", "Test login (mini-oidc)", "A test identity provider with ready-made users."],
  ["wallet-proxy", "Wallet API", "The wallet backend, as native apps and SDKs reach it."],
  ["wallet-backend", "Wallet backend", "The wallet's server side."],
  ["pdp", "Trust (PDP)", "The trust policy decision point."],
];
const ROLE_OF = new Map(ROLES.map(([k, role, hint], i) => [k, { role, hint, order: i }]));

/** A urls dict {component: url} -> [{key, role, hint, url}] sorted by role, unknown keys last (by name). */
export function urlCards(urls) {
  return Object.entries(urls || {})
    .filter(([, v]) => typeof v === "string" && v)
    .map(([key, url]) => {
      const r = ROLE_OF.get(key) || { role: key, hint: "", order: ROLES.length };
      return { key, role: r.role, hint: r.hint, url, order: r.order };
    })
    .sort((a, b) => a.order - b.order || a.key.localeCompare(b.key))
    .map(({ order, ...rest }) => rest);
}

/** Only https URLs become links (an instance could report anything). */
export const isHttpsUrl = (u) => typeof u === "string" && /^https:\/\/[^\s/]+/.test(u);

/** "instance_id" -> "Instance id", "config_name" -> "Config name". */
export function humanKey(k) {
  const s = String(k).replace(/[_-]+/g, " ").trim();
  return s ? cap(s) : s;
}

/** Tool arguments as plain [label, value] lines. An environment id is shown with its label
 *  (nameOf(id) -> label | null); objects are compact JSON, long values are cut. */
export function describeArgs(args, nameOf = () => null) {
  const out = [];
  for (const [k, v] of Object.entries(args || {})) {
    let label = humanKey(k), text;
    if ((k === "id" || k === "instance_id") && typeof v === "string") {
      label = "Environment";
      const n = nameOf(v);
      text = n && n !== v ? `${n} (${v})` : v;
    } else if (typeof v === "boolean") text = v ? "yes" : "no";
    else if (v === null || v === undefined) text = "none";
    else if (typeof v === "object") text = JSON.stringify(v);
    else text = String(v);
    out.push([label, text.length > 240 ? text.slice(0, 237) + "…" : text]);
  }
  return out;
}

export const briefArgs = (a) => { const s = JSON.stringify(a || {}); return s === "{}" ? "" : s.length > 80 ? s.slice(0, 77) + "…" : s; };

/** "2 of 5 environments" (only environments that count: not destroyed). */
export function quotaLine(instances, limits) {
  const n = (instances || []).length;
  const max = limits?.max_concurrent;
  return max ? `${n} of ${max} environment${max === 1 ? "" : "s"}` : `${n} environment${n === 1 ? "" : "s"}`;
}

/** What a user sees for an environment: its label, else its id. */
export const envLabel = (i) => (i && (i.name || i.id)) || "";

/** A deliberately tiny, safe subset of Markdown for the assistant's replies: paragraphs, `- ` bullet lists,
 *  **bold** and `code`. Returns a structure (never markup), so the renderer builds DOM nodes with text only:
 *  nothing the model says can become a link, an image, a script or an attribute.
 *  blocks: [{type:"p"|"ul", inlines|items}], inline: {t:"text"|"b"|"code", v}. */
export function parseRich(text) {
  const inline = (s) => {
    const out = [];
    const re = /\*\*([^*\n]+)\*\*|`([^`\n]+)`/g;
    let last = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push({ t: "text", v: s.slice(last, m.index) });
      out.push(m[1] !== undefined ? { t: "b", v: m[1] } : { t: "code", v: m[2] });
      last = re.lastIndex;
    }
    if (last < s.length) out.push({ t: "text", v: s.slice(last) });
    return out;
  };
  const blocks = [];
  for (const chunk of String(text || "").replace(/\r\n?/g, "\n").split(/\n{2,}/)) {
    const lines = chunk.split("\n").filter((l) => l.trim() !== "");
    if (!lines.length) continue;
    if (lines.every((l) => /^\s*[-*] +/.test(l))) {
      blocks.push({ type: "ul", items: lines.map((l) => inline(l.replace(/^\s*[-*] +/, ""))) });
    } else {
      blocks.push({ type: "p", inlines: inline(lines.join("\n")) });
    }
  }
  return blocks;
}
