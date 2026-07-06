#!/usr/bin/env node
/**
 * Android Wallet Conformance Test Runner (USB Device)
 *
 * Runs OID4VCI and OID4VP conformance tests against the Android sample-app
 * running on a USB-connected physical device. Uses `adb shell` directly
 * (instead of `waydroid shell`) to dispatch deep-link intents and monitors
 * logcat for flow completion.
 *
 * Key differences from run-android-conformance.mjs (Waydroid):
 *   - Uses `adb shell` instead of `waydroid shell`
 *   - Device accesses host via `adb reverse` port forwarding (localhost on device = host)
 *   - Supports ADB_SERIAL for multi-device setups
 *
 * Prerequisites:
 *   - Conformance suite running (https://localhost.emobix.co.uk:8443)
 *   - Physical Android device connected via USB with USB debugging enabled
 *   - Port forwarding set up: `make usb-android-setup` or `adb reverse tcp:PORT tcp:PORT`
 *   - Sample app installed on device
 *   - /etc/hosts: 127.0.0.1 localhost.emobix.co.uk
 *   - wallet-backend running and accessible from device at localhost (via adb reverse)
 *
 * Usage:
 *   node run-android-usb-conformance.mjs [--plan vci|vp|all] [--serial DEVICE_SERIAL] [--results-dir DIR]
 *
 * Output:
 *   Writes conformance results to <results-dir>/ (default: ./conformance-results/):
 *     - <profile>-summary.json  (compatible with siros-conformance publish-pages.mjs)
 *     - conformance-report-<planId>.zip  (HTML report from conformance suite)
 *   To publish results: make publish-conformance-results
 *
 * Environment:
 *   CONFORMANCE_URL    - Conformance suite URL (default: https://localhost.emobix.co.uk:8443/)
 *   ADMIN_URL          - Wallet backend admin URL (default: http://localhost:8081)
 *   ADMIN_TOKEN        - Admin auth token
 *   ADB_SERIAL         - Target specific device serial (for multi-device setups)
 */

import { execFile } from 'child_process';
import { promisify } from 'util';
import { readFileSync, writeFileSync, mkdirSync, existsSync, unlinkSync } from 'fs';
import { resolve, dirname, join } from 'path';
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
const serialArg = args.includes('--serial') ? args[args.indexOf('--serial') + 1] : process.env.ADB_SERIAL || '';
const RESULTS_DIR = args.includes('--results-dir')
  ? resolve(args[args.indexOf('--results-dir') + 1])
  : resolve(__dirname, 'conformance-results');

// Build adb command prefix
const ADB_CMD = serialArg ? ['adb', '-s', serialArg] : ['adb'];

// =============================================================================
// ADB Shell (USB device)
// =============================================================================

async function shell(...cmdArgs) {
  try {
    const { stdout } = await execFileAsync(ADB_CMD[0], [...ADB_CMD.slice(1), 'shell', ...cmdArgs], {
      timeout: EXEC_TIMEOUT,
    });
    return (stdout || '').trim();
  } catch (err) {
    throw new Error(`adb shell ${cmdArgs.join(' ')} failed: ${err.message}`);
  }
}

async function clearLogcat() {
  await execFileAsync(ADB_CMD[0], [...ADB_CMD.slice(1), 'logcat', '-c'], { timeout: 5000 });
}

async function getLogcat(lines = 200) {
  const { stdout } = await execFileAsync(ADB_CMD[0], [...ADB_CMD.slice(1), 'logcat', '-d', '-t', String(lines)], {
    timeout: EXEC_TIMEOUT,
  });
  return (stdout || '').trim();
}

async function sendDeepLink(url) {
  console.log(`  → Sending deep link: ${url.slice(0, 100)}...`);
  // Quote the URL to prevent shell metacharacter expansion (& in openid4vp:// URIs)
  const escapedUrl = `'${url.replace(/'/g, "'\\''")}'`;
  await shell('am', 'start', '-a', 'android.intent.action.VIEW', '-d', escapedUrl, '-n', ACTIVITY);
}

// =============================================================================
// Screenshot capture and upload
// =============================================================================

async function captureScreenshot() {
  const remotePath = '/sdcard/conformance-screenshot.png';
  const localPath = join(RESULTS_DIR, `screenshot-${Date.now()}.png`);
  try {
    mkdirSync(RESULTS_DIR, { recursive: true });
    await shell('screencap', '-p', remotePath);
    await execFileAsync(ADB_CMD[0], [...ADB_CMD.slice(1), 'pull', remotePath, localPath], { timeout: 15000 });
    await shell('rm', '-f', remotePath);
    return localPath;
  } catch (err) {
    console.log(`  ⚠ Screenshot capture failed: ${err.message}`);
    return null;
  }
}

async function getPendingImageUploads(moduleId) {
  try {
    const images = await apiRequest('GET', `api/log/${moduleId}/images`);
    return images.filter(item => item.upload);
  } catch (err) {
    console.log(`  ⚠ Could not fetch pending images: ${err.message}`);
    return [];
  }
}

async function uploadScreenshot(moduleId, uploadId, screenshotPath) {
  try {
    const imageData = readFileSync(screenshotPath);
    const base64 = imageData.toString('base64');
    const dataUrl = `data:image/png;base64,${base64}`;

    const url = `${CONFORMANCE_URL}api/log/${encodeURIComponent(moduleId)}/images/${encodeURIComponent(uploadId)}`;
    const res = await fetch(url, {
      method: 'POST',
      body: dataUrl,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      console.log(`  ⚠ Screenshot upload failed: HTTP ${res.status} ${text.slice(0, 200)}`);
      return false;
    }
    console.log(`  ✓ Screenshot uploaded for ${uploadId}`);
    return true;
  } catch (err) {
    console.log(`  ⚠ Screenshot upload error: ${err.message}`);
    return false;
  }
}

async function captureAndUploadScreenshots(moduleId) {
  const pending = await getPendingImageUploads(moduleId);
  if (pending.length === 0) return 0;

  console.log(`  📷 ${pending.length} screenshot(s) required`);
  // Small delay to let the error screen render
  await new Promise(r => setTimeout(r, 1000));

  const screenshotPath = await captureScreenshot();
  if (!screenshotPath) return 0;

  let uploaded = 0;
  for (const item of pending) {
    if (await uploadScreenshot(moduleId, item._id, screenshotPath)) {
      uploaded++;
    }
  }

  // Clean up local screenshot
  try { unlinkSync(screenshotPath); } catch { /* ignore */ }
  return uploaded;
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
      const output = await getLogcat(200);
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
    const output = await getLogcat(50);
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

async function getModuleConditions(moduleId) {
  const logs = await getTestLog(moduleId);
  const counts = {};
  const failures = [];
  for (const entry of logs) {
    const result = entry.result;
    if (result && result !== 'FINISHED') {
      counts[result] = (counts[result] || 0) + 1;
      if (result === 'FAILURE') {
        failures.push({ src: entry.src || '', msg: entry.msg || '' });
      }
    }
  }
  return { counts, failures };
}

async function exportPlanResults(planId) {
  const url = `${CONFORMANCE_URL}api/plan/exporthtml/${planId}`;
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(60000) });
    if (!res.ok) throw new Error(`Export failed: HTTP ${res.status}`);
    const buffer = await res.arrayBuffer();
    mkdirSync(RESULTS_DIR, { recursive: true });
    const filename = `conformance-report-${planId}.zip`;
    const outputPath = join(RESULTS_DIR, filename);
    writeFileSync(outputPath, Buffer.from(buffer));
    const sizeKB = (buffer.byteLength / 1024).toFixed(1);
    console.log(`  Exported report: ${outputPath} (${sizeKB}KB)`);
    return outputPath;
  } catch (err) {
    console.log(`  Warning: could not export HTML report: ${err.message}`);
    return null;
  }
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

  // Ensure app is running (don't force-stop — preserves in-memory credentials from prior plans)
  const pid = await shell('pidof', PACKAGE).catch(() => '');
  if (!pid.trim()) {
    await shell('am', 'start', '-n', ACTIVITY);
    await new Promise(r => setTimeout(r, 3000));
  }

  const results = [];

  for (const moduleName of modules) {
    console.log(`\n─── Module: ${moduleName} ───`);

    // Flush the backend's issuer metadata cache before each VCI module.
    // The conformance suite serves different metadata per test module at the
    // same URL (e.g. notification_endpoint is only present in the notification
    // module). Without this, the backend would use stale cached metadata from
    // the previous module.
    if (ADMIN_TOKEN && planDef.planName.includes('vci')) {
      try {
        await fetch(`${ADMIN_URL}/admin/cache/metadata`, {
          method: 'DELETE',
          headers: { 'Authorization': `Bearer ${ADMIN_TOKEN}` },
        });
      } catch { /* best-effort */ }
    }

    const moduleInfo = await createModule(plan.id, moduleName);
    const moduleId = moduleInfo.id;

    let state;
    try {
      state = await waitForState(moduleId, ['WAITING', 'FINISHED'], 60000);
    } catch (err) {
      console.log(`  ✗ Timeout creating module: ${err.message}`);
      results.push({ module: moduleName, status: 'TIMEOUT', result: 'TIMEOUT', passed: false, conditions: {}, failures: [] });
      continue;
    }

    if (state === 'FINISHED') {
      const info = await getModuleInfo(moduleId);
      const { counts, failures } = await getModuleConditions(moduleId);
      const icon = info.result === 'PASSED' ? '✓' : info.result === 'SKIPPED' ? '○' : '✗';
      console.log(`  ${icon} ${info.result} (no interaction needed)`);
      results.push({ module: moduleName, status: info.status, result: info.result, passed: info.result === 'PASSED' || info.result === 'SKIPPED', conditions: counts, failures });
      continue;
    }

    // WAITING — get interaction URL
    const url = await getWalletInteractionUrl(moduleId);
    if (!url) {
      console.log('  ✗ No interaction URL found in logs');
      const logs = await getRecentSdkLogs();
      if (logs) console.log(`  SDK logs:\n${logs}`);
      results.push({ module: moduleName, status: 'NO_URL', result: 'NO_URL', passed: false, conditions: {}, failures: [] });
      continue;
    }

    // Dispatch to Android device via ADB
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

    // For negative/REVIEW tests: capture and upload a screenshot of the error page
    // The conformance suite requires screenshots showing the wallet rejected the request.
    const isNegativeTest = moduleName.includes('negative-test') || moduleName.includes('multisigned');
    if (isNegativeTest || !flowResult.success) {
      const uploaded = await captureAndUploadScreenshots(moduleId);
      if (uploaded > 0) {
        // Give the suite a moment to process the upload and transition state
        await new Promise(r => setTimeout(r, 2000));
      }
    }

    // Wait for conformance suite final verdict
    // Negative tests need longer — the suite waits for a request that should never come
    const suiteTimeout = isNegativeTest ? 180000 : 60000;
    try {
      await waitForState(moduleId, ['FINISHED'], suiteTimeout);
    } catch {
      console.log('  ⚠ Suite did not finish evaluating in time');
    }

    const finalInfo = await getModuleInfo(moduleId);
    const { counts, failures } = await getModuleConditions(moduleId);
    const icon = finalInfo.result === 'PASSED' ? '✓' : finalInfo.result === 'SKIPPED' ? '○' : '✗';
    console.log(`  ${icon} Suite verdict: ${finalInfo.result}`);
    results.push({
      module: moduleName,
      status: finalInfo.status || 'UNKNOWN',
      result: finalInfo.result || 'UNKNOWN',
      passed: finalInfo.result === 'PASSED' || finalInfo.result === 'SKIPPED',
      conditions: counts,
      failures,
    });

    await new Promise(r => setTimeout(r, 2000));
  }

  // Export HTML report ZIP
  await exportPlanResults(plan.id);

  // Determine profile name for summary
  const profile = planDef.planName.includes('vci') ? 'wallet-vci-android' : 'wallet-vp-android';
  const variantParts = Object.values(planDef.variant || {});
  const variantLabel = variantParts.join(' / ');

  // Write summary JSON (siros-conformance compatible)
  const passed = results.filter(r => r.passed).length;
  const failed = results.filter(r => !r.passed).length;

  const summary = {
    profile,
    plan: planDef.planName,
    planId: plan.id,
    variant: variantLabel,
    timestamp: new Date().toISOString(),
    planDetailUrl: `${CONFORMANCE_URL}plan-detail.html?plan=${plan.id}`,
    total: results.length,
    passed,
    failed,
    modules: results,
    metadata: {
      targetRepo: process.env.TARGET_REPO || 'sirosfoundation/sirosid-dev',
      targetPr: process.env.TARGET_PR || '',
      runId: process.env.GITHUB_RUN_ID || `local-${Date.now()}`,
      actor: process.env.GITHUB_ACTOR || process.env.USER || 'local',
      sha: process.env.GITHUB_SHA || '',
      ref: process.env.GITHUB_REF || '',
      source: 'sirosid-dev/usb-device',
      device: serialArg || 'default',
      images: {
        'wallet-frontend': process.env.WALLET_FRONTEND_IMAGE || '',
        'go-wallet-backend': process.env.WALLET_BACKEND_IMAGE || '',
        'go-trust': process.env.GO_TRUST_IMAGE || '',
      },
      goldenRelease: process.env.GOLDEN_RELEASE || '',
    },
  };

  mkdirSync(RESULTS_DIR, { recursive: true });
  const summaryPath = join(RESULTS_DIR, `${profile}-summary.json`);
  writeFileSync(summaryPath, JSON.stringify(summary, null, 2));
  console.log(`  Summary written: ${summaryPath}`);

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
console.log('║  Android Wallet Conformance Test Runner (USB Device)     ║');
console.log('╚═══════════════════════════════════════════════════════════╝');
console.log(`\nConformance URL: ${CONFORMANCE_URL}`);
console.log(`Plan: ${planArg}`);
console.log(`Admin URL: ${ADMIN_URL}`);
console.log(`ADB target: ${serialArg || '(default device)'}`);
console.log(`Results dir: ${RESULTS_DIR}`);
console.log('');

try {
  // Pre-flight checks
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

  console.log('Checking USB device...');
  const deviceState = await shell('echo', 'connected');
  if (!deviceState.includes('connected')) {
    console.error('ERROR: No USB device connected. Check USB cable and debugging authorization.');
    process.exit(1);
  }
  console.log('  ✓ Device connected');

  // Verify port forwarding
  console.log('Checking port forwarding...');
  try {
    const { stdout } = await execFileAsync(ADB_CMD[0], [...ADB_CMD.slice(1), 'reverse', '--list'], { timeout: 5000 });
    const forwards = (stdout || '').trim().split('\n').filter(Boolean);
    if (forwards.length === 0) {
      console.log('  ⚠ No adb reverse port forwarding detected.');
      console.log('    Run: make usb-android-setup  (or: adb reverse tcp:PORT tcp:PORT for each service port)');
    } else {
      console.log(`  ✓ ${forwards.length} ports forwarded`);
    }
  } catch {
    console.log('  ⚠ Could not check port forwarding');
  }

  console.log('Checking sample app...');
  const packages = await shell('pm', 'list', 'packages', PACKAGE);
  if (!packages.includes(PACKAGE)) {
    console.error(`ERROR: ${PACKAGE} not installed on device`);
    process.exit(1);
  }
  console.log('  ✓ Sample app installed');

  console.log('Checking conformance suite...');
  await waitForReady();
  console.log('  ✓ Conformance suite ready');

  const allResults = [];

  if (planArg === 'vci' || planArg === 'all') {
    const r = await runPlan(VCI_PLAN, 'OID4VCI Wallet (USB Device)');
    allResults.push(r);
  }

  if (planArg === 'vp' || planArg === 'all') {
    const r = await runPlan(VP_PLAN, 'OID4VP Wallet (USB Device)');
    allResults.push(r);
  }

  const totalPassed = allResults.reduce((s, r) => s + r.passed, 0);
  const totalFailed = allResults.reduce((s, r) => s + r.failed, 0);
  console.log(`\n═══ TOTAL: ${totalPassed} passed, ${totalFailed} failed ═══`);
  console.log(`\nResults saved to: ${RESULTS_DIR}`);
  console.log(`To publish: make publish-conformance-results\n`);

  process.exit(totalFailed > 0 ? 1 : 0);
} catch (err) {
  console.error(`\nFATAL: ${err.message}`);
  console.error(err.stack);
  process.exit(2);
}
