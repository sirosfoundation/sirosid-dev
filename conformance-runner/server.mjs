/**
 * Conformance Runner Server
 *
 * Express server that drives OpenID conformance suite test plans
 * and streams results via SSE to the dashboard.
 *
 * API:
 *   GET  /api/health               - Health check
 *   GET  /api/status               - Conformance suite connectivity status
 *   GET  /api/plans                - Available test plan types
 *   POST /api/runs                 - Start a new conformance run
 *   GET  /api/runs                 - List recent runs
 *   GET  /api/runs/:id             - Get run details
 *   GET  /api/log/:moduleId        - Proxy: conformance suite test log
 *   GET  /api/info/:moduleId       - Proxy: conformance suite module info
 *   GET  /api/plan/:planId         - Proxy: conformance suite plan details
 *   GET  /api/events               - SSE event stream
 *   ALL  /mcp                      - MCP Streamable HTTP endpoint
 */

import express from 'express';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { chromium } from 'playwright';
import { ConformanceAPI } from './lib/conformance-api.mjs';
import { runWalletVCI, runWalletVP } from './lib/wallet-runner.mjs';
import { setupMcpServer } from './lib/mcp-server.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());

const PORT = parseInt(process.env.PORT || '3001', 10);
const CONFORMANCE_URL = process.env.CONFORMANCE_URL || 'https://localhost.emobix.co.uk:8443/';
const CONFORMANCE_PUBLIC_URL = process.env.CONFORMANCE_PUBLIC_URL || 'https://localhost.emobix.co.uk:8443/';
const ISSUER_CONFORMANCE_URL = process.env.ISSUER_CONFORMANCE_URL || 'http://vc-apigw:8080';
const VERIFIER_CONFORMANCE_URL = process.env.VERIFIER_CONFORMANCE_URL || 'http://vc-verifier:8080';

const api = new ConformanceAPI(CONFORMANCE_URL);

// ---------------------------------------------------------------------------
// Config loading helpers
// ---------------------------------------------------------------------------

function loadConfig(filename, replacements = {}) {
  let config = readFileSync(resolve(__dirname, filename), 'utf-8');
  for (const [key, value] of Object.entries(replacements)) {
    config = config.replaceAll(`\${${key}}`, value);
  }
  return config;
}

// ---------------------------------------------------------------------------
// Plan definitions
// ---------------------------------------------------------------------------

const PLANS = {
  'vci-issuer': {
    label: 'OID4VCI Issuer',
    planName: 'oid4vci-1_0-issuer-test-plan',
    configFile: 'vci-issuer-config.json',
    phase: 1,
    variants: [
      {
        name: 'sd_jwt_vc / pre-authorized_code',
        variant: {
          credential_format: 'sd_jwt_vc',
          sender_constrain: 'dpop',
          vci_grant_type: 'pre_authorization_code',
          vci_authorization_code_flow_variant: 'issuer_initiated',
          client_auth_type: 'private_key_jwt',
          fapi_request_method: 'unsigned',
          fapi_profile: 'vci',
        },
      },
      {
        name: 'sd_jwt_vc / authorization_code',
        variant: {
          credential_format: 'sd_jwt_vc',
          sender_constrain: 'dpop',
          vci_grant_type: 'authorization_code',
          client_auth_type: 'private_key_jwt',
          fapi_request_method: 'unsigned',
          fapi_profile: 'vci',
        },
      },
    ],
    loadConfig() {
      const uniqueSuffix = Date.now().toString(36);
      return loadConfig(this.configFile, {
        ISSUER_DISCOVERY_URL: `${ISSUER_CONFORMANCE_URL}/.well-known/openid-credential-issuer`,
        ISSUER_RESOURCE_URL: ISSUER_CONFORMANCE_URL,
        ISSUER_CREDENTIAL_ISSUER_URL: ISSUER_CONFORMANCE_URL,
        ALIAS: `siros-issuer-vci-${uniqueSuffix}`,
      });
    },
  },
  'vp-verifier': {
    label: 'OID4VP Verifier',
    planName: 'oid4vp-1final-rp-test-plan',
    configFile: 'vp-verifier-config.json',
    phase: 1,
    variants: [{
      name: 'sd_jwt_vc / x509_san_dns / direct_post / request_uri_signed',
      variant: {
        credential_format: 'sd_jwt_vc',
        client_id_prefix: 'x509_san_dns',
        response_mode: 'direct_post',
        request_method: 'request_uri_signed',
        vp_profile: 'plain_vp',
      },
    }],
    loadConfig() {
      return loadConfig(this.configFile, {
        VERIFIER_DISCOVERY_URL: `${VERIFIER_CONFORMANCE_URL}/.well-known/openid-configuration`,
        VERIFIER_RESOURCE_URL: VERIFIER_CONFORMANCE_URL,
      });
    },
  },
  'vci-wallet': {
    label: 'OID4VCI Wallet',
    planName: 'oid4vci-1_0-wallet-test-plan',
    configFile: 'vci-wallet-config.json',
    phase: 2,
    variants: [{
      name: 'sd_jwt_vc / pre_authorization_code / immediate / by_value',
      variant: {
        credential_format: 'sd_jwt_vc',
        vci_grant_type: 'pre_authorization_code',
        vci_credential_issuance_mode: 'immediate',
        vci_credential_offer_variant: 'by_value',
        sender_constrain: 'dpop',
        vci_credential_encryption: 'plain',
        fapi_profile: 'vci',
        fapi_request_method: 'unsigned',
        client_auth_type: 'private_key_jwt',
        authorization_request_type: 'simple',
        vci_authorization_code_flow_variant: 'issuer_initiated',
      },
    }],
    loadConfig() { return loadConfig(this.configFile); },
  },
  'vp-wallet': {
    label: 'OID4VP Wallet',
    planName: 'oid4vp-1final-wallet-test-plan',
    configFile: 'vp-wallet-config.json',
    phase: 2,
    variants: [{
      name: 'sd_jwt_vc / x509_san_dns / direct_post / request_uri_signed',
      variant: {
        credential_format: 'sd_jwt_vc',
        client_id_prefix: 'x509_san_dns',
        response_mode: 'direct_post',
        request_method: 'request_uri_signed',
        vp_profile: 'plain_vp',
      },
    }],
    loadConfig() { return loadConfig(this.configFile); },
  },
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/** @type {Map<string, object>} */
const runs = new Map();
let runCounter = 0;

/** @type {Set<import('express').Response>} */
const sseClients = new Set();

function broadcast(event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of sseClients) {
    res.write(payload);
  }
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

app.get('/api/health', (_req, res) => {
  res.json({ status: 'ok' });
});

app.get('/api/status', async (_req, res) => {
  const ready = await api.isReady();
  res.json({
    conformance_suite: ready ? 'connected' : 'unavailable',
    url: CONFORMANCE_URL,
    publicUrl: CONFORMANCE_PUBLIC_URL,
  });
});

app.get('/api/plans', (_req, res) => {
  const list = Object.entries(PLANS).map(([id, p]) => ({
    id,
    label: p.label,
    planName: p.planName,
    phase: p.phase,
    variants: (p.variants || []).map(v => v.name),
  }));
  res.json(list);
});

app.get('/api/runs', (_req, res) => {
  const list = [...runs.values()].sort((a, b) => b.startedAt - a.startedAt);
  res.json(list);
});

app.get('/api/runs/:id', (req, res) => {
  const run = runs.get(req.params.id);
  if (!run) return res.status(404).json({ error: 'Not found' });
  res.json(run);
});

app.post('/api/runs', async (req, res) => {
  const { planType } = req.body || {};
  if (!PLANS[planType]) {
    return res.status(400).json({ error: `Unknown plan type. Choose: ${Object.keys(PLANS).join(', ')}` });
  }

  const ready = await api.isReady();
  if (!ready) {
    return res.status(503).json({ error: 'Conformance suite not available' });
  }

  try {
    const result = startRun(planType);
    res.status(201).json(result);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Proxy endpoints — serve conformance suite data without cert warnings
app.get('/api/log/:moduleId', async (req, res) => {
  try {
    const log = await api.getTestLog(req.params.moduleId);
    res.json(log);
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get('/api/info/:moduleId', async (req, res) => {
  try {
    const info = await api.getModuleInfo(req.params.moduleId);
    res.json(info);
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

app.get('/api/plan/:planId', async (req, res) => {
  try {
    const plan = await api.request('GET', `${CONFORMANCE_URL}api/plan/${req.params.planId}`, { expectedStatus: 200 });
    res.json(plan);
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

// SSE endpoint
app.get('/api/events', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.write(`event: connected\ndata: ${JSON.stringify({ time: Date.now() })}\n\n`);
  sseClients.add(res);

  // Send current state for all runs (active and finished)
  for (const run of runs.values()) {
    res.write(`event: run_state\ndata: ${JSON.stringify(run)}\n\n`);
  }

  req.on('close', () => sseClients.delete(res));
});

// ---------------------------------------------------------------------------
// Run execution
// ---------------------------------------------------------------------------

async function executeRun(id, plan, planType) {
  const run = runs.get(id);

  try {
    run.status = 'creating';
    broadcast('run_update', { id, status: 'creating' });

    // Iterate over variants — create a separate plan per variant
    const allResults = [];
    const variantList = plan.variants || [{ name: 'default', variant: plan.variant }];

    for (const variantConfig of variantList) {
      console.log(`[${planType}] Starting variant: ${variantConfig.name}`);
      broadcast('run_update', { id, status: 'creating', variant: variantConfig.name });

      // Load config per variant so each gets a unique alias
      const configJson = plan.loadConfig();

      const testPlan = await api.createTestPlan(plan.planName, configJson, variantConfig.variant);
      const modules = testPlan.modules.map(m => m.testModule);

      // Use the last plan's detail URL (or could aggregate)
      run.planId = testPlan.id;
      run.modules = [...(run.modules || []), ...modules.map(m => `[${variantConfig.name}] ${m}`)];
      run.planDetailUrl = `${CONFORMANCE_PUBLIC_URL}plan-detail.html?plan=${testPlan.id}`;
      run.status = 'running';
      broadcast('run_update', { id, status: 'running', planId: testPlan.id, modules, planDetailUrl: run.planDetailUrl, variant: variantConfig.name });

      const emit = (event) => {
        const enriched = { ...event, variant: variantConfig.name };
        if (event.type === 'module_result' && event.moduleId) {
          enriched.logUrl = `${CONFORMANCE_PUBLIC_URL}log-detail.html?log=${event.moduleId}`;
        }
        broadcast('module_event', { runId: id, ...enriched });
        if (event.type === 'module_result') {
          run.results.push({
            module: event.module,
            variant: variantConfig.name,
            status: event.status,
            result: event.result,
            moduleId: event.moduleId,
            logUrl: event.moduleId ? `${CONFORMANCE_PUBLIC_URL}log-detail.html?log=${event.moduleId}` : null,
          });
        }
      };

      let results;

      if (plan.phase === 2) {
        if (planType === 'vci-wallet') {
          results = await runWalletVCI(api, testPlan.id, modules, configJson, emit);
        } else {
          results = await runWalletVP(api, testPlan.id, modules, configJson, emit);
        }
      } else {
        results = await runServerSideModules(testPlan.id, modules, emit);
      }

      allResults.push(...results.map(r => ({ ...r, variant: variantConfig.name })));
    }

    run.results = allResults;
    run.status = 'finished';
    run.finishedAt = Date.now();

    const passed = allResults.filter(r => r.passed).length;
    const failed = allResults.filter(r => !r.passed).length;
    broadcast('run_finished', {
      id,
      passed,
      failed,
      total: allResults.length,
      planDetailUrl: run.planDetailUrl,
    });
  } catch (err) {
    run.status = 'error';
    run.error = err.message;
    run.finishedAt = Date.now();
    broadcast('run_error', { id, error: err.message });
    throw err;
  }
}

/**
 * Create a credential offer with a pre-authorized code by driving the standalone
 * OIDC flow: initiate → mini-oidc (auto-approve) → callback → extract offer.
 * Returns the credential offer object suitable for the conformance suite.
 */
async function createCredentialOffer() {
  // Step 1: Initiate standalone OIDC flow
  const initRes = await fetch(`${ISSUER_CONFORMANCE_URL}/oidcrp/initiate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ credential_type: 'pid_1_8' }),
  });
  if (!initRes.ok) throw new Error(`OIDC initiate failed: ${initRes.status}`);
  const { authorization_url } = await initRes.json();

  // Step 2: Call mini-oidc authorize (auto-approves, returns 302 to callback)
  const oidcRes = await fetch(authorization_url, { redirect: 'manual' });
  const callbackUrl = oidcRes.headers.get('location');
  if (!callbackUrl) throw new Error('No redirect from OIDC provider');

  // Step 3: Call the callback (standalone mode returns JSON with credential_offer)
  const cbRes = await fetch(callbackUrl);
  if (!cbRes.ok) throw new Error(`OIDC callback failed: ${cbRes.status}`);
  const cbData = await cbRes.json();

  if (!cbData.credential_offer) {
    throw new Error('No credential_offer in OIDC callback response');
  }

  // Rewrite credential_issuer to use the proxy URL visible to the conformance suite
  const offer = cbData.credential_offer;
  offer.credential_issuer = ISSUER_CONFORMANCE_URL;

  // Strip empty tx_code from pre-auth grant (the issuer serializes the zero struct)
  const preAuthGrant = offer.grants?.['urn:ietf:params:oauth:grant-type:pre-authorized_code'];
  if (preAuthGrant?.tx_code && !preAuthGrant.tx_code.input_mode && !preAuthGrant.tx_code.length) {
    delete preAuthGrant.tx_code;
  }

  console.log('[createCredentialOffer] offer grants:', JSON.stringify(offer.grants));
  return offer;
}

/**
 * Handle a WAITING state for a module. Checks what the suite is waiting for
 * and takes the appropriate action (credential offer, tx_code, or browser auth).
 * Returns true if the module is still running and may enter WAITING again.
 */
async function handleWaiting(moduleId, moduleName, browser) {
  const action = await api.getWaitingAction(moduleId);
  console.log(`[${moduleName}] WAITING action: ${action}`);

  if (action === 'credential_offer') {
    // Create a credential offer from the issuer and submit it
    const offer = await createCredentialOffer();
    const offerUrl = await api.getCredentialOfferUrl(moduleId);
    if (!offerUrl) throw new Error('No credential offer URL found');
    const encoded = encodeURIComponent(JSON.stringify(offer));
    const submitUrl = `${offerUrl}?credential_offer=${encoded}`;
    console.log(`[${moduleName}] Submitting credential offer to: ${offerUrl}`);
    const res = await fetch(submitUrl, {
      headers: { 'Content-Type': 'application/json' },
    });
    console.log(`[${moduleName}] Credential offer response: ${res.status}`);
    return true; // May enter WAITING again for tx_code or browser auth

  } else if (action === 'tx_code') {
    // Send tx_code
    const txCodeUrl = await api.getTxCodeUrl(moduleId);
    if (!txCodeUrl) throw new Error('No tx_code URL found');
    console.log(`[${moduleName}] Sending tx_code`);
    await fetch(`${txCodeUrl}?code=1234`);
    return true; // May enter WAITING again for browser auth

  } else {
    // Browser interaction — authorize flow
    const browserUrl = await api.getBrowserInteractionUrl(moduleId);
    if (browserUrl) {
      console.log(`[${moduleName}] Browser interaction at: ${browserUrl.slice(0, 100)}`);
      const context = await browser.newContext({ ignoreHTTPSErrors: true });
      const page = await context.newPage();

      // Log all navigations for debugging
      page.on('console', msg => console.log(`[${moduleName}] BROWSER CONSOLE: ${msg.text()}`));
      page.on('response', res => {
        if (res.status() >= 300) {
          console.log(`[${moduleName}] BROWSER ${res.status()} ${res.url().slice(0, 120)}`);
        }
      });
      page.on('requestfailed', req => {
        console.log(`[${moduleName}] BROWSER REQUEST FAILED: ${req.url().slice(0, 120)} ${req.failure()?.errorText}`);
      });

      try {
        // Navigate to authorize URL — this loads the consent page which may
        // auto-redirect through OIDC provider before settling back.
        console.log(`[${moduleName}] Navigating to authorize URL...`);
        await page.goto(browserUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
        console.log(`[${moduleName}] Page loaded, current URL: ${page.url()}`);

        // Wait for the OIDC redirect chain to complete:
        // consent.html → mini-oidc/authorize → oidcrp/callback → consent/#/credentials
        try {
          await page.waitForURL(url => url.hash?.includes('credentials') || url.pathname.includes('oidcrp/callback'), { timeout: 30000 });
          console.log(`[${moduleName}] OIDC redirect chain completed, URL: ${page.url()}`);
        } catch (waitErr) {
          console.log(`[${moduleName}] waitForURL timed out, current URL: ${page.url()}`);
          console.log(`[${moduleName}] Page title: ${await page.title().catch(() => 'unknown')}`);
          // Take a snapshot of the page content for debugging
          const bodyText = await page.locator('body').innerText({ timeout: 2000 }).catch(() => 'could not get text');
          console.log(`[${moduleName}] Page body (first 500 chars): ${bodyText.slice(0, 500)}`);
        }

        // Give Alpine.js time to render the credential selection UI
        await page.waitForTimeout(2000);

        // Select the first credential radio button.
        // The radio uses Alpine.js + appearance-none CSS, so Playwright's
        // check() can't verify state change. Use evaluate() to set checked
        // and dispatch events so Alpine picks it up.
        const credRadio = page.locator('input[name="credential"][type="radio"]').first();
        if (await credRadio.isVisible({ timeout: 5000 }).catch(() => false)) {
          console.log(`[${moduleName}] Selecting credential`);
          await credRadio.evaluate(el => {
            el.checked = true;
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('input', { bubbles: true }));
          });
          await page.waitForTimeout(500);
        }

        // Handle consent/approve button — matches "Get credential" submit or Approve/Allow/Authorize buttons
        const approveBtn = page.locator(
          'input[type="submit"][value="Get credential"], button:has-text("Approve"), button:has-text("Allow"), button:has-text("Authorize")'
        ).first();
        if (await approveBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
          console.log(`[${moduleName}] Clicking approve/get credential`);
          await approveBtn.click();
          await page.waitForTimeout(5000);
        }
      } catch (browserErr) {
        console.error(`[${moduleName}] Browser interaction error:`, browserErr.message);
      } finally {
        await context.close();
      }
    } else {
      console.log(`[${moduleName}] WAITING but no browser URL found`);
    }
    return false; // Browser auth is the last interaction
  }
}

/**
 * Phase 1: Run issuer/verifier modules. The conformance suite drives the
 * interaction — we create, start, and handle WAITING states which may
 * require credential offers, tx_codes, or browser-based authorization.
 */
async function runServerSideModules(planId, modules, emit) {
  const results = [];
  let browser;

  try {
    browser = await chromium.launch({ args: ['--no-sandbox', '--ignore-certificate-errors'] });

    for (const moduleName of modules) {
      emit({ type: 'module_start', module: moduleName });

      const moduleInfo = await api.createTestFromPlan(planId, moduleName);
      const moduleId = moduleInfo.id;

      let state;
      try {
        state = await api.waitForState(moduleId, ['WAITING', 'FINISHED', 'INTERRUPTED'], 120000);
      } catch (err) {
        const info = await api.getModuleInfo(moduleId).catch(() => ({}));
        emit({ type: 'module_result', module: moduleName, moduleId, status: info.status || 'ERROR', result: info.result || 'TIMEOUT' });
        results.push({ module: moduleName, moduleId, status: info.status || 'ERROR', result: info.result || 'TIMEOUT', passed: false });
        continue;
      }

      // Handle up to 3 WAITING cycles (credential_offer → tx_code → browser auth)
      let waitCycles = 0;
      while (state === 'WAITING' && waitCycles < 5) {
        waitCycles++;
        try {
          const moreWaiting = await handleWaiting(moduleId, moduleName, browser);
          if (!moreWaiting) break;
          // Wait for next state transition
          state = await api.waitForState(moduleId, ['WAITING', 'FINISHED', 'INTERRUPTED'], 60000);
        } catch (waitErr) {
          console.error(`[${moduleName}] Error handling WAITING:`, waitErr.message);
          break;
        }
      }

      // Wait for final result
      if (state !== 'FINISHED' && state !== 'INTERRUPTED') {
        try {
          state = await api.waitForState(moduleId, ['FINISHED', 'INTERRUPTED'], 60000);
        } catch {
          const check = await api.getModuleInfo(moduleId).catch(() => ({}));
          state = check.status || 'INTERRUPTED';
        }
      }

      const finalInfo = await api.getModuleInfo(moduleId);
      const finalResult = finalInfo.result || (state === 'INTERRUPTED' ? 'INTERRUPTED' : 'TIMEOUT');

      // Log failure details for debugging
      if (finalResult !== 'PASSED') {
        try {
          const logs = await api.getTestLog(moduleId);
          const failures = logs.filter(l => l.result === 'FAILURE' || l.result === 'WARNING');
          if (failures.length > 0) {
            console.log(`[${moduleName}] Failure details:`);
            for (const f of failures.slice(0, 5)) {
              console.log(`  [${f.result}] ${f.src}: ${(f.msg || '').slice(0, 300)}`);
            }
          }
        } catch { /* best-effort */ }
      }

      emit({ type: 'module_result', module: moduleName, moduleId, status: finalInfo.status || state, result: finalResult });
      results.push({ module: moduleName, moduleId, status: finalInfo.status || state, result: finalResult, passed: finalResult === 'PASSED' });
    }
  } finally {
    if (browser) await browser.close();
  }

  return results;
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

// Expose startRun as a callable function for MCP
function startRun(planType) {
  const plan = PLANS[planType];
  if (!plan) throw new Error(`Unknown plan type: ${planType}`);

  const id = `run-${++runCounter}-${Date.now()}`;
  const run = {
    id,
    planType,
    label: plan.label,
    phase: plan.phase,
    status: 'creating',
    startedAt: Date.now(),
    planId: null,
    modules: [],
    results: [],
    error: null,
    finishedAt: null,
  };
  runs.set(id, run);
  broadcast('run_start', { id, planType, label: plan.label });

  executeRun(id, plan, planType).catch(err => {
    run.status = 'error';
    run.error = err.message;
    run.finishedAt = Date.now();
    broadcast('run_error', { id, error: err.message });
  });

  return { id, planType };
}

// Set up MCP server
setupMcpServer({
  app,
  api,
  runs,
  plans: PLANS,
  startRun,
  env: {
    CONFORMANCE_URL,
    CONFORMANCE_PUBLIC_URL,
    ISSUER_CONFORMANCE_URL,
    VERIFIER_CONFORMANCE_URL,
    WALLET_FRONTEND_URL: process.env.WALLET_FRONTEND_URL || 'http://wallet-frontend:3000',
    ADMIN_URL: process.env.ADMIN_URL || 'http://amity:8080',
  },
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Conformance runner listening on port ${PORT}`);
  console.log(`Conformance suite URL: ${CONFORMANCE_URL}`);
});
