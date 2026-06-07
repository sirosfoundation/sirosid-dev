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

  // --- Authorization endpoint (auto-approve) ---
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
      createdAt: Date.now(),
    });

    // Auto-approve: redirect immediately
    const redirectUrl = new URL(q.redirect_uri);
    redirectUrl.searchParams.set('code', code);
    if (q.state) redirectUrl.searchParams.set('state', q.state);

    console.log(`[mini-oidc] auto-approve → ${redirectUrl.toString()}`);
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
