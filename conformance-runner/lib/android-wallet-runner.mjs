/**
 * Android Wallet Runner for conformance tests.
 *
 * Drives the Android sample-app via `waydroid shell` (or ADB) by sending
 * deep-link intents and monitoring logcat for flow completion.
 *
 * Works with either:
 *   - `waydroid shell` (host-local, no auth needed)
 *   - `adb` (remote/Waydroid over TCP)
 */

import { execFile, spawn } from 'child_process';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

const PACKAGE = 'org.sirosfoundation.sdk.sample';
const ACTIVITY = `${PACKAGE}/.MainActivity`;
const EXEC_TIMEOUT = 15_000;

// Prefer waydroid shell over ADB since it doesn't need auth
const USE_WAYDROID = process.env.USE_WAYDROID !== '0';
const ADB_PATH = process.env.ADB_PATH || 'adb';
const WAYDROID_IP = process.env.WAYDROID_IP || '192.168.240.112';

/**
 * Execute a shell command on the Android system.
 */
async function androidShell(...args) {
  if (USE_WAYDROID) {
    const { stdout } = await execFileAsync('sudo', ['waydroid', 'shell', '--', ...args], {
      timeout: EXEC_TIMEOUT,
    });
    return (stdout || '').trim();
  } else {
    const { stdout } = await execFileAsync(ADB_PATH, ['shell', ...args], {
      timeout: EXEC_TIMEOUT,
    });
    return (stdout || '').trim();
  }
}

/**
 * Check if the sample-app is installed.
 */
export async function isAppInstalled() {
  const result = await androidShell('pm', 'list', 'packages', PACKAGE);
  return result.includes(PACKAGE);
}

/**
 * Launch the sample-app.
 */
export async function launchApp() {
  await androidShell('am', 'force-stop', PACKAGE).catch(() => {});
  await androidShell('am', 'start', '-n', ACTIVITY);
  await new Promise(r => setTimeout(r, 3000));
}

/**
 * Clear logcat buffer.
 */
export async function clearLogcat() {
  await androidShell('logcat', '-c');
}

/**
 * Send a deep-link intent to the sample-app.
 */
export async function sendDeepLink(url) {
  // Use am start with VIEW action — the app's intent filter catches these schemes
  await androidShell(
    'am', 'start',
    '-a', 'android.intent.action.VIEW',
    '-d', url,
    '-n', ACTIVITY,
  );
}

/**
 * Wait for a flow completion signal in logcat.
 * Returns { success: boolean, message: string }
 */
export async function waitForFlowCompletion(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;

  const successPatterns = [
    /flow.*complete/i,
    /credential.*stored/i,
    /credential.*accepted/i,
    /presentation.*sent/i,
    /vp.*response.*sent/i,
    /issuance.*success/i,
    /CredentialStore.*stored/i,
    /VCI.*flow.*done/i,
  ];
  const errorPatterns = [
    /flow.*error/i,
    /flow.*failed/i,
    /credential.*error/i,
    /presentation.*error/i,
    /issuance.*failed/i,
    /Exception|FATAL/i,
  ];

  while (Date.now() < deadline) {
    try {
      const output = await androidShell('logcat', '-d', '-t', '200');

      for (const line of output.split('\n')) {
        // Only consider lines from our SDK tags
        if (!/SIROS|SirosWallet|SirosSDK|WalletSDK|CredentialStore|UniFFI/.test(line)) continue;

        for (const pattern of successPatterns) {
          if (pattern.test(line)) {
            return { success: true, message: line.trim() };
          }
        }
        for (const pattern of errorPatterns) {
          if (pattern.test(line)) {
            return { success: false, message: line.trim() };
          }
        }
      }
    } catch {
      // logcat read failed, retry
    }

    await new Promise(r => setTimeout(r, 2000));
  }

  return { success: false, message: `Timed out after ${timeoutMs}ms waiting for flow completion` };
}

/**
 * Get recent logcat lines filtered for SIROS-related output.
 */
export async function getRecentLogs(lines = 100) {
  try {
    const output = await androidShell('logcat', '-d', '-t', String(lines));
    return output.split('\n')
      .filter(l => /SIROS|SirosWallet|SirosSDK|WalletSDK|CredentialStore|UniFFI|Wallet/.test(l))
      .join('\n');
  } catch {
    return '(logcat unavailable)';
  }
}

/**
 * Run VCI wallet conformance modules against the Android sample-app.
 *
 * The conformance suite acts as issuer. For each module:
 * 1. Create the test module → it enters WAITING state
 * 2. Extract the credential_offer URL from the logs
 * 3. Send it as a deep-link intent to the Android app
 * 4. Monitor logcat for completion
 * 5. Wait for the conformance suite to finish evaluating
 */
export async function runAndroidWalletVCI(api, planId, modules, configJson, emit) {
  const results = [];

  // Check app is ready
  const installed = await isAppInstalled();
  if (!installed) {
    throw new Error('Android sample-app is not installed in Waydroid');
  }

  // Launch app fresh
  await launchApp();
  emit({ type: 'log', message: 'Android sample-app launched' });

  for (const moduleName of modules) {
    emit({ type: 'module_start', module: moduleName });
    console.log(`\n=== [Android VCI] Running module: ${moduleName} ===`);

    const moduleInfo = await api.createTestFromPlan(planId, moduleName);
    const moduleId = moduleInfo.id;
    console.log(`Module ${moduleName} created: ${moduleId}`);

    let state;
    try {
      state = await api.waitForState(moduleId, ['WAITING', 'FINISHED'], 60000);
    } catch (err) {
      console.log(`Module ${moduleName} timed out: ${err.message}`);
      emit({ type: 'module_result', module: moduleName, moduleId, status: 'ERROR', result: 'TIMEOUT' });
      results.push({ module: moduleName, moduleId, status: 'ERROR', result: 'TIMEOUT', passed: false });
      continue;
    }

    if (state === 'FINISHED') {
      const info = await api.getModuleInfo(moduleId);
      console.log(`Module ${moduleName} finished immediately: ${info.result}`);
      emit({ type: 'module_result', module: moduleName, moduleId, status: info.status, result: info.result });
      results.push({ module: moduleName, moduleId, status: info.status, result: info.result, passed: info.result === 'PASSED' || info.result === 'SKIPPED' });
      continue;
    }

    // Module is WAITING — get the credential offer URL
    const interactionUrl = await api.getWalletInteractionUrl(moduleId);
    if (!interactionUrl) {
      console.log(`Module ${moduleName}: no interaction URL found`);
      emit({ type: 'module_result', module: moduleName, moduleId, status: 'ERROR', result: 'NO_URL' });
      results.push({ module: moduleName, moduleId, status: 'ERROR', result: 'NO_URL', passed: false });
      continue;
    }

    console.log(`Module ${moduleName}: sending offer to Android: ${interactionUrl.slice(0, 80)}...`);
    emit({ type: 'log', message: `${moduleName}: dispatching deep link` });

    // Clear logcat before sending the intent
    await clearLogcat();

    // Send the deep link to the Android app
    await sendDeepLink(interactionUrl);

    // Wait for the SDK to process the flow
    const flowResult = await waitForFlowCompletion(90000);
    console.log(`Module ${moduleName}: flow ${flowResult.success ? 'completed' : 'failed'}: ${flowResult.message}`);
    emit({ type: 'log', message: `${moduleName}: ${flowResult.success ? 'OK' : 'FAIL'} — ${flowResult.message}` });

    // Wait for conformance suite to finish evaluating
    try {
      await api.waitForState(moduleId, ['FINISHED'], 60000);
    } catch {
      console.log(`Module ${moduleName} did not finish in time`);
    }

    const finalInfo = await api.getModuleInfo(moduleId);
    console.log(`Module ${moduleName} result: ${finalInfo.result}`);
    emit({ type: 'module_result', module: moduleName, moduleId, status: finalInfo.status, result: finalInfo.result });
    results.push({
      module: moduleName,
      moduleId,
      status: finalInfo.status,
      result: finalInfo.result,
      passed: finalInfo.result === 'PASSED' || finalInfo.result === 'SKIPPED',
    });

    // Brief pause between modules
    await new Promise(r => setTimeout(r, 2000));
  }

  return results;
}

/**
 * Run VP wallet conformance modules against the Android sample-app.
 *
 * The conformance suite acts as verifier. For each module:
 * 1. Create the test module → it enters WAITING state
 * 2. Extract the openid4vp:// request URL from the logs
 * 3. Send it as a deep-link intent to the Android app
 * 4. Monitor logcat for completion
 * 5. Wait for the conformance suite to finish evaluating
 */
export async function runAndroidWalletVP(api, planId, modules, configJson, emit) {
  const results = [];

  // Check app is ready
  const installed = await isAppInstalled();
  if (!installed) {
    throw new Error('Android sample-app is not installed in Waydroid');
  }

  // Launch app fresh
  await launchApp();
  emit({ type: 'log', message: 'Android sample-app launched' });

  for (const moduleName of modules) {
    emit({ type: 'module_start', module: moduleName });
    console.log(`\n=== [Android VP] Running module: ${moduleName} ===`);

    const moduleInfo = await api.createTestFromPlan(planId, moduleName);
    const moduleId = moduleInfo.id;
    console.log(`Module ${moduleName} created: ${moduleId}`);

    let state;
    try {
      state = await api.waitForState(moduleId, ['WAITING', 'FINISHED'], 60000);
    } catch (err) {
      console.log(`Module ${moduleName} timed out: ${err.message}`);
      emit({ type: 'module_result', module: moduleName, moduleId, status: 'ERROR', result: 'TIMEOUT' });
      results.push({ module: moduleName, moduleId, status: 'ERROR', result: 'TIMEOUT', passed: false });
      continue;
    }

    if (state === 'FINISHED') {
      const info = await api.getModuleInfo(moduleId);
      emit({ type: 'module_result', module: moduleName, moduleId, status: info.status, result: info.result });
      results.push({ module: moduleName, moduleId, status: info.status, result: info.result, passed: info.result === 'PASSED' || info.result === 'SKIPPED' });
      continue;
    }

    // Module is WAITING — get the VP request URL
    const interactionUrl = await api.getWalletInteractionUrl(moduleId);
    if (!interactionUrl) {
      emit({ type: 'module_result', module: moduleName, moduleId, status: 'ERROR', result: 'NO_URL' });
      results.push({ module: moduleName, moduleId, status: 'ERROR', result: 'NO_URL', passed: false });
      continue;
    }

    console.log(`Module ${moduleName}: sending VP request to Android: ${interactionUrl.slice(0, 80)}...`);
    emit({ type: 'log', message: `${moduleName}: dispatching VP deep link` });

    await clearLogcat();
    await sendDeepLink(interactionUrl);

    const flowResult = await waitForFlowCompletion(90000);
    console.log(`Module ${moduleName}: flow ${flowResult.success ? 'completed' : 'failed'}: ${flowResult.message}`);
    emit({ type: 'log', message: `${moduleName}: ${flowResult.success ? 'OK' : 'FAIL'} — ${flowResult.message}` });

    try {
      await api.waitForState(moduleId, ['FINISHED'], 60000);
    } catch {
      console.log(`Module ${moduleName} did not finish in time`);
    }

    const finalInfo = await api.getModuleInfo(moduleId);
    emit({ type: 'module_result', module: moduleName, moduleId, status: finalInfo.status, result: finalInfo.result });
    results.push({
      module: moduleName,
      moduleId,
      status: finalInfo.status,
      result: finalInfo.result,
      passed: finalInfo.result === 'PASSED' || finalInfo.result === 'SKIPPED',
    });

    await new Promise(r => setTimeout(r, 2000));
  }

  return results;
}
