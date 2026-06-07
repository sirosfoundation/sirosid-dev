// Debug Alpine.js loading on the consent page
import { chromium } from 'playwright';

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

// Create a PAR first
const params = new URLSearchParams({
  response_type: 'code',
  client_id: 'e2e-test-client',
  redirect_uri: 'http://localhost:3000/cb',
  scope: 'pid_1_8',
  state: 'teststate123',
  code_challenge: 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk',
  code_challenge_method: 'S256',
});

const parResp = await fetch('https://vc-proxy:8443/op/par', {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: params.toString(),
});
const parBody = await parResp.json();
console.log('PAR:', parResp.status, JSON.stringify(parBody));

const authorizeUrl = `https://vc-proxy:8443/authorize?client_id=e2e-test-client&request_uri=${encodeURIComponent(parBody.request_uri)}`;

const browser = await chromium.launch({ args: ['--no-sandbox', '--ignore-certificate-errors'] });
const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await ctx.newPage();

// Log ALL network
page.on('requestfailed', req => console.log('NET_FAIL:', req.url(), req.failure()?.errorText));
page.on('response', r => {
  const u = r.url();
  if (u.includes('static') || u.includes('consent') || u.includes('alpine') || u.includes('valibot') || r.status() >= 400)
    console.log('NET:', r.status(), u.slice(0, 150));
});

page.on('console', msg => console.log('JS:', msg.type(), msg.text()));
page.on('pageerror', err => console.log('JS_ERROR:', err.message));

await page.goto(authorizeUrl, { waitUntil: 'load', timeout: 30000 });
console.log('URL after load:', page.url());

await page.waitForTimeout(8000);
console.log('URL after 8s:', page.url());

// Inspect Alpine
const state = await page.evaluate(() => {
  const section = document.querySelector('section[x-data]');
  return {
    hasAlpine: typeof Alpine !== 'undefined',
    sectionFound: !!section,
    authMethod: section?.dataset.authMethod,
    redirectUrl: section?.dataset.redirectUrl?.slice(0, 100),
    alpineData: section?.__x?.$data ? Object.keys(section.__x.$data) : null,
    bodyText: document.body.innerText?.slice(0, 300),
  };
});
console.log('State:', JSON.stringify(state, null, 2));

await browser.close();
