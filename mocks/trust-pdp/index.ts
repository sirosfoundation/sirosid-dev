/**
 * Mock AuthZEN PDP Service for E2E Testing
 *
 * Simulates an AuthZEN-compliant Policy Decision Point for trust evaluation.
 * This mock implements a simplified version of go-trust for testing.
 *
 * Endpoints:
 * - /.well-known/authzen-configuration - AuthZEN discovery
 * - /access/v1/evaluation - Access evaluation endpoint
 *
 * Usage:
 *   npx ts-node mocks/trust-pdp/index.ts
 *
 * Environment variables:
 *   PORT - Server port (default: 9090)
 *   PDP_ID - PDP identifier URL (default: http://localhost:9090)
 *   TRUSTED_ISSUERS - Comma-separated list of trusted issuer URLs
 *   TRUSTED_VERIFIERS - Comma-separated list of trusted verifier URLs
 */

import * as http from 'http';

const PORT = parseInt(process.env.PORT || '9090', 10);
const PDP_ID = process.env.PDP_ID || `http://localhost:${PORT}`;

// Configurable trusted entities
const TRUSTED_ISSUERS = (process.env.TRUSTED_ISSUERS || 'http://localhost:9000').split(',').map(s => s.trim());
const TRUSTED_VERIFIERS = (process.env.TRUSTED_VERIFIERS || 'http://localhost:9001').split(',').map(s => s.trim());

// AuthZEN discovery metadata
const authzenMetadata = {
  policy_decision_point: PDP_ID,
  access_evaluation_endpoint: `${PDP_ID}/access/v1/evaluation`,
  access_evaluation_batch_endpoint: `${PDP_ID}/access/v1/evaluations`,
  api_version: '1.0',
  // go-trust specific extensions
  trust_frameworks_supported: ['test-framework', 'eudi-wallet'],
  evaluation_types_supported: ['trust', 'authorization'],
};

// Mock trust policies
interface TrustPolicy {
  entity_pattern: RegExp;
  role: 'issuer' | 'verifier';
  trusted: boolean;
  reason: string;
  trust_framework?: string;
}

const trustPolicies: TrustPolicy[] = [
  // Explicitly trusted issuers
  ...TRUSTED_ISSUERS.map(issuer => ({
    entity_pattern: new RegExp(`^${escapeRegex(issuer)}$`),
    role: 'issuer' as const,
    trusted: true,
    reason: 'Issuer is in the trusted issuers list',
    trust_framework: 'test-framework',
  })),
  // Explicitly trusted verifiers
  ...TRUSTED_VERIFIERS.map(verifier => ({
    entity_pattern: new RegExp(`^${escapeRegex(verifier)}$`),
    role: 'verifier' as const,
    trusted: true,
    reason: 'Verifier is in the trusted verifiers list',
    trust_framework: 'test-framework',
  })),
  // Example: Trust anything on localhost for testing
  {
    entity_pattern: /^https?:\/\/localhost(:\d+)?/,
    role: 'issuer' as const,
    trusted: true,
    reason: 'Local development issuer - trusted for testing',
    trust_framework: 'local-dev',
  },
  {
    entity_pattern: /^https?:\/\/localhost(:\d+)?/,
    role: 'verifier' as const,
    trusted: true,
    reason: 'Local development verifier - trusted for testing',
    trust_framework: 'local-dev',
  },
  // Example: Trust EUDI reference issuers
  {
    entity_pattern: /\.eudi\.wallet\.gov|\.ec\.europa\.eu/,
    role: 'issuer' as const,
    trusted: true,
    reason: 'EUDI reference implementation issuer',
    trust_framework: 'eudi-wallet',
  },
];

function escapeRegex(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

interface EvaluationRequest {
  subject: {
    type: string;
    id?: string;
    properties?: Record<string, unknown>;
  };
  resource: {
    type: string;
    id: string;
    properties?: Record<string, unknown>;
  };
  action: {
    name: string;
    properties?: Record<string, unknown>;
  };
  context?: Record<string, unknown>;
}

interface EvaluationResponse {
  decision: boolean;
  context?: {
    reason?: string;
    trust_framework?: string;
    entity_identifier?: string;
    role?: string;
  };
}

function evaluateTrust(request: EvaluationRequest): EvaluationResponse {
  // Extract entity and role from the request
  const entityId = request.resource.id;
  const role = request.resource.properties?.role as string || 
               request.action.properties?.role as string ||
               'unknown';

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
    },
  };
}

function handleRequest(req: http.IncomingMessage, res: http.ServerResponse) {
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
            const evalRequest = JSON.parse(body) as EvaluationRequest;
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
              message: err instanceof Error ? err.message : 'Unknown error',
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
        trusted_issuers: TRUSTED_ISSUERS.length,
        trusted_verifiers: TRUSTED_VERIFIERS.length,
      }));
      break;

    case '/policies':
      // Debug endpoint to see configured policies
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
