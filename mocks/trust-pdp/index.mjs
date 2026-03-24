/**
 * Mock AuthZEN PDP Service for E2E Testing
 *
 * Simulates an AuthZEN-compliant Policy Decision Point for trust evaluation.
 * Can mimic go-trust static registries for testing different trust scenarios.
 *
 * Endpoints:
 * - /.well-known/authzen-configuration - AuthZEN discovery
 * - /access/v1/evaluation - Access evaluation endpoint
 * - /mode - Control endpoint to change trust mode dynamically
 *
 * Usage:
 *   node index.mjs
 *
 * Environment variables:
 *   PORT - Server port (default: 9090)
 *   PDP_ID - PDP identifier URL (default: http://localhost:9090)
 *   TRUST_MODE - Static trust mode: 'default' | 'always' | 'never' (default: 'default')
 *   TRUSTED_ISSUERS - Comma-separated list of trusted issuer URLs
 *   TRUSTED_VERIFIERS - Comma-separated list of trusted verifier URLs
 *
 * Trust Modes (mimic go-trust static registries):
 *   - default: Use configured trust policies (issuers/verifiers lists)
 *   - always: Always return decision=true (like AlwaysTrustedRegistry)
 *   - never: Always return decision=false (like NeverTrustedRegistry)
 */

import http from 'http';

const PORT = parseInt(process.env.PORT || '9090', 10);
const PDP_ID = process.env.PDP_ID || `http://localhost:${PORT}`;

// Current trust mode - can be changed dynamically via /mode endpoint
let trustMode = process.env.TRUST_MODE || 'default';

// Configurable trusted entities
const TRUSTED_ISSUERS = (process.env.TRUSTED_ISSUERS || 'http://localhost:9000').split(',').map(s => s.trim());
const TRUSTED_VERIFIERS = (process.env.TRUSTED_VERIFIERS || 'http://localhost:9001').split(',').map(s => s.trim());

// AuthZEN discovery metadata
const authzenMetadata = {
  policy_decision_point: PDP_ID,
  access_evaluation_endpoint: `${PDP_ID}/access/v1/evaluation`,
  access_evaluation_batch_endpoint: `${PDP_ID}/access/v1/evaluations`,
  api_version: '1.0',
  trust_frameworks_supported: ['test-framework', 'eudi-wallet'],
  evaluation_types_supported: ['trust', 'authorization'],
};

function escapeRegex(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Build trust policies from environment
function buildTrustPolicies() {
  const policies = [];
  
  // Explicitly trusted issuers
  for (const issuer of TRUSTED_ISSUERS) {
    policies.push({
      entity_pattern: new RegExp(`^${escapeRegex(issuer)}$`),
      role: 'issuer',
      trusted: true,
      reason: 'Issuer is in the trusted issuers list',
      trust_framework: 'test-framework',
    });
  }
  
  // Explicitly trusted verifiers
  for (const verifier of TRUSTED_VERIFIERS) {
    policies.push({
      entity_pattern: new RegExp(`^${escapeRegex(verifier)}$`),
      role: 'verifier',
      trusted: true,
      reason: 'Verifier is in the trusted verifiers list',
      trust_framework: 'test-framework',
    });
  }
  
  // Trust anything on localhost for testing
  policies.push({
    entity_pattern: /^https?:\/\/localhost(:\d+)?/,
    role: 'issuer',
    trusted: true,
    reason: 'Local development issuer - trusted for testing',
    trust_framework: 'local-dev',
  });
  policies.push({
    entity_pattern: /^https?:\/\/localhost(:\d+)?/,
    role: 'verifier',
    trusted: true,
    reason: 'Local development verifier - trusted for testing',
    trust_framework: 'local-dev',
  });
  
  return policies;
}

const trustPolicies = buildTrustPolicies();

function evaluateTrust(request) {
  // Extract entity and role from the request
  const entityId = request.resource?.id || '';
  const role = request.resource?.properties?.role || 
               request.action?.properties?.role ||
               'unknown';

  // Handle static trust modes (mimicking go-trust static registries)
  if (trustMode === 'always') {
    return {
      decision: true,
      context: {
        reason: 'AlwaysTrusted: all requests are trusted',
        trust_framework: 'static-always',
        entity_identifier: entityId,
        role: role,
        mode: 'always',
      },
    };
  }

  if (trustMode === 'never') {
    return {
      decision: false,
      context: {
        reason: 'NeverTrusted: all requests are denied',
        trust_framework: 'static-never',
        entity_identifier: entityId,
        role: role,
        mode: 'never',
      },
    };
  }

  // Default mode: use configured policies
  // Find matching policy
  for (const policy of trustPolicies) {
    if (policy.entity_pattern.test(entityId) && policy.role === role) {
      return {
        decision: policy.trusted,
        context: {
          reason: policy.reason,
          trust_framework: policy.trust_framework,
          entity_identifier: entityId,
          role: role,
          mode: 'default',
        },
      };
    }
  }

  // Default: not trusted
  return {
    decision: false,
    context: {
      reason: `No trust policy found for ${role} ${entityId}`,
      entity_identifier: entityId,
      role: role,
      mode: 'default',
    },
  };
}

function handleRequest(req, res) {
  const url = req.url || '/';

  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  console.log(`[${new Date().toISOString()}] ${req.method} ${url}`);

  switch (url) {
    case '/.well-known/authzen-configuration':
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(authzenMetadata, null, 2));
      break;

    case '/access/v1/evaluation':
      if (req.method === 'POST') {
        let body = '';
        req.on('data', chunk => {
          body += chunk.toString();
        });
        req.on('end', () => {
          try {
            const evalRequest = JSON.parse(body);
            console.log('Evaluation request:', JSON.stringify(evalRequest, null, 2));

            const evalResponse = evaluateTrust(evalRequest);
            console.log('Evaluation response:', JSON.stringify(evalResponse, null, 2));

            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(evalResponse, null, 2));
          } catch (err) {
            console.error('Error processing evaluation request:', err);
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ 
              error: 'Invalid request',
              message: err.message,
            }));
          }
        });
      } else {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Method not allowed' }));
      }
      break;

    case '/health':
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ 
        status: 'ok', 
        pdp: PDP_ID,
        mode: trustMode,
        trusted_issuers: TRUSTED_ISSUERS.length,
        trusted_verifiers: TRUSTED_VERIFIERS.length,
      }));
      break;

    case '/mode':
      if (req.method === 'GET') {
        // Return current mode
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ 
          mode: trustMode,
          available_modes: ['default', 'always', 'never'],
        }));
      } else if (req.method === 'POST') {
        // Change mode
        let body = '';
        req.on('data', chunk => {
          body += chunk.toString();
        });
        req.on('end', () => {
          try {
            const data = JSON.parse(body);
            const newMode = data.mode;
            if (['default', 'always', 'never'].includes(newMode)) {
              const oldMode = trustMode;
              trustMode = newMode;
              console.log(`Trust mode changed: ${oldMode} -> ${newMode}`);
              res.writeHead(200, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ 
                mode: trustMode,
                previous_mode: oldMode,
              }));
            } else {
              res.writeHead(400, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ 
                error: 'Invalid mode',
                message: `Mode must be one of: default, always, never`,
              }));
            }
          } catch (err) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ 
              error: 'Invalid JSON',
              message: err.message,
            }));
          }
        });
        return; // Don't end the request here
      } else {
        res.writeHead(405, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Method not allowed' }));
      }
      break;

    case '/policies':
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        trusted_issuers: TRUSTED_ISSUERS,
        trusted_verifiers: TRUSTED_VERIFIERS,
        policy_count: trustPolicies.length,
      }, null, 2));
      break;

    default:
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Not found', path: url }));
  }
}

const server = http.createServer(handleRequest);

server.listen(PORT, () => {
  console.log(`Mock AuthZEN PDP Service running on ${PDP_ID}`);
  console.log(`  /.well-known/authzen-configuration - AuthZEN discovery`);
  console.log(`  /access/v1/evaluation - Access evaluation endpoint`);
  console.log(`  /health - Health check endpoint`);
  console.log(`  /policies - Debug: view configured policies`);
  console.log(`\nTrusted Issuers: ${TRUSTED_ISSUERS.join(', ')}`);
  console.log(`Trusted Verifiers: ${TRUSTED_VERIFIERS.join(', ')}`);
});

// Handle graceful shutdown
process.on('SIGTERM', () => {
  console.log('Shutting down mock PDP...');
  server.close(() => {
    process.exit(0);
  });
});

process.on('SIGINT', () => {
  console.log('Shutting down mock PDP...');
  server.close(() => {
    process.exit(0);
  });
});
