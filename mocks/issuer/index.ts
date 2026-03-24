/**
 * Mock Issuer Service for E2E Testing
 *
 * Full OpenID4VCI credential issuer implementation with:
 * - /.well-known/openid-credential-issuer endpoint
 * - /.well-known/oauth-authorization-server endpoint
 * - /offer endpoint to generate credential offers (authorization code flow)
 * - /par endpoint for pushed authorization requests
 * - /authorize endpoint (auto-approves and redirects)
 * - /token endpoint for code exchange
 * - /credential endpoint for credential issuance
 * - /mdoc_iacas endpoint for IACA certificates
 *
 * Usage:
 *   npx ts-node mocks/issuer/index.ts
 *
 * Environment variables:
 *   PORT - Server port (default: 9000)
 *   ISSUER_ID - Issuer identifier URL (default: http://localhost:9000)
 *   INCLUDE_IACA - Whether to include IACA certificates (default: true)
 *   DEFAULT_REDIRECT_URI - Default callback URL (default: http://localhost:3000/)
 */

import * as http from 'http';
import * as urlLib from 'url';
import * as crypto from 'crypto';

const PORT = parseInt(process.env.PORT || '9000', 10);
const ISSUER_ID = process.env.ISSUER_ID || `http://localhost:${PORT}`;
const INCLUDE_IACA = process.env.INCLUDE_IACA !== 'false';
const DEFAULT_REDIRECT_URI = process.env.DEFAULT_REDIRECT_URI || 'http://localhost:3000/';

// Generate signing keypair at startup (ES256 = P-256 curve)
const { privateKey: signingPrivateKey, publicKey: signingPublicKey } = crypto.generateKeyPairSync('ec', {
  namedCurve: 'P-256',
});

// Export public key as JWK for JWKS endpoint
function getPublicKeyJWK(): Record<string, any> {
  const jwk = signingPublicKey.export({ format: 'jwk' });
  return {
    ...jwk,
    kid: 'issuer-signing-key-1',
    use: 'sig',
    alg: 'ES256',
  };
}

// Sign a JWT with the issuer's private key
function signJWT(header: Record<string, any>, payload: Record<string, any>): string {
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

// In-memory stores for OAuth state
const authorizationCodes = new Map<string, {
  clientId: string;
  redirectUri: string;
  codeChallenge?: string;
  codeChallengeMethod?: string;
  scope: string;
  createdAt: number;
}>();

const accessTokens = new Map<string, {
  clientId: string;
  scope: string;
  createdAt: number;
  cNonce: string;
}>();

const parRequests = new Map<string, {
  clientId: string;
  redirectUri: string;
  codeChallenge?: string;
  codeChallengeMethod?: string;
  scope: string;
  state?: string;
  createdAt: number;
}>();

// Generate a random string
function randomString(length: number = 32): string {
  return crypto.randomBytes(length).toString('base64url').slice(0, length);
}

// Test IACA certificate (self-signed for testing)
const TEST_IACA_CERTIFICATE = `-----BEGIN CERTIFICATE-----
MIICojCCAimgAwIBAgIUFakeIacaCertificateForTesting123456Aww0GCSqGSIb3DQEBCwUA
MFoxCzAJBgNVBAYTAlVTMRMwEQYDVQQIDApXYXNoaW5ndG9uMRAwDgYDVQQHDAdT
ZWF0dGxlMRAwDgYDVQQKDAdUZXN0IENBMR IwEAYDVQQDDAlUZXN0IFJPT1QwHhcN
MjQwMTAxMDAwMDAwWhcNMjUwMTAxMDAwMDAwWjBaMQswCQYDVQQGEwJVUzETMBEG
A1UECAwKV2FzaGluZ3RvbjEQMA4GA1UEBwwHU2VhdHRsZTEQMA4GA1UECgwHVGVz
dCBDQTESMBAGA1UEAwwJVGVzdCBJQUNBMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A
MIIBCgKCAQEA0VX7fL1r7sP2aDe0ZzCJN5E3UcSPLAT9pM5tWiPW+nLqmJ9nHFrY
dU3w9H+k7rSJk5bXnV1K2R3YJh0PkHqYpMl/zs+9pPyJ3Td6E8EChJjS4KL+D5bQ
w3S8xJk0bFMYwAFJ9Y4bJ8lM6pV4RZ0nH5cJpMqY8H9FjJQH0S2JkK9xX5mLBw+1
TnQ2pXr8pJ2P0yLM9xJfHcR8RpKjK2n3Z4R2dJpQJ8K9xLJ0dJ3V4mJz5H9rJQPm
KDQJQ2R3dM5kpJk4rJ2L4xJ4dM5rJQPmKDQJQ2R3dM5kpJk4rJ2L4xJ4dM5rJQPm
KDQJQ2R3dM5kpJk4rJ2L4xJ4dM5rJQPmKDQJQ2R3dM5kpJk4rwIDAQABo4IBADCC
APwwCQYDVR0TBAIwADALBgNVHQ8EBAMCBaAwHQYDVR0OBBYEFEFakeTestCertifi
cateForTestingMB8GA1UdIwQYMBaAFEFakeTestCertificateForTestingMA0GCSqG
SIb3DQEBCwUAA4IBAQBfakeSignatureDataForTestPurposesOnly123456789
-----END CERTIFICATE-----`;

// Credential issuer metadata
const credentialIssuerMetadata = {
  credential_issuer: ISSUER_ID,
  authorization_servers: [ISSUER_ID],
  credential_endpoint: `${ISSUER_ID}/credential`,
  batch_credential_endpoint: `${ISSUER_ID}/batch_credential`,
  deferred_credential_endpoint: `${ISSUER_ID}/deferred_credential`,
  display: [
    {
      name: 'Test Issuer',
      locale: 'en-US',
      logo: {
        uri: `${ISSUER_ID}/logo.png`,
        alt_text: 'Test Issuer Logo',
      },
    },
  ],
  credential_configurations_supported: {
    identity_credential: {
      format: 'vc+sd-jwt',
      vct: 'https://credentials.example.com/identity_credential',
      scope: 'identity_credential',
      cryptographic_binding_methods_supported: ['jwk'],
      credential_signing_alg_values_supported: ['ES256'],
      proof_types_supported: {
        jwt: {
          proof_signing_alg_values_supported: ['ES256'],
        },
      },
      display: [
        {
          name: 'Identity Credential',
          locale: 'en-US',
          logo: {
            uri: `${ISSUER_ID}/identity-logo.png`,
            alt_text: 'Identity Logo',
          },
          background_color: '#12107c',
          text_color: '#FFFFFF',
        },
      ],
      claims: {
        given_name: { display: [{ name: 'Given Name', locale: 'en-US' }] },
        family_name: { display: [{ name: 'Family Name', locale: 'en-US' }] },
        email: { display: [{ name: 'Email', locale: 'en-US' }] },
        birthdate: { display: [{ name: 'Date of Birth', locale: 'en-US' }] },
      },
    },
    'eu.europa.ec.eudi.pid.1': {
      format: 'mso_mdoc',
      doctype: 'eu.europa.ec.eudi.pid.1',
      scope: 'eu.europa.ec.eudi.pid.1',
      cryptographic_binding_methods_supported: ['cose_key'],
      credential_signing_alg_values_supported: ['ES256'],
      proof_types_supported: {
        jwt: {
          proof_signing_alg_values_supported: ['ES256'],
        },
      },
      display: [
        {
          name: 'EU Digital Identity',
          locale: 'en-US',
          logo: {
            uri: `${ISSUER_ID}/pid-logo.png`,
            alt_text: 'PID Logo',
          },
          background_color: '#12107c',
          text_color: '#FFFFFF',
        },
      ],
    },
    'org.iso.18013.5.1.mDL': {
      format: 'mso_mdoc',
      doctype: 'org.iso.18013.5.1.mDL',
      scope: 'org.iso.18013.5.1.mDL',
      cryptographic_binding_methods_supported: ['cose_key'],
      credential_signing_alg_values_supported: ['ES256'],
      display: [
        {
          name: "Mobile Driver's License",
          locale: 'en-US',
        },
      ],
    },
  },
  // mdoc_iacas_uri points to the IACA certificates endpoint
  ...(INCLUDE_IACA ? { mdoc_iacas_uri: `${ISSUER_ID}/mdoc_iacas` } : {}),
};

// OAuth authorization server metadata
const authorizationServerMetadata = {
  issuer: ISSUER_ID,
  authorization_endpoint: `${ISSUER_ID}/authorize`,
  token_endpoint: `${ISSUER_ID}/token`,
  pushed_authorization_request_endpoint: `${ISSUER_ID}/par`,
  require_pushed_authorization_requests: false,
  response_types_supported: ['code'],
  response_modes_supported: ['query'],
  grant_types_supported: ['authorization_code', 'urn:ietf:params:oauth:grant-type:pre-authorized_code'],
  code_challenge_methods_supported: ['S256', 'plain'],
  token_endpoint_auth_methods_supported: ['none'],
  scopes_supported: ['identity_credential', 'eu.europa.ec.eudi.pid.1', 'org.iso.18013.5.1.mDL'],
  dpop_signing_alg_values_supported: ['ES256'],
};

// IACA certificates response
const iacaCertificates = {
  iacas: [{ certificate: TEST_IACA_CERTIFICATE }],
};

// Parse request body
async function parseBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk: Buffer | string) => body += chunk);
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

// Parse URL-encoded body
function parseUrlEncoded(body: string): Record<string, string> {
  const params = new URLSearchParams(body);
  const result: Record<string, string> = {};
  params.forEach((value, key) => {
    result[key] = value;
  });
  return result;
}

// Generate a mock SD-JWT credential
function generateMockSDJWTCredential(cNonce: string): string {
  // Create a real signed credential
  const header = { alg: 'ES256', typ: 'vc+sd-jwt' };
  const payload = {
    iss: ISSUER_ID,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + 86400 * 365, // 1 year
    vct: 'https://credentials.example.com/identity_credential',
    cnf: { jwk: { kty: 'EC', crv: 'P-256', x: 'mock', y: 'mock' } },
    _sd: ['mockDisclosure1', 'mockDisclosure2'],
    given_name: 'Test',
    family_name: 'User',
    email: 'test@example.com',
    birthdate: '1990-01-01',
  };
  
  // Sign the JWT with the issuer's private key (includes JWK in header)
  return signJWT(header, payload);
}

// Verify PKCE code challenge
function verifyCodeChallenge(codeVerifier: string, codeChallenge: string, method: string = 'S256'): boolean {
  if (method === 'plain') {
    return codeVerifier === codeChallenge;
  }
  // S256
  const hash = crypto.createHash('sha256').update(codeVerifier).digest('base64url');
  return hash === codeChallenge;
}

async function handleRequest(req: http.IncomingMessage, res: http.ServerResponse) {
  const parsedUrl = urlLib.parse(req.url || '/', true);
  const pathname = parsedUrl.pathname || '/';
  const query = parsedUrl.query as Record<string, string>;

  // CORS headers for testing
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, DPoP');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  console.log(`[${new Date().toISOString()}] ${req.method} ${pathname}`);

  switch (pathname) {
    // ===== Well-known endpoints =====
    case '/.well-known/openid-credential-issuer':
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(credentialIssuerMetadata, null, 2));
      break;

    case '/.well-known/jwks.json': {
      // Return the issuer's public signing key as JWKS
      const jwks = {
        keys: [getPublicKeyJWK()],
      };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(jwks, null, 2));
      break;
    }

    case '/.well-known/oauth-authorization-server':
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(authorizationServerMetadata, null, 2));
      break;

    // ===== Credential Offer Generation =====
    case '/offer': {
      // Generate a credential offer for authorization code flow
      const credentialConfigurationId = query.credential || 'identity_credential';
      
      // Create a credential offer
      const offer = {
        credential_issuer: ISSUER_ID,
        credential_configuration_ids: [credentialConfigurationId],
        grants: {
          authorization_code: {
            issuer_state: randomString(16),
          },
        },
      };
      
      // Return the offer as a credential_offer_uri that points back to this server
      const offerEncoded = encodeURIComponent(JSON.stringify(offer));
      const credentialOfferUri = `${ISSUER_ID}/credential-offer?offer=${offerEncoded}`;
      
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        credential_offer_uri: credentialOfferUri,
        credential_offer: offer,
      }));
      break;
    }

    case '/credential-offer': {
      // Return the credential offer object
      const offerParam = query.offer;
      if (offerParam) {
        try {
          const offer = JSON.parse(decodeURIComponent(offerParam));
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(offer, null, 2));
        } catch (e) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'invalid_request', error_description: 'Invalid offer parameter' }));
        }
      } else {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid_request', error_description: 'Missing offer parameter' }));
      }
      break;
    }

    // ===== Pushed Authorization Request (PAR) =====
    case '/par': {
      if (req.method !== 'POST') {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'method_not_allowed' }));
        break;
      }

      const body = await parseBody(req);
      const params = parseUrlEncoded(body);
      
      const requestUri = `urn:ietf:params:oauth:request_uri:${randomString(32)}`;
      parRequests.set(requestUri, {
        clientId: params.client_id || 'wallet',
        redirectUri: params.redirect_uri || DEFAULT_REDIRECT_URI,
        codeChallenge: params.code_challenge,
        codeChallengeMethod: params.code_challenge_method || 'S256',
        scope: params.scope || 'identity_credential',
        state: params.state,
        createdAt: Date.now(),
      });

      console.log(`  PAR created: ${requestUri}`);
      
      res.writeHead(201, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        request_uri: requestUri,
        expires_in: 600,
      }));
      break;
    }

    // ===== Authorization Endpoint =====
    case '/authorize': {
      // Auto-approve and redirect with authorization code
      let clientId = query.client_id || 'wallet';
      let redirectUri = query.redirect_uri || DEFAULT_REDIRECT_URI;
      let codeChallenge = query.code_challenge;
      let codeChallengeMethod = query.code_challenge_method || 'S256';
      let scope = query.scope || 'identity_credential';
      let state = query.state;

      // Check for PAR request_uri
      if (query.request_uri) {
        const parData = parRequests.get(query.request_uri);
        if (parData) {
          clientId = parData.clientId;
          redirectUri = parData.redirectUri;
          codeChallenge = parData.codeChallenge;
          codeChallengeMethod = parData.codeChallengeMethod || 'S256';
          scope = parData.scope;
          state = parData.state;
          parRequests.delete(query.request_uri);
        } else {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'invalid_request', error_description: 'Invalid or expired request_uri' }));
          break;
        }
      }

      // Generate authorization code
      const code = randomString(32);
      authorizationCodes.set(code, {
        clientId,
        redirectUri,
        codeChallenge,
        codeChallengeMethod,
        scope,
        createdAt: Date.now(),
      });

      console.log(`  Auth code generated: ${code.substring(0, 8)}... for scope: ${scope}`);

      // Build redirect URL
      const redirectUrl = new URL(redirectUri);
      redirectUrl.searchParams.set('code', code);
      if (state) {
        redirectUrl.searchParams.set('state', state);
      }
      if (query.issuer_state) {
        redirectUrl.searchParams.set('issuer_state', query.issuer_state);
      }

      console.log(`  Redirecting to: ${redirectUrl.toString()}`);
      
      res.writeHead(302, { Location: redirectUrl.toString() });
      res.end();
      break;
    }

    // ===== Token Endpoint =====
    case '/token': {
      if (req.method !== 'POST') {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'method_not_allowed' }));
        break;
      }

      const body = await parseBody(req);
      const params = parseUrlEncoded(body);

      if (params.grant_type === 'authorization_code') {
        const codeData = authorizationCodes.get(params.code);
        
        if (!codeData) {
          console.log(`  Invalid code: ${params.code?.substring(0, 8)}...`);
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'invalid_grant', error_description: 'Invalid or expired authorization code' }));
          break;
        }

        // Verify PKCE if code challenge was provided
        if (codeData.codeChallenge && params.code_verifier) {
          if (!verifyCodeChallenge(params.code_verifier, codeData.codeChallenge, codeData.codeChallengeMethod)) {
            console.log(`  PKCE verification failed`);
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'invalid_grant', error_description: 'Invalid code_verifier' }));
            break;
          }
        }

        // Delete used code
        authorizationCodes.delete(params.code);

        // Generate access token and c_nonce
        const accessToken = randomString(32);
        const cNonce = randomString(16);
        
        accessTokens.set(accessToken, {
          clientId: codeData.clientId,
          scope: codeData.scope,
          createdAt: Date.now(),
          cNonce,
        });

        console.log(`  Token issued: ${accessToken.substring(0, 8)}... with c_nonce: ${cNonce}`);

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          access_token: accessToken,
          token_type: 'Bearer',
          expires_in: 3600,
          c_nonce: cNonce,
          c_nonce_expires_in: 86400,
          authorization_details: [{
            type: 'openid_credential',
            credential_configuration_id: codeData.scope,
          }],
        }));
      } else {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'unsupported_grant_type' }));
      }
      break;
    }

    // ===== Credential Endpoint =====
    case '/credential': {
      if (req.method !== 'POST') {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'method_not_allowed' }));
        break;
      }

      // Verify access token
      const authHeader = req.headers.authorization;
      if (!authHeader || !authHeader.startsWith('Bearer ')) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid_token' }));
        break;
      }

      const token = authHeader.substring(7);
      const tokenData = accessTokens.get(token);
      
      if (!tokenData) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid_token', error_description: 'Invalid or expired access token' }));
        break;
      }

      const body = await parseBody(req);
      let credentialRequest;
      try {
        credentialRequest = JSON.parse(body);
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid_request', error_description: 'Invalid JSON body' }));
        break;
      }

      console.log(`  Credential request:`, JSON.stringify(credentialRequest).substring(0, 200));

      // Generate a new c_nonce for the response
      const newCNonce = randomString(16);
      tokenData.cNonce = newCNonce;

      // Return mock credential based on format
      const format = credentialRequest.format || 'vc+sd-jwt';
      
      if (format === 'vc+sd-jwt') {
        const credential = generateMockSDJWTCredential(tokenData.cNonce);
        console.log(`  Issuing SD-JWT credential`);
        
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          credential,
          c_nonce: newCNonce,
          c_nonce_expires_in: 86400,
        }));
      } else if (format === 'mso_mdoc') {
        // Return a mock mDL (in practice this would be a CBOR-encoded mdoc)
        console.log(`  Issuing mso_mdoc credential (mock)`);
        
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          credential: 'mock_mdoc_credential_base64',
          c_nonce: newCNonce,
          c_nonce_expires_in: 86400,
        }));
      } else {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'unsupported_credential_format' }));
      }
      break;
    }

    // ===== IACA Certificates =====
    case '/mdoc_iacas':
      if (INCLUDE_IACA) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(iacaCertificates, null, 2));
      } else {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'IACA certificates not available' }));
      }
      break;

    // ===== Health Check =====
    case '/health':
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', issuer: ISSUER_ID }));
      break;

    // ===== Logo/Assets =====
    case '/logo.png':
    case '/identity-logo.png':
    case '/pid-logo.png':
      // Return a simple 1x1 transparent PNG
      const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', 'base64');
      res.writeHead(200, { 'Content-Type': 'image/png' });
      res.end(png);
      break;

    default:
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Not found', path: pathname }));
  }
}

const server = http.createServer(handleRequest);

server.listen(PORT, () => {
  console.log(`Mock Issuer Service running on ${ISSUER_ID}`);
  console.log(`\nEndpoints:`);
  console.log(`  /.well-known/openid-credential-issuer - Credential issuer metadata`);
  console.log(`  /.well-known/oauth-authorization-server - Authorization server metadata`);
  console.log(`  /.well-known/jwks.json - JWKS (signing public keys)`);
  console.log(`  /offer - Generate credential offer (GET ?credential=identity_credential)`);
  console.log(`  /credential-offer - Fetch credential offer by URI`);
  console.log(`  /par - Pushed Authorization Request (POST)`);
  console.log(`  /authorize - Authorization endpoint (auto-approves)`);
  console.log(`  /token - Token endpoint (POST)`);
  console.log(`  /credential - Credential endpoint (POST)`);
  if (INCLUDE_IACA) {
    console.log(`  /mdoc_iacas - IACA certificates`);
  }
  console.log(`  /health - Health check endpoint`);
});

// Handle graceful shutdown
process.on('SIGTERM', () => {
  console.log('Shutting down mock issuer...');
  server.close(() => {
    process.exit(0);
  });
});

process.on('SIGINT', () => {
  console.log('Shutting down mock issuer...');
  server.close(() => {
    process.exit(0);
  });
});
