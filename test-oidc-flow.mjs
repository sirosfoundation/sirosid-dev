// Test script: verify OIDC redirect chain from authorize → consent → mini-oidc → callback
import { chromium } from 'playwright';

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

async function createPAR() {
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: 'e2e-test-client',
    redirect_uri: 'http://localhost:3000/cb',
    scope: 'pid_1_8',
    state: 'teststate123',
    code_challenge: 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk',
    code_challenge_method: 'S256',
  });

  const resp = await fetch('https://vc-proxy:8443/op/par', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: params.toString(),
  });
  const body = await resp.json();
  console.log('PAR response:', resp.status, JSON.stringify(body));
  return body.request_uri;
}

const requestUri = await createPAR();
if (!requestUri) {
  console.log('PAR failed');
  process.exit(1);
}

const authorizeUrl = `https://vc-proxy:8443/authorize?client_id=e2e-test-client&request_uri=${encodeURIComponent(requestUri)}`;
console.log('Authorize URL:', authorizeUrl);

const browser = await chromium.launch({ args: ['--no-sandbox', '--ignore-certificate-errors'] });
const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await ctx.newPage();

page.on('console', msg => console.log('CONSOLE:', msg.type(), msg.text()));
page.on('pageerror', err => console.log('PAGE_ERROR:', err.message));

console.log('Navigating to authorize...');
await page.goto(authorizeUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
console.log('After goto, URL:', page.url());

// Wait a bit for JS to load
await page.waitForTimeout(3000);

// Check page state
const dataAttrs = await page.evaluate(() => {
  const el = document.querySelector('[data-auth-method]');
  if (!el) return { found: false };
  return {
    found: true,
    authMethod: el.dataset.authMethod,
    redirectUrl: el.dataset.redirectUrl,
    alpineReady: typeof Alpine !== 'undefined',
  };
});
console.log('Page data:', JSON.stringify(dataAttrs));

// Check for JS errors in the page
const errors = await page.evaluate(() => {
  return window.__jsErrors || [];
});

for (let i = 0; i < 10; i++) {
  await page.waitForTimeout(2000);
  console.log(`  t=${(i + 1) * 2}s URL:`, page.url());
  if (page.url().includes('credentials') || page.url().includes('callback')) break;
}

const html = await page.content();
console.log('Final body (500 chars):', html.slice(0, 500));

await browser.close();
