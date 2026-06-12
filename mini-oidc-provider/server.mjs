// Mini OIDC Provider — auto-approve, no login form.
// Implements: discovery, authorization (auto-redirect), token, userinfo, jwks.
import { createServer } from 'node:http';
import { parse as parseURL } from 'node:url';
import { randomBytes, createHash } from 'node:crypto';
import * as jose from 'jose';

const PORT = parseInt(process.env.PORT || '9005', 10);
const ISSUER = process.env.ISSUER || `http://localhost:${PORT}`;

// --- Key pair (generated at startup) ---
let keyPair;
let jwks;
const KID = 'mini-oidc-1';

async function initKeys() {
  keyPair = await jose.generateKeyPair('ES256', { extractable: true });
  const pubJWK = await jose.exportJWK(keyPair.publicKey);
  pubJWK.kid = KID;
  pubJWK.use = 'sig';
  pubJWK.alg = 'ES256';
  jwks = { keys: [pubJWK] };
}

// --- In-memory authorization codes ---
const codes = new Map(); // code -> { clientId, redirectUri, nonce, codeChallenge, codeChallengeMethod, scope }

// --- Test user claims ---
const TEST_USER = {
  sub: 'test-user-001',
  given_name: 'Alice',
  family_name: 'Wonderland',
  name: 'Alice Wonderland',
  email: 'alice@example.com',
  birthdate: '1990-01-15',
  birth_date: '1990-01-15',
  place_of_birth: { locality: 'Stockholm' },
  nationalities: ['SE'],
  issuing_authority: 'Swedish Tax Agency',
  issuing_country: 'SE',
  date_of_expiry: '2030-12-31',
};

function json(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(data),
    'Cache-Control': 'no-store',
  });
  res.end(data);
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString();
      if (req.headers['content-type']?.includes('application/json')) {
        try { resolve(JSON.parse(raw)); } catch { resolve({}); }
      } else {
        resolve(Object.fromEntries(new URLSearchParams(raw)));
      }
    });
    req.on('error', reject);
  });
}

function verifyCodeChallenge(verifier, challenge, method) {
  if (method === 'S256') {
    const hash = createHash('sha256').update(verifier).digest();
    const expected = hash.toString('base64url');
    return expected === challenge;
  }
  return verifier === challenge; // plain
}

async function handler(req, res) {
  const url = parseURL(req.url, true);
  const path = url.pathname;

  // --- Discovery ---
  if (path === '/.well-known/openid-configuration' && req.method === 'GET') {
    return json(res, 200, {
      issuer: ISSUER,
      authorization_endpoint: `${ISSUER}/authorize`,
      token_endpoint: `${ISSUER}/token`,
      userinfo_endpoint: `${ISSUER}/userinfo`,
      jwks_uri: `${ISSUER}/jwks`,
      registration_endpoint: `${ISSUER}/register`,
      scopes_supported: ['openid', 'profile', 'email'],
      response_types_supported: ['code'],
      grant_types_supported: ['authorization_code'],
      subject_types_supported: ['public'],
      id_token_signing_alg_values_supported: ['ES256'],
      token_endpoint_auth_methods_supported: ['client_secret_post', 'client_secret_basic', 'none'],
      code_challenge_methods_supported: ['S256', 'plain'],
    });
  }

  // --- JWKS ---
  if (path === '/jwks' && req.method === 'GET') {
    return json(res, 200, jwks);
  }

  // --- Dynamic client registration (RFC 7591) ---
  if (path === '/register' && req.method === 'POST') {
    const body = await parseBody(req);
    const clientId = `dyn-${randomBytes(8).toString('hex')}`;
    const clientSecret = randomBytes(32).toString('hex');
    return json(res, 201, {
      client_id: clientId,
      client_secret: clientSecret,
      client_id_issued_at: Math.floor(Date.now() / 1000),
      client_secret_expires_at: 0,
      redirect_uris: body.redirect_uris || [],
      grant_types: ['authorization_code'],
      response_types: ['code'],
      token_endpoint_auth_method: body.token_endpoint_auth_method || 'client_secret_basic',
    });
  }

  // --- Authorization endpoint (consent page) ---
  if (path === '/authorize' && req.method === 'GET') {
    const q = url.query;
    const code = randomBytes(32).toString('hex');

    codes.set(code, {
      clientId: q.client_id,
      redirectUri: q.redirect_uri,
      nonce: q.nonce,
      codeChallenge: q.code_challenge,
      codeChallengeMethod: q.code_challenge_method || 'plain',
      scope: q.scope || 'openid',
      state: q.state || '',
      createdAt: Date.now(),
    });

    // Show a consent page with user info and an approve button
    const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mini OIDC — Sign In</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #f0f2f5; display: flex; justify-content: center; align-items: flex-start;
           min-height: 100vh; min-height: 100dvh; padding: 8px; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.1);
            padding: 16px; max-width: 400px; width: 100%; text-align: center; margin-top: 8px; }
    .avatar { width: 48px; height: 48px; border-radius: 50%; background: #4f46e5;
              color: #fff; font-size: 22px; line-height: 48px; margin: 0 auto 8px; }
    h1 { font-size: 18px; color: #111; margin-bottom: 2px; }
    .sub { color: #666; font-size: 13px; margin-bottom: 12px; }
    .info { text-align: left; background: #f8f9fa; border-radius: 8px; padding: 8px 12px;
            margin-bottom: 16px; font-size: 13px; color: #333; }
    .info dt { font-weight: 600; margin-top: 6px; }
    .info dt:first-child { margin-top: 0; }
    .info dd { margin-left: 0; color: #555; }
    .scope { display: inline-block; background: #e0e7ff; color: #3730a3; border-radius: 4px;
             padding: 2px 8px; font-size: 12px; margin: 2px; }
    form { display: flex; gap: 12px; justify-content: center; }
    button { padding: 10px 28px; border: none; border-radius: 8px; font-size: 15px;
             font-weight: 600; cursor: pointer; transition: background .15s; }
    .approve { background: #4f46e5; color: #fff; }
    .approve:hover { background: #4338ca; }
    .deny { background: #e5e7eb; color: #374151; }
    .deny:hover { background: #d1d5db; }
    .client { color: #4f46e5; font-weight: 600; }
  </style>
</head>
<body>
  <div class="card">
    <div class="avatar">A</div>
    <h1>${TEST_USER.name}</h1>
    <p class="sub">${TEST_USER.email}</p>
    <div class="info">
      <dl>
        <dt>Application</dt>
        <dd class="client">${q.client_id || 'Unknown'}</dd>
        <dt>Requested access</dt>
        <dd>${(q.scope || 'openid').split(' ').map(s => '<span class="scope">' + s + '</span>').join(' ')}</dd>
      </dl>
    </div>
    <form method="POST" action="/authorize/consent">
      <input type="hidden" name="code" value="${code}">
      <button type="submit" class="approve">Approve</button>
    </form>
  </div>
</body>
</html>`;

    console.log(`[mini-oidc] showing consent page for client=${q.client_id}`);
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': Buffer.byteLength(html),
      'Cache-Control': 'no-store',
    });
    return res.end(html);
  }

  // --- Authorization consent callback (approve button) ---
  if (path === '/authorize/consent' && req.method === 'POST') {
    const body = await parseBody(req);
    const authCode = codes.get(body.code);
    if (!authCode) {
      res.writeHead(400, { 'Content-Type': 'text/plain' });
      return res.end('Invalid or expired authorization code');
    }

    const redirectUrl = new URL(authCode.redirectUri);
    redirectUrl.searchParams.set('code', body.code);
    if (authCode.state) redirectUrl.searchParams.set('state', authCode.state);

    console.log(`[mini-oidc] user approved → ${redirectUrl.toString()}`);
    res.writeHead(302, { Location: redirectUrl.toString() });
    return res.end();
  }

  // --- Token endpoint ---
  if (path === '/token' && req.method === 'POST') {
    const body = await parseBody(req);
    const { code, code_verifier, redirect_uri } = body;

    const authCode = codes.get(code);
    if (!authCode) {
      return json(res, 400, { error: 'invalid_grant', error_description: 'unknown code' });
    }
    codes.delete(code);

    // Verify code expiry (5 minutes)
    if (Date.now() - authCode.createdAt > 5 * 60 * 1000) {
      return json(res, 400, { error: 'invalid_grant', error_description: 'code expired' });
    }

    // Verify PKCE
    if (authCode.codeChallenge && code_verifier) {
      if (!verifyCodeChallenge(code_verifier, authCode.codeChallenge, authCode.codeChallengeMethod)) {
        return json(res, 400, { error: 'invalid_grant', error_description: 'PKCE verification failed' });
      }
    }

    // Build ID token
    const now = Math.floor(Date.now() / 1000);
    const idToken = await new jose.SignJWT({
      ...TEST_USER,
      nonce: authCode.nonce,
    })
      .setProtectedHeader({ alg: 'ES256', kid: KID })
      .setIssuer(ISSUER)
      .setAudience(authCode.clientId)
      .setSubject(TEST_USER.sub)
      .setIssuedAt(now)
      .setExpirationTime(now + 3600)
      .sign(keyPair.privateKey);

    const accessToken = randomBytes(32).toString('hex');

    return json(res, 200, {
      access_token: accessToken,
      token_type: 'Bearer',
      expires_in: 3600,
      id_token: idToken,
      scope: authCode.scope,
    });
  }

  // --- UserInfo endpoint ---
  if (path === '/userinfo' && req.method === 'GET') {
    // In a real provider we'd validate the access token.
    // For testing, always return the test user.
    return json(res, 200, TEST_USER);
  }

  // --- Health ---
  if (path === '/health') {
    return json(res, 200, { status: 'ok' });
  }

  res.writeHead(404);
  res.end('Not Found');
}

await initKeys();

const server = createServer(handler);
server.listen(PORT, () => {
  console.log(`[mini-oidc] listening on :${PORT}, issuer=${ISSUER}`);
});
