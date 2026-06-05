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
 *   GET  /api/events               - SSE event stream
 */

import express from 'express';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { ConformanceAPI } from './lib/conformance-api.mjs';
import { runWalletVCI, runWalletVP } from './lib/wallet-runner.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());

const PORT = parseInt(process.env.PORT || '3001', 10);
const CONFORMANCE_URL = process.env.CONFORMANCE_URL || 'https://localhost.emobix.co.uk:8443/';
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
    variant: {
      credential_format: 'sd_jwt_vc',
      sender_constrain: 'dpop',
      client_auth_type: 'private_key_jwt',
      vci_grant_type: 'pre_authorization_code',
      fapi_profile: 'vci',
      fapi_request_method: 'unsigned',
      fapi_response_mode: 'plain_response',
      openid: 'plain_oauth',
      authorization_request_type: 'simple',
      vci_authorization_code_flow_variant: 'issuer_initiated',
      vci_credential_encryption: 'plain',
    },
    loadConfig() {
      return loadConfig(this.configFile, {
        ISSUER_DISCOVERY_URL: `${ISSUER_CONFORMANCE_URL}/.well-known/openid-credential-issuer`,
        ISSUER_RESOURCE_URL: ISSUER_CONFORMANCE_URL,
        ISSUER_CREDENTIAL_ISSUER_URL: ISSUER_CONFORMANCE_URL,
      });
    },
  },
  'vp-verifier': {
    label: 'OID4VP Verifier',
    planName: 'oid4vp-1final-rp-test-plan',
    configFile: 'vp-verifier-config.json',
    phase: 1,
    variant: {
      credential_format: 'sd_jwt_vc',
      client_id_prefix: 'x509_san_dns',
      response_mode: 'direct_post',
      request_method: 'request_uri_signed',
      vp_profile: 'plain_vp',
    },
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
    loadConfig() { return loadConfig(this.configFile); },
  },
  'vp-wallet': {
    label: 'OID4VP Wallet',
    planName: 'oid4vp-1final-wallet-test-plan',
    configFile: 'vp-wallet-config.json',
    phase: 2,
    variant: {
      credential_format: 'sd_jwt_vc',
      client_id_prefix: 'x509_san_dns',
      response_mode: 'direct_post',
      request_method: 'request_uri_signed',
      vp_profile: 'plain_vp',
    },
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
  res.json({ conformance_suite: ready ? 'connected' : 'unavailable', url: CONFORMANCE_URL });
});

app.get('/api/plans', (_req, res) => {
  const list = Object.entries(PLANS).map(([id, p]) => ({
    id,
    label: p.label,
    planName: p.planName,
    phase: p.phase,
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
  const plan = PLANS[planType];
  if (!plan) {
    return res.status(400).json({ error: `Unknown plan type. Choose: ${Object.keys(PLANS).join(', ')}` });
  }

  // Check if conformance suite is reachable
  const ready = await api.isReady();
  if (!ready) {
    return res.status(503).json({ error: 'Conformance suite not available' });
  }

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

  // Run async
  executeRun(id, plan, planType).catch(err => {
    run.status = 'error';
    run.error = err.message;
    run.finishedAt = Date.now();
    broadcast('run_error', { id, error: err.message });
  });

  res.status(201).json({ id, planType });
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

  // Send current state for any active runs
  for (const run of runs.values()) {
    if (run.status === 'running' || run.status === 'creating') {
      res.write(`event: run_state\ndata: ${JSON.stringify(run)}\n\n`);
    }
  }

  req.on('close', () => sseClients.delete(res));
});

// ---------------------------------------------------------------------------
// Run execution
// ---------------------------------------------------------------------------

async function executeRun(id, plan, planType) {
  const run = runs.get(id);

  try {
    // Load config
    run.status = 'creating';
    broadcast('run_update', { id, status: 'creating' });
    const configJson = plan.loadConfig();

    // Create test plan
    const testPlan = await api.createTestPlan(plan.planName, configJson, plan.variant);
    run.planId = testPlan.id;
    run.modules = testPlan.modules.map(m => m.testModule);
    run.status = 'running';
    broadcast('run_update', { id, status: 'running', planId: testPlan.id, modules: run.modules });

    const emit = (event) => {
      broadcast('module_event', { runId: id, ...event });
      if (event.type === 'module_result') {
        run.results.push({ module: event.module, status: event.status, result: event.result });
      }
    };

    let results;

    if (plan.phase === 2) {
      // Wallet tests — need Playwright
      if (planType === 'vci-wallet') {
        results = await runWalletVCI(api, testPlan.id, run.modules, configJson, emit);
      } else {
        results = await runWalletVP(api, testPlan.id, run.modules, configJson, emit);
      }
    } else {
      // Phase 1 — issuer/verifier tests: conformance suite drives, we just poll
      results = await runServerSideModules(testPlan.id, run.modules, emit);
    }

    run.results = results;
    run.status = 'finished';
    run.finishedAt = Date.now();

    const passed = results.filter(r => r.passed).length;
    const failed = results.filter(r => !r.passed).length;
    broadcast('run_finished', {
      id,
      passed,
      failed,
      total: results.length,
      planDetailUrl: api.getPlanDetailUrl(testPlan.id),
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
 * Phase 1: Run issuer/verifier modules. The conformance suite drives the
 * interaction — we just create, start, and poll each module.
 */
async function runServerSideModules(planId, modules, emit) {
  const results = [];

  for (const moduleName of modules) {
    emit({ type: 'module_start', module: moduleName });

    const moduleInfo = await api.createTestFromPlan(planId, moduleName);
    const moduleId = moduleInfo.id;

    let state;
    try {
      state = await api.waitForState(moduleId, ['WAITING', 'FINISHED', 'INTERRUPTED'], 120000);
    } catch (err) {
      // True timeout — couldn't determine state
      const info = await api.getModuleInfo(moduleId).catch(() => ({}));
      emit({ type: 'module_result', module: moduleName, status: info.status || 'ERROR', result: info.result || 'TIMEOUT' });
      results.push({ module: moduleName, status: info.status || 'ERROR', result: info.result || 'TIMEOUT', passed: false });
      continue;
    }

    if (state === 'FINISHED' || state === 'INTERRUPTED') {
      const info = await api.getModuleInfo(moduleId);
      emit({ type: 'module_result', module: moduleName, status: info.status, result: info.result });
      results.push({ module: moduleName, status: info.status, result: info.result, passed: info.result === 'PASSED' });
      continue;
    }

    // WAITING — for issuer tests, may need browser interaction at mock AS
    // For now, just wait for it to finish (mock AS auto-approves)
    try {
      await api.waitForState(moduleId, ['FINISHED'], 120000);
    } catch {}

    const finalInfo = await api.getModuleInfo(moduleId);
    emit({ type: 'module_result', module: moduleName, status: finalInfo.status, result: finalInfo.result });
    results.push({ module: moduleName, status: finalInfo.status, result: finalInfo.result, passed: finalInfo.result === 'PASSED' });
  }

  return results;
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Conformance runner listening on port ${PORT}`);
  console.log(`Conformance suite URL: ${CONFORMANCE_URL}`);
});
