/**
 * Mock Issuer Service for E2E Testing - Full OID4VCI Flow
 *
 * Simulates a complete OpenID4VCI credential issuer with:
 * - Metadata endpoints (.well-known/*)
 * - PAR (Pushed Authorization Request) endpoint
 * - Authorization endpoint
 * - Token endpoint (authorization_code and pre-authorized_code grants)
 * - Credential endpoint (issues SD-JWT VCs)
 * - /offer endpoint for generating credential offers
 * - /mdoc_iacas endpoint for IACA certificates
 *
 * Usage:
 *   node index.mjs
 *
 * Environment variables:
 *   PORT - Server port (default: 9000)
 *   ISSUER_ID - Issuer identifier URL (default: http://localhost:9000)
 *   WALLET_URL - Wallet frontend URL (default: http://localhost:3000)
 *   INCLUDE_IACA - Whether to include IACA certificates (default: true)
 */

import http from 'http';
import crypto from 'crypto';

const PORT = parseInt(process.env.PORT || '9000', 10);
const ISSUER_ID = process.env.ISSUER_ID || `http://localhost:${PORT}`;
const WALLET_URL = process.env.WALLET_URL || 'http://localhost:3000';
const INCLUDE_IACA = process.env.INCLUDE_IACA !== 'false';

// In-memory storage for authorization requests and tokens
const authorizationRequests = new Map();
const accessTokens = new Map();
const preAuthorizedCodes = new Map();

// Generate random string
function generateRandomString(length = 32) {
  return crypto.randomBytes(length).toString('base64url').slice(0, length);
}

// Base64URL encode
function base64url(data) {
  if (typeof data === 'string') {
    return Buffer.from(data).toString('base64url');
  }
  return Buffer.from(JSON.stringify(data)).toString('base64url');
}

// Test IACA certificate (self-signed for testing)
const TEST_IACA_CERTIFICATE = `-----BEGIN CERTIFICATE-----
MIIBkTCB+wIJALKvPP8cdtHpMA0GCSqGSIb3DQEBCwUAMBExDzANBgNVBAMMBlRl
c3RDQTAeFw0yNDAxMDEwMDAwMDBaFw0yNTAxMDEwMDAwMDBaMBExDzANBgNVBAMM
BlRlc3RDQTBcMA0GCSqGSIb3DQEBAQUAA0sAMEgCQQC7EL5b8/yKtM8Z7KvJKP7z
4mS0mDJW8a7hAQZ6jWxK2M8L3uKP5qL8mQxK2M8L3uKP5qL8mQxK2M8L3uKP5qL8
AgMBAAGjUDBOMB0GA1UdDgQWBBTFakeTestCertificateMB8GA1UdIwQYMBaAFBTF
akeTestCertificateMA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZIhvcNAQELBQADQQBt
estSignatureForTestPurposesOnly
-----END CERTIFICATE-----`;

// Credential issuer metadata
const getCredentialIssuerMetadata = () => ({
  credential_issuer: ISSUER_ID,
  authorization_servers: [ISSUER_ID],
  credential_endpoint: `${ISSUER_ID}/credential`,
  batch_credential_endpoint: `${ISSUER_ID}/batch_credential`,
  deferred_credential_endpoint: `${ISSUER_ID}/deferred_credential`,
  display: [
    {
      name: 'E2E Test Issuer',
      locale: 'en-US',
      logo: {
        uri: `${ISSUER_ID}/logo.png`,
        alt_text: 'Test Issuer Logo',
      },
    },
  ],
  credential_configurations_supported: {
    // SD-JWT identity credential for testing
    'identity_credential': {
      format: 'vc+sd-jwt',
      vct: 'https://example.com/identity_credential',
      scope: 'identity_credential',
      cryptographic_binding_methods_supported: ['jwk'],
      credential_signing_alg_values_supported: ['ES256'],
      proof_types_supported: {
        jwt: { proof_signing_alg_values_supported: ['ES256'] },
      },
      // credential_metadata wrapper for display/claims per OID4VCI draft 15 / wallet-common schema
      credential_metadata: {
        claims: [
          { path: ['given_name'], mandatory: true, display: [{ name: 'Given Name', locale: 'en-US' }] },
          { path: ['family_name'], mandatory: true, display: [{ name: 'Family Name', locale: 'en-US' }] },
          { path: ['birth_date'], mandatory: false, display: [{ name: 'Birth Date', locale: 'en-US' }] },
          { path: ['email'], mandatory: false, display: [{ name: 'Email', locale: 'en-US' }] },
        ],
        display: [
          {
            name: 'Identity Credential',
            locale: 'en-US',
            background_color: '#1a5f2a',
            text_color: '#FFFFFF',
          },
        ],
      },
    },
    // mDL-style mdoc credential
    'eu.europa.ec.eudi.pid.1': {
      format: 'mso_mdoc',
      doctype: 'eu.europa.ec.eudi.pid.1',
      scope: 'eu.europa.ec.eudi.pid.1',
      cryptographic_binding_methods_supported: ['cose_key'],
      // mso_mdoc uses COSE algorithm numbers, not string names
      credential_signing_alg_values_supported: [-7], // -7 = ES256 in COSE
      proof_types_supported: {
        jwt: { proof_signing_alg_values_supported: ['ES256'] },
      },
      credential_metadata: {
        display: [
          {
            name: 'EU Digital Identity',
            locale: 'en-US',
            background_color: '#12107c',
            text_color: '#FFFFFF',
          },
        ],
      },
    },
    'org.iso.18013.5.1.mDL': {
      format: 'mso_mdoc',
      doctype: 'org.iso.18013.5.1.mDL',
      scope: 'org.iso.18013.5.1.mDL',
      cryptographic_binding_methods_supported: ['cose_key'],
      credential_signing_alg_values_supported: [-7], // -7 = ES256 in COSE
      credential_metadata: {
        display: [{ name: "Mobile Driver's License", locale: 'en-US' }],
      },
    },
  },
  ...(INCLUDE_IACA ? { mdoc_iacas_uri: `${ISSUER_ID}/mdoc_iacas` } : {}),
});

// OAuth authorization server metadata
const getAuthorizationServerMetadata = () => ({
  issuer: ISSUER_ID,
  authorization_endpoint: `${ISSUER_ID}/authorize`,
  token_endpoint: `${ISSUER_ID}/token`,
  pushed_authorization_request_endpoint: `${ISSUER_ID}/par`,
  require_pushed_authorization_requests: false,
  response_types_supported: ['code'],
  response_modes_supported: ['query'],
  grant_types_supported: ['authorization_code', 'urn:ietf:params:oauth:grant-type:pre-authorized_code'],
  code_challenge_methods_supported: ['S256'],
  token_endpoint_auth_methods_supported: ['none'],
  scopes_supported: ['identity_credential', 'eu.europa.ec.eudi.pid.1', 'org.iso.18013.5.1.mDL'],
  dpop_signing_alg_values_supported: ['ES256'],
});

// IACA certificates response - format must match wallet's MdocIacasResponseSchema
// The schema expects: { iacas: [{ certificate: string }] }
const iacaCertificates = {
  iacas: [{ certificate: TEST_IACA_CERTIFICATE }],
};

// Generate a real EC P-256 keypair for signing and JWKS
const MOCK_KID = 'issuer-signing-key-1';
const { privateKey: signingPrivateKey, publicKey: signingPublicKey } = crypto.generateKeyPairSync('ec', {
  namedCurve: 'P-256',
});

// Export public key as JWK for JWKS endpoint
function getPublicKeyJWK() {
  const jwk = signingPublicKey.export({ format: 'jwk' });
  return {
    ...jwk,
    kid: MOCK_KID,
    use: 'sig',
    alg: 'ES256',
  };
}

// Sign a JWT with the issuer's private key (real ES256 signature)
function signJWT(header, payload) {
  const headerWithJWK = {
    ...header,
    jwk: getPublicKeyJWK(),
  };

  const headerBase64 = Buffer.from(JSON.stringify(headerWithJWK)).toString('base64url');
  const payloadBase64 = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const signingInput = `${headerBase64}.${payloadBase64}`;

  const signature = crypto.sign('SHA256', Buffer.from(signingInput), signingPrivateKey);
  const signatureBase64 = signature.toString('base64url');

  return `${headerBase64}.${payloadBase64}.${signatureBase64}`;
}

const mockJwks = {
  keys: [getPublicKeyJWK()],
};

// Create a mock SD-JWT credential (simplified - real one would need proper signing)
function createMockSDJWT(claims, holderBinding) {
  const now = Math.floor(Date.now() / 1000);
  
  // JWT header
  const header = {
    alg: 'ES256',
    typ: 'vc+sd-jwt',
    kid: `${ISSUER_ID}#${MOCK_KID}`,
  };

  // Create selective disclosure for each claim
  const disclosures = [];
  const sdDigests = [];
  
  for (const [key, value] of Object.entries(claims)) {
    const salt = generateRandomString(16);
    const disclosureArray = [salt, key, value];
    const disclosureJson = JSON.stringify(disclosureArray);
    const disclosureB64 = base64url(disclosureJson);
    disclosures.push(disclosureB64);
    
    // SHA-256 hash of the disclosure
    const hash = crypto.createHash('sha256').update(disclosureB64).digest('base64url');
    sdDigests.push(hash);
  }

  // JWT payload (without the actual claim values, only _sd array)
  const payload = {
    iss: ISSUER_ID,
    sub: holderBinding?.jwk ? crypto.createHash('sha256').update(JSON.stringify(holderBinding.jwk)).digest('hex').slice(0, 16) : generateRandomString(16),
    iat: now,
    exp: now + 365 * 24 * 60 * 60, // Valid for 1 year
    vct: 'https://example.com/identity_credential',
    _sd: sdDigests,
    _sd_alg: 'sha-256',
    ...(holderBinding?.jwk ? { cnf: { jwk: holderBinding.jwk } } : {}),
  };

  // Create real signed JWT (header.payload.signature format)
  const headerB64 = base64url(header);
  const payloadB64 = base64url(payload);
  const signingInput = `${headerB64}.${payloadB64}`;
  const signature = crypto.sign('SHA256', Buffer.from(signingInput), signingPrivateKey);
  const signatureB64 = signature.toString('base64url');
  
  const jwt = `${headerB64}.${payloadB64}.${signatureB64}`;
  
  // SD-JWT format: jwt~disclosure1~disclosure2~...
  return `${jwt}~${disclosures.join('~')}~`;
}

// Parse request body
async function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

// Parse URL-encoded form data
function parseFormData(body) {
  const params = new URLSearchParams(body);
  const result = {};
  params.forEach((value, key) => {
    result[key] = value;
  });
  return result;
}

// Handle PAR (Pushed Authorization Request)
async function handlePAR(req, res) {
  const body = await parseBody(req);
  const params = parseFormData(body);
  
  const requestUri = `urn:ietf:params:oauth:request_uri:${generateRandomString(32)}`;
  
  authorizationRequests.set(requestUri, {
    ...params,
    created: Date.now(),
    expires: Date.now() + 60000, // 60 seconds
  });

  console.log('Created PAR:', requestUri, 'for scope:', params.scope);

  res.writeHead(201, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    request_uri: requestUri,
    expires_in: 60,
  }));
}

// Handle Authorization endpoint
function handleAuthorize(req, res, query) {
  const requestUri = query.request_uri;
  const clientId = query.client_id;
  
  let authRequest;
  if (requestUri) {
    authRequest = authorizationRequests.get(requestUri);
  } else {
    // Direct authorization request
    authRequest = query;
  }
  
  if (!authRequest) {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'invalid_request', error_description: 'Request not found' }));
    return;
  }

  // For testing, auto-approve and generate code immediately
  const code = generateRandomString(32);
  const state = authRequest.state || query.state;
  const redirectUri = authRequest.redirect_uri || query.redirect_uri || WALLET_URL;
  
  // Store code for token exchange
  authorizationRequests.set(code, {
    ...authRequest,
    code,
    created: Date.now(),
    type: 'authorization_code',
  });

  console.log('Generated auth code:', code, 'for redirect:', redirectUri);

  // Redirect back to wallet with code
  const redirectUrl = new URL(redirectUri);
  redirectUrl.searchParams.set('code', code);
  if (state) redirectUrl.searchParams.set('state', state);
  
  res.writeHead(302, { 'Location': redirectUrl.toString() });
  res.end();
}

// Handle Token endpoint
async function handleToken(req, res) {
  const body = await parseBody(req);
  const params = parseFormData(body);
  
  const grantType = params.grant_type;
  let accessToken, cNonce;
  
  if (grantType === 'authorization_code') {
    const code = params.code;
    const authRequest = authorizationRequests.get(code);
    
    if (!authRequest) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'invalid_grant', error_description: 'Invalid authorization code' }));
      return;
    }
    
    accessToken = generateRandomString(32);
    cNonce = generateRandomString(16);
    
    accessTokens.set(accessToken, {
      ...authRequest,
      accessToken,
      cNonce,
      created: Date.now(),
    });
    
    // Cleanup used code
    authorizationRequests.delete(code);
    
  } else if (grantType === 'urn:ietf:params:oauth:grant-type:pre-authorized_code') {
    const preAuthCode = params['pre-authorized_code'];
    const preAuth = preAuthorizedCodes.get(preAuthCode);
    
    if (!preAuth) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'invalid_grant', error_description: 'Invalid pre-authorized code' }));
      return;
    }
    
    accessToken = generateRandomString(32);
    cNonce = generateRandomString(16);
    
    accessTokens.set(accessToken, {
      ...preAuth,
      accessToken,
      cNonce,
      created: Date.now(),
    });
    
    // Cleanup used pre-auth code
    preAuthorizedCodes.delete(preAuthCode);
    
  } else {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'unsupported_grant_type' }));
    return;
  }

  console.log('Issued access token:', accessToken);

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    access_token: accessToken,
    token_type: 'Bearer',
    expires_in: 3600,
    c_nonce: cNonce,
    c_nonce_expires_in: 300,
  }));
}

// Handle Credential endpoint
async function handleCredential(req, res) {
  console.log('handleCredential called');
  
  const authHeader = req.headers['authorization'];
  // Support both Bearer and DPoP token schemes
  let accessToken;
  if (authHeader?.startsWith('Bearer ')) {
    accessToken = authHeader.slice(7);  // Remove "Bearer " prefix
  } else if (authHeader?.startsWith('DPoP ')) {
    accessToken = authHeader.slice(5);  // Remove "DPoP " prefix
    // Note: In production, we'd verify the DPoP proof in the request header
    // For testing, we just extract the token
  } else {
    console.log('Missing or invalid Authorization header:', authHeader);
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'invalid_token' }));
    return;
  }
  
  const tokenData = accessTokens.get(accessToken);
  console.log('Access token:', accessToken, 'Found:', !!tokenData);
  
  if (!tokenData) {
    console.log('Token not found. Known tokens:', [...accessTokens.keys()]);
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'invalid_token' }));
    return;
  }
  
  const body = await parseBody(req);
  let credentialRequest;
  try {
    credentialRequest = JSON.parse(body);
  } catch (e) {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'invalid_request', error_description: 'Invalid JSON' }));
    return;
  }

  console.log('Credential request:', credentialRequest);
  
  // Extract holder binding from proof if provided
  let holderBinding = null;
  if (credentialRequest.proof?.jwt) {
    try {
      const proofParts = credentialRequest.proof.jwt.split('.');
      const proofHeader = JSON.parse(Buffer.from(proofParts[0], 'base64url').toString());
      if (proofHeader.jwk) {
        holderBinding = { jwk: proofHeader.jwk };
      }
    } catch (e) {
      console.log('Could not parse proof JWT:', e.message);
    }
  }

  // Generate mock credential claims
  const claims = {
    given_name: 'Test',
    family_name: 'User',
    birth_date: '1990-01-15',
    email: 'test.user@example.com',
    issuing_country: 'SE',
  };

  // Create SD-JWT credential
  const credential = createMockSDJWT(claims, holderBinding);
  const newCNonce = generateRandomString(16);

  // Update token with new nonce
  tokenData.cNonce = newCNonce;

  console.log('Issued credential for token:', accessToken);

  // Return credential in the format expected by wallet:
  // { credentials: [{ credential: "..." }], c_nonce: "...", c_nonce_expires_in: 300 }
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    credentials: [{ credential }],
    c_nonce: newCNonce,
    c_nonce_expires_in: 300,
  }));
}

// Handle /offer endpoint - creates a credential offer with authorization_code grant
// The wallet frontend only supports authorization_code grant, not pre-authorized_code
function handleOffer(req, res, query) {
  const preAuthCode = generateRandomString(32);
  const issuerState = generateRandomString(32);
  const credentialConfigId = query.credential || 'identity_credential';
  
  preAuthorizedCodes.set(preAuthCode, {
    credentialConfigurationId: credentialConfigId,
    created: Date.now(),
    scope: credentialConfigId,
  });

  // Store issuer_state for authorization code flow
  authorizationRequests.set(`issuer_state:${issuerState}`, {
    credentialConfigurationId: credentialConfigId,
    created: Date.now(),
  });

  const credentialOffer = {
    credential_issuer: ISSUER_ID,
    credential_configuration_ids: [credentialConfigId],
    grants: {
      // authorization_code grant is required - wallet only supports this
      authorization_code: {
        issuer_state: issuerState,
      },
      // Also include pre-authorized_code for wallets that support it
      'urn:ietf:params:oauth:grant-type:pre-authorized_code': {
        'pre-authorized_code': preAuthCode,
      },
    },
  };

  // Create credential offer URI
  const offerJson = JSON.stringify(credentialOffer);
  const credentialOfferUri = `${ISSUER_ID}/offers/${preAuthCode}`;
  
  // Store the offer for retrieval
  authorizationRequests.set(`offer:${preAuthCode}`, credentialOffer);
  
  // Create wallet URL
  const walletUrl = new URL(WALLET_URL);
  walletUrl.searchParams.set('credential_offer_uri', credentialOfferUri);

  console.log('Created credential offer with pre-auth code:', preAuthCode);

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    credential_offer: credentialOffer,
    credential_offer_uri: credentialOfferUri,
    wallet_url: walletUrl.toString(),
    pre_authorized_code: preAuthCode,
  }));
}

// Handle retrieval of stored credential offers
function handleOfferRetrieval(preAuthCode, res) {
  const offer = authorizationRequests.get(`offer:${preAuthCode}`);
  
  if (!offer) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not_found' }));
    return;
  }

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(offer));
}

async function handleRequest(req, res) {
  const urlParts = req.url.split('?');
  const pathname = urlParts[0];
  const query = Object.fromEntries(new URLSearchParams(urlParts[1] || ''));

  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, DPoP');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  console.log(`[${new Date().toISOString()}] ${req.method} ${pathname}`);

  try {
    // Static routes
    switch (pathname) {
      case '/.well-known/openid-credential-issuer':
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(getCredentialIssuerMetadata(), null, 2));
        return;

      case '/.well-known/oauth-authorization-server':
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(getAuthorizationServerMetadata(), null, 2));
        return;

      case '/mdoc_iacas':
        if (INCLUDE_IACA) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(iacaCertificates, null, 2));
        } else {
          res.writeHead(404, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'IACA certificates not available' }));
        }
        return;

      case '/jwks':
      case '/.well-known/jwks.json':
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(mockJwks, null, 2));
        return;

      case '/par':
        if (req.method === 'POST') {
          await handlePAR(req, res);
        } else {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'method_not_allowed' }));
        }
        return;

      case '/authorize':
        handleAuthorize(req, res, query);
        return;

      case '/token':
        if (req.method === 'POST') {
          await handleToken(req, res);
        } else {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'method_not_allowed' }));
        }
        return;

      case '/credential':
        if (req.method === 'POST') {
          await handleCredential(req, res);
        } else {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'method_not_allowed' }));
        }
        return;

      case '/offer':
        handleOffer(req, res, query);
        return;

      case '/health':
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', issuer: ISSUER_ID }));
        return;

      case '/':
        // Demo page
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(`
<!DOCTYPE html>
<html>
<head><title>E2E Test Issuer</title></head>
<body>
  <h1>E2E Test Issuer</h1>
  <p>Issuer ID: ${ISSUER_ID}</p>
  <p><a href="/offer">Get Credential Offer</a></p>
  <p><a href="/.well-known/openid-credential-issuer">Credential Issuer Metadata</a></p>
  <p><a href="/.well-known/oauth-authorization-server">Authorization Server Metadata</a></p>
</body>
</html>
        `);
        return;
    }

    // Dynamic routes
    if (pathname.startsWith('/offers/')) {
      const preAuthCode = pathname.split('/')[2];
      handleOfferRetrieval(preAuthCode, res);
      return;
    }

    // Not found
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not_found', path: pathname }));
  } catch (error) {
    console.error('Request handler error:', error);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'internal_error', message: error.message }));
  }
}

const server = http.createServer(handleRequest);

server.listen(PORT, () => {
  console.log(`Full Flow Mock Issuer running on ${ISSUER_ID}`);
  console.log(`  /.well-known/openid-credential-issuer - Credential issuer metadata`);
  console.log(`  /.well-known/oauth-authorization-server - Authorization server metadata`);
  console.log(`  /par - Pushed Authorization Request (POST)`);
  console.log(`  /authorize - Authorization endpoint`);
  console.log(`  /token - Token endpoint (POST)`);
  console.log(`  /credential - Credential endpoint (POST)`);
  console.log(`  /offer - Get pre-authorized credential offer`);
  console.log(`  /jwks - Issuer JWKS`);
  if (INCLUDE_IACA) console.log(`  /mdoc_iacas - IACA certificates`);
  console.log(`  /health - Health check endpoint`);
});

// Handle graceful shutdown
process.on('SIGTERM', () => {
  console.log('Shutting down mock issuer...');
  server.close(() => process.exit(0));
});

process.on('SIGINT', () => {
  console.log('Shutting down mock issuer...');
  server.close(() => process.exit(0));
});
