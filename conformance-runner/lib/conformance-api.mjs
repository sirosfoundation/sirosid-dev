/**
 * Conformance Suite API client.
 * Ported from sirosid-tests/helpers/conformance-api.ts for use in the runner.
 */

export class ConformanceAPI {
  constructor(baseUrl) {
    this.baseUrl = baseUrl.endsWith('/') ? baseUrl : baseUrl + '/';
    this.headers = { 'Content-Type': 'application/json' };
  }

  async request(method, url, { body, params, expectedStatus, timeout = 20000 } = {}) {
    let fullUrl = url;
    if (params && Object.keys(params).length > 0) {
      const sp = new URLSearchParams(params);
      fullUrl += (fullUrl.includes('?') ? '&' : '?') + sp.toString();
    }
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const ctrl = new AbortController();
        const tid = setTimeout(() => ctrl.abort(), timeout);
        const opts = { method, headers: { ...this.headers }, signal: ctrl.signal };
        if (body) opts.body = body;
        const res = await fetch(fullUrl, opts);
        clearTimeout(tid);
        if (expectedStatus !== undefined && res.status !== expectedStatus) {
          const text = await res.text().catch(() => '');
          if (res.status >= 500 && attempt < 3) { await this.sleep(2000 * attempt); continue; }
          throw new Error(`${method} ${url} failed: HTTP ${res.status} - ${text.slice(0, 200)}`);
        }
        const ct = res.headers.get('content-type') || '';
        return ct.includes('application/json') ? await res.json() : await res.text();
      } catch (err) {
        if (err.name === 'AbortError') throw new Error(`${method} ${url} timed out`);
        if (attempt < 3) { await this.sleep(2000 * attempt); continue; }
        throw err;
      }
    }
  }

  sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  async waitForServerReady(timeoutMs = 120000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      try {
        const ctrl = new AbortController();
        const tid = setTimeout(() => ctrl.abort(), 10000);
        const res = await fetch(`${this.baseUrl}api/runner/available`, {
          headers: this.headers, signal: ctrl.signal,
        });
        clearTimeout(tid);
        if (res.status === 200) return;
      } catch {}
      await this.sleep(5000);
    }
    throw new Error('Conformance suite did not become ready');
  }

  async isReady() {
    try {
      const ctrl = new AbortController();
      const tid = setTimeout(() => ctrl.abort(), 5000);
      const res = await fetch(`${this.baseUrl}api/runner/available`, {
        headers: this.headers, signal: ctrl.signal,
      });
      clearTimeout(tid);
      return res.status === 200;
    } catch { return false; }
  }

  async getAllTestModules() {
    return this.request('GET', `${this.baseUrl}api/runner/available`, { expectedStatus: 200 });
  }

  async createTestPlan(planName, configuration, variant) {
    const params = { planName };
    if (variant) params.variant = JSON.stringify(variant);
    return this.request('POST', `${this.baseUrl}api/plan`, {
      params, body: configuration, expectedStatus: 201,
    });
  }

  async createTestFromPlan(planId, testName) {
    return this.request('POST', `${this.baseUrl}api/runner`, {
      params: { test: testName, plan: planId }, expectedStatus: 201,
    });
  }

  async startTest(moduleId) {
    return this.request('POST', `${this.baseUrl}api/runner/${moduleId}`, { expectedStatus: 200 });
  }

  async getModuleInfo(moduleId) {
    return this.request('GET', `${this.baseUrl}api/info/${moduleId}`, { expectedStatus: 200 });
  }

  async getTestLog(moduleId) {
    return this.request('GET', `${this.baseUrl}api/log/${moduleId}`, { expectedStatus: 200 });
  }

  async waitForState(moduleId, requiredStates, timeoutMs = 240000) {
    const deadline = Date.now() + timeoutMs;
    let last = null;
    while (Date.now() < deadline) {
      const info = await this.getModuleInfo(moduleId);
      const status = info.status;
      if (status !== last) last = status;
      if (requiredStates.includes(status)) return status;
      if (status === 'INTERRUPTED') throw new Error(`Module ${moduleId} was interrupted`);
      await this.sleep(1000);
    }
    throw new Error(`Timed out waiting for ${requiredStates.join('|')} (last: ${last})`);
  }

  async getWalletInteractionUrl(moduleId) {
    const logs = await this.getTestLog(moduleId);
    for (const entry of logs) {
      if (entry.credential_offer_redirect_url) return entry.credential_offer_redirect_url;
      if (entry.redirect_to_authorization_endpoint) return entry.redirect_to_authorization_endpoint;
      const msg = entry.msg || '';
      if (msg.includes('request_uri=') || msg.includes('client_id=')) {
        const m = msg.match(/https?:\/\/[^\s"']+request_uri=[^\s"']+/);
        if (m) return m[0];
      }
      if (msg.includes('credential_offer_uri=') || msg.includes('openid-credential-offer')) {
        const m = msg.match(/(openid-credential-offer:\/\/[^\s"']+|https?:\/\/[^\s"']+credential_offer[^\s"']+)/);
        if (m) return m[0];
      }
    }
    return null;
  }

  getPlanDetailUrl(planId) { return `${this.baseUrl}plan-detail.html?plan=${planId}`; }
  getLogDetailUrl(moduleId) { return `${this.baseUrl}log-detail.html?log=${moduleId}`; }
}
