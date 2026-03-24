/**
 * Mock Verifier Service for E2E Testing - Full OID4VP Flow
 *
 * Simulates a complete OpenID4VP verifier with:
 * - /.well-known/openid4vp-verifier endpoint
 * - /create-request endpoint to initiate verification
 * - /request/:id endpoint to serve authorization requests
 * - /response/:id endpoint for direct_post VP responses
 * - /status/:id endpoint to check verification status
 *
 * Usage:
 *   npx ts-node mocks/verifier/index.ts
 *
 * Environment variables:
 *   PORT - Server port (default: 9001)
 *   VERIFIER_ID - Verifier identifier URL (default: http://localhost:9001)
 *   WALLET_URL - Wallet frontend URL (default: http://localhost:3000)
 */

import * as http from 'http';
import * as crypto from 'crypto';

const PORT = parseInt(process.env.PORT || '9001', 10);
const VERIFIER_ID = process.env.VERIFIER_ID || `http://localhost:${PORT}`;
const WALLET_URL = process.env.WALLET_URL || 'http://localhost:3000';

// Generate signing keypair at startup (ES256 = P-256 curve)
const { privateKey: signingPrivateKey, publicKey: signingPublicKey } = crypto.generateKeyPairSync('ec', {
  namedCurve: 'P-256',
});

// Export public key as JWK for JWKS endpoint
function getPublicKeyJWK(): Record<string, any> {
  const jwk = signingPublicKey.export({ format: 'jwk' });
  return {
    ...jwk,
    kid: 'verifier-signing-key-1',
    use: 'sig',
    alg: 'ES256',
  };
}

// Sign a request object JWT with the verifier's private key
function signRequestObject(payload: Record<string, any>): string {
  const header = {
    alg: 'ES256',
    typ: 'oauth-authz-req+jwt',
    kid: 'verifier-signing-key-1',
  };
  
  const headerBase64 = Buffer.from(JSON.stringify(header)).toString('base64url');
  const payloadBase64 = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const signingInput = `${headerBase64}.${payloadBase64}`;
  
  const signature = crypto.sign('SHA256', Buffer.from(signingInput), signingPrivateKey);
  const signatureBase64 = signature.toString('base64url');
  
  return `${headerBase64}.${payloadBase64}.${signatureBase64}`;
}

// In-memory storage for presentation requests and responses
const presentationRequests = new Map<string, any>();
const presentationResponses = new Map<string, any>();

// Generate random string
function generateRandomString(length: number = 32): string {
  return crypto.randomBytes(length).toString('base64url').slice(0, length);
}

// Verifier metadata
const verifierMetadata = {
  issuer: VERIFIER_ID,
  client_id: VERIFIER_ID,
  client_name: 'E2E Test Verifier',
  redirect_uris: [`${VERIFIER_ID}/callback`],
  response_types_supported: ['vp_token'],
  response_modes_supported: ['direct_post'],
  vp_formats: {
    'vc+sd-jwt': {
      'sd-jwt_alg_values': ['ES256', 'ES384', 'ES512'],
      'kb-jwt_alg_values': ['ES256', 'ES384', 'ES512'],
    },
    mso_mdoc: {
      alg: ['ES256', 'ES384', 'ES512'],
    },
    jwt_vp: {
      alg: ['ES256', 'ES384'],
    },
  },
  presentation_definition_uri_supported: true,
  request_object_signing_alg_values_supported: ['ES256'],
  display: [
    {
      name: 'E2E Test Verifier',
      locale: 'en-US',
      logo: {
        uri: `${VERIFIER_ID}/logo.png`,
        alt_text: 'Test Verifier Logo',
      },
    },
  ],
  trust_framework: 'test-framework',
  organization_name: 'Test Organization',
  organization_id: 'test-org-001',
};

// Create a DCQL query for identity credential
function createDCQLQuery() {
  return {
    credentials: [
      {
        id: 'identity_credential',
        format: 'vc+sd-jwt',
        meta: {
          vct_values: ['https://example.com/identity_credential'],
        },
        claims: [
          { id: 'given_name', path: ['given_name'] },
          { id: 'family_name', path: ['family_name'] },
        ],
      },
    ],
  };
}

// Parse request body
async function parseBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk: Buffer) => body += chunk.toString());
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

// Parse URL-encoded form data
function parseFormData(body: string): Record<string, string> {
  const params = new URLSearchParams(body);
  const result: Record<string, string> = {};
  params.forEach((value, key) => {
    result[key] = value;
  });
  return result;
}

// Handle creating a new presentation request
function handleCreateRequest(req: http.IncomingMessage, res: http.ServerResponse) {
  const requestId = generateRandomString(32);
  const nonce = generateRandomString(32);
  const state = generateRandomString(32);

  const dcqlQuery = createDCQLQuery();

  presentationRequests.set(requestId, {
    requestId,
    nonce,
    state,
    dcqlQuery,
    created: Date.now(),
    status: 'pending',
  });

  const requestUri = `${VERIFIER_ID}/request/${requestId}`;
  
  // Create wallet redirect URL
  const walletUrl = new URL(WALLET_URL);
  walletUrl.searchParams.set('client_id', VERIFIER_ID);
  walletUrl.searchParams.set('request_uri', requestUri);

  console.log('Created verification request:', requestId);

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    request_id: requestId,
    request_uri: requestUri,
    wallet_url: walletUrl.toString(),
    nonce,
    state,
    expires_in: 300,
  }));
}

// Handle fetching the request object (request_uri endpoint)
function handleGetRequest(requestId: string, res: http.ServerResponse) {
  const request = presentationRequests.get(requestId);
  
  if (!request) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'request_not_found' }));
    return;
  }

  // Return the authorization request object
  const authorizationRequest = {
    response_type: 'vp_token',
    client_id: VERIFIER_ID,
    client_id_scheme: 'redirect_uri',
    response_uri: `${VERIFIER_ID}/response/${requestId}`,
    response_mode: 'direct_post',
    nonce: request.nonce,
    state: request.state,
    dcql_query: request.dcqlQuery,
    client_metadata: verifierMetadata,
  };

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(authorizationRequest));
}

// Handle direct_post presentation response
async function handleResponse(requestId: string, req: http.IncomingMessage, res: http.ServerResponse) {
  const request = presentationRequests.get(requestId);
  
  if (!request) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'request_not_found' }));
    return;
  }

  const body = await parseBody(req);
  const params = parseFormData(body);

  console.log('Received presentation response:', {
    requestId,
    hasVpToken: !!params.vp_token,
    state: params.state,
  });

  // Store the response
  presentationResponses.set(requestId, {
    requestId,
    vpToken: params.vp_token,
    presentationSubmission: params.presentation_submission,
    state: params.state,
    received: Date.now(),
  });

  // Update request status
  request.status = 'completed';

  // Return success with redirect URL
  const successUrl = new URL(`${VERIFIER_ID}/success`);
  successUrl.searchParams.set('request_id', requestId);

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    redirect_uri: successUrl.toString(),
  }));
}

// Handle checking the status of a request
function handleStatus(requestId: string, res: http.ServerResponse) {
  const request = presentationRequests.get(requestId);
  
  if (!request) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'request_not_found' }));
    return;
  }

  const response = presentationResponses.get(requestId);

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({
    request_id: requestId,
    status: request.status,
    has_response: !!response,
    response: response ? {
      received: response.received,
      has_vp_token: !!response.vpToken,
    } : null,
  }));
}

// Handle success page
function handleSuccess(res: http.ServerResponse, query: Record<string, string>) {
  const requestId = query.request_id;
  const response = presentationResponses.get(requestId);

  res.writeHead(200, { 'Content-Type': 'text/html' });
  res.end(`
<!DOCTYPE html>
<html>
<head>
  <title>Verification Successful</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
    .success { color: green; font-size: 24px; margin-bottom: 20px; }
    .details { background: #f5f5f5; padding: 15px; border-radius: 5px; }
  </style>
</head>
<body>
  <div class="success">✓ Verification Successful</div>
  <div class="details">
    <h3>Request ID: ${requestId}</h3>
    <p>VP Token received: ${response ? 'Yes' : 'No'}</p>
    ${response ? `<p>Received at: ${new Date(response.received).toISOString()}</p>` : ''}
  </div>
  <script>
    window.verificationComplete = true;
    window.requestId = '${requestId}';
  </script>
</body>
</html>
  `);
}

async function handleRequest(req: http.IncomingMessage, res: http.ServerResponse) {
  const urlParts = (req.url || '').split('?');
  const pathname = urlParts[0];
  const query = Object.fromEntries(new URLSearchParams(urlParts[1] || ''));

  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  console.log(`[${new Date().toISOString()}] ${req.method} ${pathname}`);

  try {
    // Static routes
    switch (pathname) {
      case '/.well-known/openid4vp-verifier':
      case '/.well-known/openid-configuration':
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(verifierMetadata, null, 2));
        return;

      case '/.well-known/jwks.json': {
        // Return the verifier's public signing key as JWKS
        const jwks = {
          keys: [getPublicKeyJWK()],
        };
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(jwks, null, 2));
        return;
      }

      case '/create-request':
        if (req.method === 'POST') {
          handleCreateRequest(req, res);
        } else {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'method_not_allowed' }));
        }
        return;

      // Legacy endpoint for compatibility
      case '/presentation_request':
        if (req.method === 'POST') {
          handleCreateRequest(req, res);
        } else {
          res.writeHead(405, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'method_not_allowed' }));
        }
        return;

      case '/success':
        handleSuccess(res, query);
        return;

      case '/health':
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', verifier: VERIFIER_ID }));
        return;

      case '/':
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(`
<!DOCTYPE html>
<html>
<head><title>E2E Test Verifier</title></head>
<body>
  <h1>E2E Test Verifier</h1>
  <p>Verifier ID: ${VERIFIER_ID}</p>
  <p><a href="/.well-known/openid4vp-verifier">Verifier Metadata</a></p>
  <button onclick="createRequest()">Create Verification Request</button>
  <div id="result"></div>
  <script>
    async function createRequest() {
      const res = await fetch('/create-request', { method: 'POST' });
      const data = await res.json();
      document.getElementById('result').innerHTML = 
        '<p>Request ID: ' + data.request_id + '</p>' +
        '<p><a href="' + data.wallet_url + '">Open in Wallet</a></p>' +
        '<p><a href="/status/' + data.request_id + '">Check Status</a></p>';
    }
  </script>
</body>
</html>
        `);
        return;
    }

    // Dynamic routes
    if (pathname.startsWith('/request/')) {
      const requestId = pathname.split('/')[2];
      handleGetRequest(requestId, res);
      return;
    }

    if (pathname.startsWith('/response/')) {
      const requestId = pathname.split('/')[2];
      if (req.method === 'POST') {
        await handleResponse(requestId, req, res);
      } else {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'method_not_allowed' }));
      }
      return;
    }

    if (pathname.startsWith('/status/')) {
      const requestId = pathname.split('/')[2];
      handleStatus(requestId, res);
      return;
    }

    // Legacy dynamic route for requests
    if (pathname.startsWith('/requests/')) {
      const requestId = pathname.split('/').pop() || '';
      handleGetRequest(requestId, res);
      return;
    }

    // Not found
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not_found', path: pathname }));
  } catch (error) {
    console.error('Request handler error:', error);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'internal_error' }));
  }
}

const server = http.createServer(handleRequest);

server.listen(PORT, () => {
  console.log(`Full Flow Mock Verifier running on ${VERIFIER_ID}`);
  console.log(`  /.well-known/openid4vp-verifier - Verifier metadata`);
  console.log(`  /.well-known/jwks.json - JWKS (signing public keys)`);
  console.log(`  /create-request - Create verification request (POST)`);
  console.log(`  /request/:id - Get authorization request`);
  console.log(`  /response/:id - Receive direct_post response (POST)`);
  console.log(`  /status/:id - Check request status`);
  console.log(`  /success - Success page`);
  console.log(`  /health - Health check endpoint`);
});

// Handle graceful shutdown
process.on('SIGTERM', () => {
  console.log('Shutting down mock verifier...');
  server.close(() => process.exit(0));
});

process.on('SIGINT', () => {
  console.log('Shutting down mock verifier...');
  server.close(() => process.exit(0));
});
