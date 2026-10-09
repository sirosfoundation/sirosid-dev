// The console's one way to talk to the service. Same-origin fetch: the browser sends the
// __Host-sid cookie and the Origin header the server requires on every write.
export class ApiError extends Error {
  constructor(status, body) {
    super(body?.message || body?.error || `HTTP ${status}`);
    this.status = status;
    this.code = body?.error;
    this.problems = body?.problems || [];
  }
}

export async function api(method, path, body) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

/** POST and read a server-sent-event stream: calls onEvent(obj) for each `data:` line. Resolves when
 *  the stream ends. A non-200 answer (not signed in, locked, bad origin) throws ApiError like api(). */
export async function stream(path, body, onEvent) {
  const res = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    throw new ApiError(res.status, data);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try { onEvent(JSON.parse(line.slice(6))); } catch (e) { if (!(e instanceof SyntaxError)) throw e; }
      }
    }
  }
}

/** A route this server does not have (an older server): the framework's own 404/405, which carries
 *  no JSON error code. A 404 from the API itself ({"error": "not_found"}) is a missing OBJECT. */
export const isMissingEndpoint = (e) => e instanceof ApiError && (e.status === 404 || e.status === 405) && !e.code;

/** GET an optional endpoint: null when the server does not have it, otherwise the body (or a throw). */
export async function optional(path) {
  try { return await api("GET", path); } catch (e) { if (isMissingEndpoint(e)) return null; throw e; }
}
