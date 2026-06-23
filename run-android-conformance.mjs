#!/usr/bin/env node
/**
 * Android Wallet Conformance Test Runner
 *
 * Runs OID4VCI and OID4VP conformance tests against the Android sample-app
 * running in Waydroid. Uses `waydroid shell` to dispatch deep-link intents
 * and monitors logcat for flow completion.
 *
 * Prerequisites:
 *   - Conformance suite running (https://localhost.emobix.co.uk:8443)
 *   - Waydroid running with sample-app installed
 *   - /etc/hosts: 127.0.0.1 localhost.emobix.co.uk
 *   - wallet-backend running and accessible from Android at 192.168.240.1
 *
 * Usage:
 *   node run-android-conformance.mjs [--plan vci|vp|all]
 *
 * Environment:
 *   CONFORMANCE_URL    - Conformance suite URL (default: https://localhost.emobix.co.uk:8443/)
 *   ADMIN_URL          - Wallet backend admin URL (default: http://localhost:8081)
 *   ADMIN_TOKEN        - Admin auth token
 *   WAYDROID_IP        - Waydroid gateway IP (default: 192.168.240.1 from Android perspective)
 */

import { execFile } from 'child_process';
import { promisify } from 'util';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const execFileAsync = promisify(execFile);
const __dirname = dirname(fileURLToPath(import.meta.url));

// =============================================================================
// Configuration
// =============================================================================

const CONFORMANCE_URL = process.env.CONFORMANCE_URL || 'https://localhost.emobix.co.uk:8443/';
const ADMIN_URL = process.env.ADMIN_URL || 'http://localhost:8081';
const ADMIN_TOKEN = process.env.ADMIN_TOKEN || '';
const PACKAGE = 'org.sirosfoundation.sdk.sample';
const ACTIVITY = `${PACKAGE}/.MainActivity`;
const EXEC_TIMEOUT = 15_000;

const args = process.argv.slice(2);
const planArg = args.includes('--plan') ? args[args.indexOf('--plan') + 1] : 'vci';

// =============================================================================
// Android Shell (via waydroid shell)
// =============================================================================

async function shell(...cmdArgs) {
  // Prefer direct waydroid invocation on host; fallback to sudo when needed.
  try {
    const { stdout } = await execFileAsync('waydroid', ['shell', '--', ...cmdArgs], {
      timeout: EXEC_TIMEOUT,
    });
    return (stdout || '').trim();
  } catch (err) {
    try {
      const { stdout } = await execFileAsync('sudo', ['-n', 'waydroid', 'shell', '--', ...cmdArgs], {
        timeout: EXEC_TIMEOUT,
      });
      return (stdout || '').trim();
    } catch {
      throw err;
    }
  }
}

async function clearLogcat() {
  await shell('logcat', '-c');
}

async function sendDeepLink(url) {
  console.log(`  → Sending deep link: ${url.slice(0, 100)}...`);
  await shell('am', 'start', '-a', 'android.intent.action.VIEW', '-d', url, '-n', ACTIVITY);
}

async function waitForFlowCompletion(timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  const successPatterns = [
    /flow.*complete/i, /credential.*stored/i, /credential.*accepted/i,
    /presentation.*sent/i, /vp.*response.*sent/i, /issuance.*success/i,
    /CredentialStore.*stored/i, /VCI.*flow.*done/i, /OID4VCI.*success/i,
    /offer.*accepted/i,
  ];
  const errorPatterns = [
    /flow.*error/i, /flow.*failed/i, /credential.*error/i,
    /presentation.*error/i, /issuance.*failed/i,
    /java\.lang\.\w*Exception/i, /FATAL EXCEPTION/i,
  ];

  while (Date.now() < deadline) {
    try {
      const output = await shell('logcat', '-d', '-t', '200');
      for (const line of output.split('\n')) {
        if (!/SIROS|SirosWallet|SirosSDK|WalletSDK|CredentialStore|UniFFI|OID4|Wallet/.test(line)) continue;
        for (const p of successPatterns) {
          if (p.test(line)) return { success: true, message: line.trim() };
        }
        for (const p of errorPatterns) {
          if (p.test(line)) return { success: false, message: line.trim() };
        }
      }
    } catch { /* retry */ }
    await new Promise(r => setTimeout(r, 2000));
  }
  return { success: false, message: `Timed out after ${timeoutMs}ms` };
}

async function getRecentSdkLogs() {
  try {
    const output = await shell('logcat', '-d', '-t', '50');
    return output.split('\n')
      .filter(l => /SIROS|SirosWallet|SirosSDK|WalletSDK|CredentialStore|UniFFI|OID4|Wallet/.test(l))
      .slice(-20)
      .join('\n');
  } catch { return '(unavailable)'; }
}

// =============================================================================
// Conformance API Client (minimal, no dependencies)
// =============================================================================

async function apiRequest(method, path, { body, params } = {}) {
  let url = `${CONFORMANCE_URL}${path}`;
  if (params) {
    const sp = new URLSearchParams(params);
    url += (url.includes('?') ? '&' : '?') + sp.toString();
  }
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = body;

  const res = await fetch(url, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${method} ${path} → ${res.status}: ${text.slice(0, 200)}`);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

async function waitForReady() {
  for (let i = 0; i < 30; i++) {
    try {
      await fetch(`${CONFORMANCE_URL}api/runner/available`, { signal: AbortSignal.timeout(5000) });
      return;
    } catch { /* retry */ }
    await new Promise(r => setTimeout(r, 2000));
  }
  throw new Error('Conformance suite not ready');
}

async function createPlan(planName, config, variant) {
  const params = { planName };
  if (variant) params.variant = JSON.stringify(variant);
  return apiRequest('POST', 'api/plan', { params, body: config });
}

async function createModule(planId, testName) {
  return apiRequest('POST', 'api/runner', { params: { test: testName, plan: planId } });
}

async function getModuleInfo(moduleId) {
  return apiRequest('GET', `api/info/${moduleId}`);
}

async function getTestLog(moduleId) {
  return apiRequest('GET', `api/log/${moduleId}`);
}

async function waitForState(moduleId, states, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const info = await getModuleInfo(moduleId);
    if (states.includes(info.status)) return info.status;
    if (info.status === 'INTERRUPTED') throw new Error('Module interrupted');
    await new Promise(r => setTimeout(r, 1500));
  }
  throw new Error(`Timeout waiting for ${states.join('|')}`);
}

async function getWalletInteractionUrl(moduleId) {
  const logs = await getTestLog(moduleId);
  for (const entry of logs) {
    if (entry.credential_offer_redirect_url) return entry.credential_offer_redirect_url;
    if (entry.redirect_to_authorization_endpoint) return entry.redirect_to_authorization_endpoint;
    const msg = entry.msg || '';
    if (msg.includes('openid-credential-offer')) {
      const m = msg.match(/(openid-credential-offer:\/\/[^\s"']+)/);
      if (m) return m[0];
    }
    if (msg.includes('openid4vp://')) {
      const m = msg.match(/(openid4vp:\/\/[^\s"']+)/);
      if (m) return m[0];
    }
    if (msg.includes('request_uri=')) {
      const m = msg.match(/(https?:\/\/[^\s"']+request_uri=[^\s"']+)/);
      if (m) return m[0];
    }
  }
  return null;
}

// =============================================================================
// Plan configs
// =============================================================================

function loadConfig(filename) {
  return readFileSync(resolve(__dirname, 'conformance-runner', filename), 'utf-8');
}

const VCI_PLAN = {
  planName: 'oid4vci-1_0-wallet-test-plan',
  configFile: 'vci-wallet-config.json',
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
};

const VP_PLAN = {
  planName: 'oid4vp-1final-wallet-test-plan',
  configFile: 'vp-wallet-config.json',
  variant: {
    credential_format: 'sd_jwt_vc',
    client_id_prefix: 'x509_san_dns',
    response_mode: 'direct_post',
    request_method: 'request_uri_signed',
    vp_profile: 'plain_vp',
  },
};

// =============================================================================
// Main
// =============================================================================

async function runPlan(planDef, label) {
  console.log(`\n${'═'.repeat(60)}`);
  console.log(`  ${label}`);
  console.log(`${'═'.repeat(60)}\n`);

  const configJson = loadConfig(planDef.configFile);
  const config = JSON.parse(configJson);

  // Register conformance issuer with wallet-backend (if VCI)
  if (ADMIN_TOKEN && planDef.planName.includes('vci')) {
    const issuerUrl = CONFORMANCE_URL.replace(/\/$/, '') + '/test/a/' + (config.alias || 'siros-wallet-vci-test') + '/';
    const clientId = config.client?.client_id || 'siros-wallet-test';
    const clientKey = config.client?.private_key || config.client?.jwks?.keys?.find(k => k.d);
    try {
      const resp = await fetch(`${ADMIN_URL}/admin/tenants/default/issuers`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${ADMIN_TOKEN}`,
        },
        body: JSON.stringify({
          credential_issuer_identifier: issuerUrl,
          client_id: clientId,
          client_jwk: clientKey ? JSON.stringify(clientKey) : null,
          visible: true,
        }),
      });
      console.log(`Registered conformance issuer: ${resp.status}`);
    } catch (e) {
      console.log(`Warning: could not register issuer: ${e.message}`);
    }
  }

  // Create test plan
  const plan = await createPlan(planDef.planName, configJson, planDef.variant);
  const modules = plan.modules.map(m => m.testModule);
  console.log(`Plan created: ${plan.id} (${modules.length} modules)`);
  console.log(`Detail: ${CONFORMANCE_URL}plan-detail.html?plan=${plan.id}\n`);

  // Ensure app is running
  await shell('am', 'force-stop', PACKAGE).catch(() => {});
  await new Promise(r => setTimeout(r, 1000));
  await shell('am', 'start', '-n', ACTIVITY);
  await new Promise(r => setTimeout(r, 3000));

  const results = [];

  for (const moduleName of modules) {
    console.log(`\n─── Module: ${moduleName} ───`);

    const moduleInfo = await createModule(plan.id, moduleName);
    const moduleId = moduleInfo.id;

    let state;
    try {
      state = await waitForState(moduleId, ['WAITING', 'FINISHED'], 60000);
    } catch (err) {
      console.log(`  ✗ Timeout creating module: ${err.message}`);
      results.push({ module: moduleName, result: 'TIMEOUT', passed: false });
      continue;
    }

    if (state === 'FINISHED') {
      const info = await getModuleInfo(moduleId);
      const icon = info.result === 'PASSED' ? '✓' : info.result === 'SKIPPED' ? '○' : '✗';
      console.log(`  ${icon} ${info.result} (no interaction needed)`);
      results.push({ module: moduleName, result: info.result, passed: info.result === 'PASSED' || info.result === 'SKIPPED' });
      continue;
    }

    // WAITING — get interaction URL
    const url = await getWalletInteractionUrl(moduleId);
    if (!url) {
      console.log('  ✗ No interaction URL found in logs');
      const logs = await getRecentSdkLogs();
      if (logs) console.log(`  SDK logs:\n${logs}`);
      results.push({ module: moduleName, result: 'NO_URL', passed: false });
      continue;
    }

    // Dispatch to Android
    await clearLogcat();
    await sendDeepLink(url);

    // Wait for flow completion
    const flowResult = await waitForFlowCompletion(90000);
    if (flowResult.success) {
      console.log(`  ✓ Flow completed: ${flowResult.message.slice(0, 80)}`);
    } else {
      console.log(`  ✗ Flow failed: ${flowResult.message.slice(0, 80)}`);
      const logs = await getRecentSdkLogs();
      if (logs) console.log(`  Recent SDK logs:\n${logs}`);
    }

    // Wait for conformance suite final verdict
    try {
      await waitForState(moduleId, ['FINISHED'], 60000);
    } catch {
      console.log('  ⚠ Suite did not finish evaluating in time');
    }

    const finalInfo = await getModuleInfo(moduleId);
    const icon = finalInfo.result === 'PASSED' ? '✓' : finalInfo.result === 'SKIPPED' ? '○' : '✗';
    console.log(`  ${icon} Suite verdict: ${finalInfo.result}`);
    results.push({
      module: moduleName,
      result: finalInfo.result || 'UNKNOWN',
      passed: finalInfo.result === 'PASSED' || finalInfo.result === 'SKIPPED',
    });

    await new Promise(r => setTimeout(r, 2000));
  }

  // Summary
  const passed = results.filter(r => r.passed).length;
  const failed = results.filter(r => !r.passed).length;
  console.log(`\n${'─'.repeat(60)}`);
  console.log(`  ${label} Summary: ${passed} passed, ${failed} failed (${results.length} total)`);
  console.log(`  Plan detail: ${CONFORMANCE_URL}plan-detail.html?plan=${plan.id}`);
  console.log(`${'─'.repeat(60)}\n`);

  return { planId: plan.id, results, passed, failed };
}

// =============================================================================
// Entry point
// =============================================================================

console.log('╔═══════════════════════════════════════════════════════════╗');
console.log('║  Android Wallet Conformance Test Runner                  ║');
console.log('╚═══════════════════════════════════════════════════════════╝');
console.log(`\nConformance URL: ${CONFORMANCE_URL}`);
console.log(`Plan: ${planArg}`);
console.log(`Admin URL: ${ADMIN_URL}`);
console.log('');

try {
  // Pre-flight checks
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

  console.log('Checking Waydroid...');
  const packages = await shell('pm', 'list', 'packages', PACKAGE);
  if (!packages.includes(PACKAGE)) {
    console.error(`ERROR: ${PACKAGE} not installed in Waydroid`);
    process.exit(1);
  }
  console.log('  ✓ Sample app installed');

  console.log('Checking conformance suite...');
  await waitForReady();
  console.log('  ✓ Conformance suite ready');

  const allResults = [];

  if (planArg === 'vci' || planArg === 'all') {
    const r = await runPlan(VCI_PLAN, 'OID4VCI Wallet (Android)');
    allResults.push(r);
  }

  if (planArg === 'vp' || planArg === 'all') {
    const r = await runPlan(VP_PLAN, 'OID4VP Wallet (Android)');
    allResults.push(r);
  }

  const totalPassed = allResults.reduce((s, r) => s + r.passed, 0);
  const totalFailed = allResults.reduce((s, r) => s + r.failed, 0);
  console.log(`\n═══ TOTAL: ${totalPassed} passed, ${totalFailed} failed ═══\n`);

  process.exit(totalFailed > 0 ? 1 : 0);
} catch (err) {
  console.error(`\nFATAL: ${err.message}`);
  console.error(err.stack);
  process.exit(2);
}
