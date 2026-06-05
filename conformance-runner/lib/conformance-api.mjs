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
      // If INTERRUPTED is not in requiredStates, treat it as a terminal state
      if (status === 'INTERRUPTED' && !requiredStates.includes('INTERRUPTED')) {
        return status;
      }
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

  /**
   * Check what the suite is waiting for by examining the test log.
   * Returns 'credential_offer' | 'tx_code' | 'browser' | null
   */
  async getWaitingAction(moduleId) {
    const logs = await this.getTestLog(moduleId);
    for (let i = logs.length - 1; i >= 0; i--) {
      const entry = logs[i];
      const src = entry.src || '';
      if (src.includes('WaitForCredentialOffer')) return 'credential_offer';
      if (src.includes('WaitForTxCode')) return 'tx_code';
    }
    // Fall back to browser interaction
    return 'browser';
  }

  /**
   * Get the credential offer endpoint URL for a module (used for issuer_initiated flow).
   */
  async getCredentialOfferUrl(moduleId) {
    const info = await this.getModuleInfo(moduleId);
    const alias = info.alias;
    if (alias) return `${this.baseUrl}test/a/${alias}/credential_offer`;
    return null;
  }

  /**
   * Get the tx_code endpoint URL for a module.
   */
  async getTxCodeUrl(moduleId) {
    const info = await this.getModuleInfo(moduleId);
    const alias = info.alias;
    if (alias) return `${this.baseUrl}test/a/${alias}/tx_code`;
    return null;
  }

  /**
   * Get browser interaction URL for issuer tests.
   * When the conformance suite enters WAITING and needs browser-based
   * authorization (e.g. mock AS login), this returns the URL to navigate to.
   */
  async getBrowserInteractionUrl(moduleId) {
    const info = await this.getModuleInfo(moduleId);
    // Check for urls field
    const urls = info.urls;
    if (urls) {
      for (const urlEntry of Object.values(urls)) {
        if (typeof urlEntry === 'string' && urlEntry.includes('/test/')) return urlEntry;
      }
    }
    // Fall back to constructing from alias
    const alias = info.alias;
    if (alias) return `${this.baseUrl}test/a/${alias}/authorize`;
    return null;
  }
}
