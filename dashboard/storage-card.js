/*
 * Storage card for the sirosid-dev dashboard (local startup.html and the
 * Fly dashboard fly_common.wallet_frontend_dashboard_html() generates).
 *
 * Talks to env-admin through the same-origin /_admin/ proxy (see
 * env-admin/server.py for the API). Renders into #storage-card/#storage-body
 * if present and otherwise does nothing, so the same file can be mounted
 * everywhere.
 *
 * "Clear all data" needs the environment's wallet-backend admin token. Locally
 * build-info.json carries it (generate-build-info.py, gitignored file served
 * on localhost only); on Fly the user is asked once and the browser keeps it
 * in sessionStorage. Either way the user also types the environment's name
 * to confirm - this is a public dashboard on Fly and the action is a wipe.
 */
(function () {
  var body = document.getElementById("storage-body");
  if (!body) return;

  var base = "/_admin";
  var status = null;
  var token = null;
  var events = null;
  var progress = [];   // lines of the current/last reset

  var style = document.createElement("style");
  style.textContent = [
    ".stor-row { display:flex; gap:1rem; flex-wrap:wrap; align-items:center; margin-bottom:0.6rem; font-size:0.85rem; color:#555; }",
    ".stor-btn { padding:0.45rem 1rem; border:none; border-radius:6px; color:#fff; font-weight:500; font-size:0.85rem; cursor:pointer; background:#b91c1c; }",
    ".stor-btn:hover:not(:disabled) { background:#991b1b; }",
    ".stor-btn:disabled { opacity:0.5; cursor:not-allowed; }",
    ".stor-pill { display:inline-block; padding:0.1rem 0.5rem; border-radius:4px; font-size:0.75rem; font-weight:600; text-transform:uppercase; }",
    ".stor-pill-ok { background:#dcfce7; color:#166534; } .stor-pill-warn { background:#fef9c3; color:#854d0e; }",
    ".stor-pill-bad { background:#fee2e2; color:#991b1b; } .stor-pill-run { background:#dbeafe; color:#1e40af; }",
    ".stor-log { margin-top:0.75rem; background:#f8f9fa; border:1px solid #e0e0e0; border-radius:6px; padding:0.6rem 0.75rem; font-family:'SF Mono',Consolas,Monaco,monospace; font-size:0.78rem; max-height:240px; overflow-y:auto; white-space:pre-wrap; }",
    ".stor-log .warn { color:#854d0e; } .stor-log .error { color:#991b1b; font-weight:600; }",
    "#storage-body table { margin-top:0.25rem; }"
  ].join("\n");
  document.head.appendChild(style);

  function esc(s) {
    var d = document.createElement("div");
    d.appendChild(document.createTextNode(s == null ? "" : String(s)));
    return d.innerHTML;
  }

  function human(bytes) {
    if (!bytes) return "0 B";
    var units = ["B", "KB", "MB", "GB"], i = 0, n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
  }

  function when(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString();
  }

  function render() {
    if (!status) {
      body.innerHTML = '<span class="meta">env-admin not reachable at <code>/_admin/</code> &mdash; ' +
        'is the stack up? (<code>make up</code> starts it with every mode)</span>';
      return;
    }
    var html = '';
    var ctl = status.control_available
      ? '<span class="stor-pill stor-pill-ok">can restart services</span>'
      : '<span class="stor-pill stor-pill-bad">no restart control</span>';
    html += '<div class="stor-row">' +
      '<span>Environment <strong>' + esc(status.env) + '</strong> &middot; ' + esc(status.platform) + '</span>' + ctl +
      (status.reset_in_progress ? '<span class="stor-pill stor-pill-run">reset in progress</span>' : '') +
      '</div>';

    var m = status.mongo || {};
    if (m.reachable) {
      var total = 0, docs = 0;
      html += '<table><thead><tr><th>Database</th><th>Size</th><th>Collections</th><th>Documents</th></tr></thead><tbody>';
      (m.databases || []).forEach(function (d) {
        total += d.size_bytes || 0; docs += d.documents || 0;
        html += '<tr><td class="svc-name">' + esc(d.name) + (d.exists ? '' : ' <span class="meta">(not created yet)</span>') + '</td>' +
          '<td class="meta">' + human(d.size_bytes) + '</td>' +
          '<td class="meta">' + esc(d.collections || 0) + '</td>' +
          '<td class="meta">' + esc(d.documents || 0) + '</td></tr>';
      });
      html += '<tr><td class="svc-name">total</td><td class="meta">' + human(total) + '</td><td></td><td class="meta">' + docs + '</td></tr>';
      html += '</tbody></table>';
      html += '<div class="meta" style="margin-top:0.4rem">Mongo data lives on a persistent volume and survives <code>make down</code> / a Fly redeploy.</div>';
    } else {
      html += '<div class="meta">No Mongo in this stack (' + esc(m.error || "not reachable") + ') &mdash; ' +
        'wallet-backend is using its in-memory store, which a restart empties. <code>PDP=helm</code> gives it the persistent volume.</div>';
    }

    if (status.consumers && status.consumers.length) {
      html += '<div class="meta" style="margin-top:0.6rem">Services restarted by a reset: ' +
        status.consumers.map(function (c) {
          var pill = c.state === "running" ? "stor-pill-ok" : "stor-pill-warn";
          return esc(c.name) + ' <span class="stor-pill ' + pill + '">' + esc(c.state) + '</span>';
        }).join(" &middot; ") + '</div>';
    }

    if (status.last_reset) {
      var lr = status.last_reset;
      var pill = lr.status === "finished" ? "stor-pill-ok" : lr.status === "error" ? "stor-pill-bad" : "stor-pill-run";
      html += '<div class="meta" style="margin-top:0.6rem">Last reset: <span class="stor-pill ' + pill + '">' + esc(lr.status) + '</span> ' +
        esc(when(lr.started_at)) + (lr.dropped && lr.dropped.length ? ' &middot; dropped ' + esc(lr.dropped.join(", ")) : '') +
        (lr.error ? ' &middot; ' + esc(lr.error) : '') + '</div>';
    }

    html += '<div class="stor-row" style="margin-top:0.9rem">' +
      '<button class="stor-btn" id="stor-clear" ' + ((status.reset_in_progress || !status.control_available) ? 'disabled' : '') +
      '>Clear all data&hellip;</button>' +
      '<span class="meta">Stops the services, drops every database above, restarts them and re-registers the issuer and verifier. ' +
      'Every user, passkey and credential in this environment is gone afterwards.</span></div>';

    if (progress.length) {
      html += '<div class="stor-log" id="stor-log">' + progress.map(function (p) {
        return '<div class="' + esc(p.level || "info") + '">' + esc(p.message) + '</div>';
      }).join("") + '</div>';
    }
    body.innerHTML = html;
    var btn = document.getElementById("stor-clear");
    if (btn) btn.onclick = clearAll;
    var log = document.getElementById("stor-log");
    if (log) log.scrollTop = log.scrollHeight;
  }

  function refresh() {
    fetch(base + "/api/storage")
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (s) { status = s; render(); if (s.reset_in_progress) connect(); })
      .catch(function () { status = null; render(); });
  }

  function loadLocalToken() {
    fetch("/build-info.json")
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (info) { if (info && info.env_admin_token) token = info.env_admin_token; })
      .catch(function () {});
  }

  function getToken() {
    if (token) return token;
    try { token = sessionStorage.getItem("envAdminToken"); } catch (e) {}
    if (token) return token;
    var t = window.prompt("Admin token for this environment (printed by `make fly-up`, or ADMIN_TOKEN for a local stack):");
    if (!t) return null;
    token = t.trim();
    try { sessionStorage.setItem("envAdminToken", token); } catch (e) {}
    return token;
  }

  function clearAll() {
    if (!status) return;
    var typed = window.prompt("This wipes every user, passkey and credential in '" + status.env +
      "' and restarts its services.\n\nType the environment name to confirm:");
    if (typed === null) return;
    if (typed.trim() !== status.env) { window.alert("Not confirmed - the name did not match."); return; }
    var tok = getToken();
    if (!tok) return;
    progress = [{ level: "info", message: "requesting reset…" }];
    render();
    connect();
    fetch(base + "/api/storage/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tok },
      body: JSON.stringify({ confirm: status.env })
    }).then(function (r) {
      return r.json().then(function (d) { return { code: r.status, data: d }; });
    }).then(function (res) {
      if (res.code === 401) {
        token = null;
        try { sessionStorage.removeItem("envAdminToken"); } catch (e) {}
        progress.push({ level: "error", message: "rejected: " + (res.data.error || "unauthorized") + " - the token was forgotten, try again" });
      } else if (res.code >= 400) {
        progress.push({ level: "error", message: "rejected: " + (res.data.error || res.code) });
      } else {
        progress.push({ level: "info", message: "reset " + res.data.id + " started" });
      }
      render();
      refresh();
    }).catch(function (err) {
      progress.push({ level: "error", message: "request failed: " + err });
      render();
    });
  }

  function connect() {
    if (events) return;
    events = new EventSource(base + "/api/events");
    events.addEventListener("reset_step", function (e) {
      var d = JSON.parse(e.data);
      progress.push({ level: d.level, message: d.message });
      render();
    });
    events.addEventListener("reset_finished", function (e) {
      var d = JSON.parse(e.data);
      progress.push({ level: d.status === "finished" ? "info" : "error",
        message: d.status === "finished" ? "reset complete" : "reset " + d.status + (d.error ? ": " + d.error : "") });
      render();
      refresh();
      setTimeout(function () { if (events) { events.close(); events = null; } }, 1000);
    });
    events.onerror = function () { if (events) { events.close(); events = null; } };
  }

  loadLocalToken();
  refresh();
  setInterval(refresh, 15000);
})();
