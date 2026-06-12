/**
 * Wallet automation runner for conformance tests.
 * Drives Playwright headless Chromium to interact with the wallet-frontend
 * for VCI (wallet) and VP (wallet) test plans.
 */

import { chromium } from 'playwright';

const FRONTEND_URL = process.env.WALLET_FRONTEND_URL || 'http://wallet-frontend:3000';
const ADMIN_URL = process.env.ADMIN_URL || 'http://amity:8080';
const ADMIN_TOKEN = process.env.ADMIN_TOKEN || '';

/**
 * Convert a conformance suite URL to a wallet /cb callback URL.
 */
function convertToWalletCallbackUrl(url, tenantId) {
  const base = tenantId ? `${FRONTEND_URL}/id/${tenantId}` : FRONTEND_URL;
  if (url.startsWith(FRONTEND_URL)) return url;
  if (url.startsWith('openid-credential-offer://')) {
    return `${base}/cb?${url.replace('openid-credential-offer://?', '')}`;
  }
  if (url.startsWith('openid4vp://')) {
    return `${base}/cb?${url.replace('openid4vp://?', '')}`;
  }
  try {
    const parsed = new URL(url);
    return `${base}/cb?${parsed.searchParams.toString()}`;
  } catch {
    return url;
  }
}

/**
 * Run VCI wallet conformance modules using Playwright.
 */
export async function runWalletVCI(api, planId, modules, configJson, emit) {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();

  // Set up CDP virtual authenticator for WebAuthn
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasUserVerification: true,
      isUserVerified: true,
      hasResidentKey: true,
    },
  });

  try {
    // Create a test tenant
    emit({ type: 'log', message: 'Creating test tenant...' });
    const tenantId = `conf-vci-${Date.now()}`;
    const username = `user-${tenantId}`;

    // Navigate to wallet, register user
    await page.goto(`${FRONTEND_URL}/id/${tenantId}/login`, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(3000);

    // Try to register
    const registerBtn = page.locator('button:has-text("Register"), a:has-text("Register")').first();
    if (await registerBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await registerBtn.click();
      await page.waitForTimeout(2000);
    }

    // Fill registration
    const nameInput = page.locator('input[name="username"], input[name="displayName"]').first();
    if (await nameInput.isVisible({ timeout: 5000 }).catch(() => false)) {
      await nameInput.fill(username);
      const submitBtn = page.locator('button[type="submit"], button:has-text("Create")').first();
      if (await submitBtn.isVisible({ timeout: 2000 })) {
        await submitBtn.click();
        await page.waitForTimeout(5000);
      }
    }

    // Dismiss welcome tour
    const dismissBtn = page.locator('button:has-text("Dismiss")');
    if (await dismissBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await dismissBtn.click();
      await page.waitForTimeout(1000);
    }

    // Register conformance issuer
    const config = JSON.parse(configJson);
    const conformanceClientId = config.client?.client_id || 'siros-wallet-test';
    const conformanceIssuerUrl = api.baseUrl.replace(/\/$/, '') + '/test/a/' + (config.alias || 'siros-wallet-vci-test') + '/';
    const clientKeyWithPrivate = config.client?.private_key || config.client?.jwks?.keys?.find(k => k.d);
    const clientPrivateKeyJwk = clientKeyWithPrivate ? JSON.stringify(clientKeyWithPrivate) : null;

    if (ADMIN_TOKEN) {
      const issuerResp = await fetch(`${ADMIN_URL}/admin/tenants/${tenantId}/issuers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${ADMIN_TOKEN}` },
        body: JSON.stringify({
          credential_issuer_identifier: conformanceIssuerUrl,
          client_id: conformanceClientId,
          client_jwk: clientPrivateKeyJwk,
          visible: true,
        }),
      });
      emit({ type: 'log', message: `Registered conformance issuer: ${issuerResp.status}` });
    }

    // Handle browser console + dialogs
    page.on('dialog', async dialog => {
      if (dialog.type() === 'prompt') await dialog.accept('123456');
      else await dialog.accept();
    });

    const results = [];

    for (const moduleName of modules) {
      emit({ type: 'module_start', module: moduleName });

      const moduleInfo = await api.createTestFromPlan(planId, moduleName);
      const moduleId = moduleInfo.id;

      let state;
      try {
        state = await api.waitForState(moduleId, ['WAITING', 'FINISHED'], 60000);
      } catch (err) {
        emit({ type: 'module_result', module: moduleName, status: 'ERROR', result: 'TIMEOUT' });
        results.push({ module: moduleName, status: 'ERROR', result: 'TIMEOUT', passed: false });
        continue;
      }

      if (state === 'FINISHED') {
        const info = await api.getModuleInfo(moduleId);
        emit({ type: 'module_result', module: moduleName, status: info.status, result: info.result });
        results.push({ module: moduleName, status: info.status, result: info.result, passed: info.result === 'PASSED' });
        continue;
      }

      // WAITING — get interaction URL and drive wallet
      const interactionUrl = await api.getWalletInteractionUrl(moduleId);
      if (!interactionUrl) {
        emit({ type: 'module_result', module: moduleName, status: 'ERROR', result: 'NO_URL' });
        results.push({ module: moduleName, status: 'ERROR', result: 'NO_URL', passed: false });
        continue;
      }

      emit({ type: 'log', message: `${moduleName}: accepting offer ${interactionUrl.slice(0, 80)}...` });

      // SPA pushState injection (preserves in-memory keystore)
      const offerParams = interactionUrl.replace('openid-credential-offer://?', '');
      const tenantBasePath = `/id/${tenantId}/`;
      await page.evaluate(({ basePath, params }) => {
        window.history.pushState(null, '', `${basePath}?${params}`);
        window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
      }, { basePath: tenantBasePath, params: offerParams });

      try {
        await page.waitForURL(url => url.pathname.includes('/cb'), { timeout: 10000 });
      } catch {
        // Fallback: full navigation
        const cbUrl = `${FRONTEND_URL}/id/${tenantId}/cb?${offerParams}`;
        await page.goto(cbUrl, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(2000);

        // Re-login if needed
        const loginBtn = page.locator('button:has-text("Log in with a Passkey")');
        if (await loginBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
          await loginBtn.click();
          await page.waitForTimeout(5000);
        }
      }

      // Handle Welcome tour
      if (await dismissBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
        await dismissBtn.click();
        await page.waitForTimeout(1000);
      }

      // Handle TX code popup
      const txCodeInput = page.locator('input[placeholder*="character code"]');
      try {
        await txCodeInput.waitFor({ state: 'visible', timeout: 15000 });
        await txCodeInput.fill('123456');
        await page.locator('button:has-text("Submit")').click();
      } catch {}

      await page.waitForTimeout(15000);

      // Wait for finish
      try {
        await api.waitForState(moduleId, ['FINISHED'], 60000);
      } catch {}

      const finalInfo = await api.getModuleInfo(moduleId);
      emit({ type: 'module_result', module: moduleName, status: finalInfo.status, result: finalInfo.result });
      results.push({ module: moduleName, status: finalInfo.status, result: finalInfo.result, passed: finalInfo.result === 'PASSED' });

      // Navigate home between modules
      await page.goto(`${FRONTEND_URL}/`, { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1000);
    }

    return results;
  } finally {
    await browser.close();
  }
}

/**
 * Run VP wallet conformance modules using Playwright.
 */
export async function runWalletVP(api, planId, modules, configJson, emit) {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();

  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasUserVerification: true,
      isUserVerified: true,
      hasResidentKey: true,
    },
  });

  try {
    emit({ type: 'log', message: 'Setting up wallet for VP tests...' });
    const tenantId = `conf-vp-${Date.now()}`;

    // Navigate to wallet, register
    await page.goto(`${FRONTEND_URL}/id/${tenantId}/login`, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(3000);

    const registerBtn = page.locator('button:has-text("Register"), a:has-text("Register")').first();
    if (await registerBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await registerBtn.click();
      await page.waitForTimeout(2000);
    }

    const nameInput = page.locator('input[name="username"], input[name="displayName"]').first();
    if (await nameInput.isVisible({ timeout: 5000 }).catch(() => false)) {
      await nameInput.fill(`user-${tenantId}`);
      const submitBtn = page.locator('button[type="submit"], button:has-text("Create")').first();
      if (await submitBtn.isVisible({ timeout: 2000 })) {
        await submitBtn.click();
        await page.waitForTimeout(5000);
      }
    }

    const dismissBtn = page.locator('button:has-text("Dismiss")');
    if (await dismissBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await dismissBtn.click();
      await page.waitForTimeout(1000);
    }

    // TODO: Pre-load a credential for VP tests
    // This requires issuing from the mock issuer first
    emit({ type: 'log', message: 'VP wallet test pre-loading not yet implemented — running modules anyway' });

    page.on('dialog', async dialog => {
      if (dialog.type() === 'prompt') await dialog.accept('123456');
      else await dialog.accept();
    });

    const results = [];

    for (const moduleName of modules) {
      emit({ type: 'module_start', module: moduleName });

      const moduleInfo = await api.createTestFromPlan(planId, moduleName);
      const moduleId = moduleInfo.id;

      let state;
      try {
        state = await api.waitForState(moduleId, ['WAITING', 'FINISHED'], 60000);
      } catch (err) {
        emit({ type: 'module_result', module: moduleName, status: 'ERROR', result: 'TIMEOUT' });
        results.push({ module: moduleName, status: 'ERROR', result: 'TIMEOUT', passed: false });
        continue;
      }

      if (state === 'FINISHED') {
        const info = await api.getModuleInfo(moduleId);
        emit({ type: 'module_result', module: moduleName, status: info.status, result: info.result });
        results.push({ module: moduleName, status: info.status, result: info.result, passed: info.result === 'PASSED' });
        continue;
      }

      // Get VP interaction URL
      const interactionUrl = await api.getWalletInteractionUrl(moduleId);
      if (!interactionUrl) {
        emit({ type: 'module_result', module: moduleName, status: 'ERROR', result: 'NO_URL' });
        results.push({ module: moduleName, status: 'ERROR', result: 'NO_URL', passed: false });
        continue;
      }

      emit({ type: 'log', message: `${moduleName}: presenting credential ${interactionUrl.slice(0, 80)}...` });

      const walletUrl = convertToWalletCallbackUrl(interactionUrl, tenantId);
      await page.goto(walletUrl, { waitUntil: 'networkidle', timeout: 30000 });
      await page.waitForTimeout(5000);

      // Check for errors
      const insufficientError = page.locator('text=Insufficient Credentials');
      if (await insufficientError.isVisible({ timeout: 3000 }).catch(() => false)) {
        emit({ type: 'module_result', module: moduleName, status: 'ERROR', result: 'NO_CREDENTIAL' });
        results.push({ module: moduleName, status: 'ERROR', result: 'NO_CREDENTIAL', passed: false });
        continue;
      }

      // Step 1: Preview → Next
      const nextButton = page.locator('#next-select-credentials');
      try {
        await nextButton.waitFor({ state: 'visible', timeout: 10000 });
        await nextButton.click();
        await page.waitForTimeout(1000);

        // Step 2: Select credential → Next
        const cards = page.locator('[id^="slider-select-credentials-"]');
        if (await cards.count() > 0) {
          await cards.first().click();
          await page.waitForTimeout(500);
        }
        await nextButton.click();
        await page.waitForTimeout(1000);

        // Step 3: Summary → Send
        const sendButton = page.locator('#send-select-credentials');
        await sendButton.waitFor({ state: 'visible', timeout: 5000 });
        await sendButton.click();

        const consentBtn = page.locator('#consent');
        if (await consentBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
          await consentBtn.click();
        }

        await page.waitForTimeout(3000);
      } catch (err) {
        emit({ type: 'log', message: `${moduleName}: VP flow error: ${err.message}` });
      }

      try {
        await api.waitForState(moduleId, ['FINISHED'], 60000);
      } catch {}

      const finalInfo = await api.getModuleInfo(moduleId);
      emit({ type: 'module_result', module: moduleName, status: finalInfo.status, result: finalInfo.result });
      results.push({ module: moduleName, status: finalInfo.status, result: finalInfo.result, passed: finalInfo.result === 'PASSED' });

      await page.goto(`${FRONTEND_URL}/`, { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1000);
    }

    return results;
  } finally {
    await browser.close();
  }
}
