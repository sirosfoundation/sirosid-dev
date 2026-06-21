/**
 * MCP Server for the Conformance Runner.
 *
 * Exposes test environment information, conformance run management,
 * log access, and Playwright-based test execution via MCP tools and resources.
 *
 * Transport: Streamable HTTP mounted at /mcp on the Express app.
 *
 * Each MCP session gets its own McpServer instance (the SDK only allows
 * one transport connection per McpServer).
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const SERVER_INFO = { name: 'conformance-runner', version: '1.0.0' };

const INSTRUCTIONS = [
  'You are connected to the SIROS conformance test runner.',
  'This MCP provides access to the OpenID conformance test environment,',
  'including the ability to view services, run conformance test plans,',
  'inspect results and logs, and execute arbitrary Playwright tests',
  'against the wallet frontend and other services in the environment.',
  '',
  'Available tools:',
  '  - get_environment: Get full environment status (services, URLs, config)',
  '  - list_plans: List available conformance test plans',
  '  - start_run: Start a conformance test run for a given plan type',
  '  - list_runs: List all conformance runs with status and results',
  '  - get_run: Get detailed status of a specific run',
  '  - get_module_log: Get the detailed test log for a specific module',
  '  - get_module_info: Get info/result for a specific module',
  '  - run_playwright: Execute arbitrary Playwright code against the test environment',
  '  - check_session: Check if there is an authenticated wallet session',
  '  - run_android_conformance: Run conformance tests against the Android sample-app in Waydroid',
  '',
  '## Android Conformance Testing',
  '',
  'The `run_android_conformance` tool runs OID4VCI/OID4VP conformance tests against',
  'the native Android sample-app in Waydroid. Prerequisites:',
  '  - Waydroid running with sample-app installed',
  '  - Conformance suite TLS cert with SANs installed in Android CA store',
  '  - /etc/hosts in Android: 192.168.240.1 localhost.emobix.co.uk',
  '  - wallet-backend with extra_hosts for localhost.emobix.co.uk',
  '  - ADMIN_TOKEN set for auto-registering conformance issuer',
  '',
  'The test runner dispatches openid-credential-offer:// deep links via',
  '`waydroid shell` and monitors logcat for flow completion signals.',
  'The app auto-registers a user via ensureAuthenticatedForTesting().',
  '',
  'See conformance-runner/ANDROID-CONFORMANCE.md for full setup details.',
  '',
  '## R2PS (Remote PAKE-Protected Signing)',
  '',
  'The R2PS service provides a remote WSCD backed by SoftHSM2 for key generation,',
  'signing, and ECDH operations via a PAKE-authenticated protocol (OPAQUE).',
  '',
  '### Starting R2PS',
  '  make up R2PS=yes VC=yes',
  '',
  'This adds:',
  '  - r2ps-server (go-r2ps-service): WSCD + WSCA + admin on port 8443 (protocol) / 8444 (admin)',
  '  - r2ps-softhsm: SoftHSM2 init (token label "r2ps-wscd", PIN "1234")',
  '  - attest-softhsm: Separate HSM for wallet-backend attestation keys',
  '',
  '### Key Provisioning Flow',
  'R2PS keys are provisioned by the wallet SDK itself (not via admin API):',
  '  1. Registration: Wallet registers with R2PS via OPAQUE (creates credential for client_id + context)',
  '  2. Authentication: Wallet authenticates via OPAQUE to get a session (session ID + symmetric key)',
  '  3. Key Generation: Authenticated wallet sends P256Generate request; server creates EC P-256 key in HSM',
  '  4. Signing: Wallet sends sign requests with KID through the authenticated session',
  '',
  '### Admin API (port 8444 / 9444 with conformance)',
  '  - GET /admin/store/keys — List all HSM-generated public keys',
  '  - GET /admin/store/keys?client_id=<id> — Filter keys by wallet client',
  '  - GET /admin/store/keys/<kid> — Get specific key by KID',
  '  - GET /admin/store/statuses/ka — Key attestation status entries',
  '  - GET /admin/store/statuses/wia — Wallet instance attestation status entries',
  '  - PUT /admin/store/status/<category>/<idx> — Update status entry',
  '  - POST /admin/store/allocate/<category> — Allocate new status list index',
  '',
  '### Port Conflict Resolution',
  'When running R2PS alongside the conformance suite (both default to port 8443),',
  'use docker-compose.r2ps-conformance.yml to remap R2PS to 9443/9444:',
  '  docker compose -f docker-compose.r2ps.yml -f docker-compose.r2ps-conformance.yml ...',
  '',
  '### Wallet-Backend Integration',
  'The wallet-backend gets R2PS URL via WALLET_R2PS_URL=http://r2ps-server:8443',
  '(set automatically by docker-compose.r2ps.yml).',
  '',
  '### Android SDK R2PS',
  'For siros-sdk-kotlin sample-app:',
  '  - BuildConfig.R2PS_ENABLED — toggle R2PS (needs native lib with r2ps Cargo feature)',
  '  - DEFAULT_R2PS_URL — server URL (default: http://192.168.240.1:9443 for Waydroid)',
  '',
  '### HSM Details',
  '  Module: /usr/lib/softhsm/libsofthsm2.so',
  '  Token label: r2ps-wscd | PIN: 1234 | SO PIN: 5678',
  '  Key type: EC P-256 | Pool size: 4 sessions',
  '',
  'Use the `get_r2ps_status` tool to query R2PS service health and provisioned keys.',
].join('\n');

/**
 * Register all tools on a McpServer instance.
 */
function registerTools(server, { api, runs, plans, startRun, env }) {
  server.tool(
    'get_environment',
    'Get full test environment status: service URLs, conformance suite connectivity, and configuration',
    {},
    async () => {
      const ready = await api.isReady();
      const serviceChecks = {};
      const urls = {
        walletFrontend: env.WALLET_FRONTEND_URL || 'http://wallet-frontend:3000',
        walletBackend: env.WALLET_BACKEND_URL || 'http://wallet-backend:8080',
        conformanceSuite: env.CONFORMANCE_URL,
        conformancePublic: env.CONFORMANCE_PUBLIC_URL,
        issuer: env.ISSUER_CONFORMANCE_URL,
        verifier: env.VERIFIER_CONFORMANCE_URL,
      };
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            conformanceSuiteConnected: ready,
            urls,
            totalRuns: runs.size,
            activeRuns: [...runs.values()].filter(r => r.status === 'running' || r.status === 'creating').length,
          }, null, 2),
        }],
      };
    }
  );

  server.tool(
    'list_plans',
    'List available conformance test plan types with their configuration',
    {},
    async () => {
      const list = Object.entries(plans).map(([id, p]) => ({
        id,
        label: p.label,
        planName: p.planName,
        phase: p.phase,
        variant: p.variant,
      }));
      return {
        content: [{ type: 'text', text: JSON.stringify(list, null, 2) }],
      };
    }
  );

  server.tool(
    'start_run',
    'Start a conformance test run. Returns the run ID for tracking.',
    { planType: z.enum(Object.keys(plans)).describe('The plan type to run') },
    async ({ planType }) => {
      const ready = await api.isReady();
      if (!ready) {
        return {
          content: [{ type: 'text', text: 'Error: Conformance suite is not available' }],
          isError: true,
        };
      }
      try {
        const result = await startRun(planType);
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              message: `Run started successfully`,
              runId: result.id,
              planType: result.planType,
              hint: 'Use get_run to poll for status and results',
            }, null, 2),
          }],
        };
      } catch (err) {
        return {
          content: [{ type: 'text', text: `Error starting run: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    'list_runs',
    'List all conformance runs with their status, results summary, and timing',
    {},
    async () => {
      const list = [...runs.values()]
        .sort((a, b) => b.startedAt - a.startedAt)
        .map(r => ({
          id: r.id,
          planType: r.planType,
          label: r.label,
          status: r.status,
          startedAt: r.startedAt ? new Date(r.startedAt).toISOString() : null,
          finishedAt: r.finishedAt ? new Date(r.finishedAt).toISOString() : null,
          totalModules: (r.results || []).length,
          passed: (r.results || []).filter(m => m.result === 'PASSED').length,
          failed: (r.results || []).filter(m => m.result !== 'PASSED' && m.result !== 'SKIPPED').length,
          error: r.error || null,
        }));
      return {
        content: [{ type: 'text', text: JSON.stringify(list, null, 2) }],
      };
    }
  );

  server.tool(
    'get_run',
    'Get detailed status of a specific conformance run including all module results',
    { runId: z.string().describe('The run ID to look up') },
    async ({ runId }) => {
      const run = runs.get(runId);
      if (!run) {
        return {
          content: [{ type: 'text', text: `Run not found: ${runId}` }],
          isError: true,
        };
      }
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            ...run,
            startedAt: run.startedAt ? new Date(run.startedAt).toISOString() : null,
            finishedAt: run.finishedAt ? new Date(run.finishedAt).toISOString() : null,
          }, null, 2),
        }],
      };
    }
  );

  server.tool(
    'get_module_log',
    'Get the detailed conformance suite test log for a specific module. Returns all log entries with HTTP requests, responses, and assertion results.',
    { moduleId: z.string().describe('The module ID (e.g. "pKeqtW2eVQHC1MJ")') },
    async ({ moduleId }) => {
      try {
        const [info, log] = await Promise.all([
          api.getModuleInfo(moduleId),
          api.getTestLog(moduleId),
        ]);
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              module: info.testModule,
              testName: info.testName,
              status: info.status,
              result: info.result,
              description: info.description,
              logEntries: log.length,
              log: log,
            }, null, 2),
          }],
        };
      } catch (err) {
        return {
          content: [{ type: 'text', text: `Error fetching log: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    'get_module_info',
    'Get the result and metadata for a specific test module',
    { moduleId: z.string().describe('The module ID') },
    async ({ moduleId }) => {
      try {
        const info = await api.getModuleInfo(moduleId);
        return {
          content: [{ type: 'text', text: JSON.stringify(info, null, 2) }],
        };
      } catch (err) {
        return {
          content: [{ type: 'text', text: `Error: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    'check_session',
    'Check if there is an active authenticated session on the wallet frontend by attempting to access the dashboard',
    { tenantId: z.string().optional().describe('Tenant ID to check (default: "default")') },
    async ({ tenantId }) => {
      const tid = tenantId || 'default';
      const frontendUrl = env.WALLET_FRONTEND_URL || 'http://wallet-frontend:3000';
      try {
        const resp = await fetch(`${frontendUrl}/id/${tid}/api/session`, {
          headers: { 'Accept': 'application/json' },
          signal: AbortSignal.timeout(5000),
        });
        if (resp.ok) {
          const data = await resp.json().catch(() => null);
          return {
            content: [{
              type: 'text',
              text: JSON.stringify({
                authenticated: true,
                tenantId: tid,
                session: data,
              }, null, 2),
            }],
          };
        }
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              authenticated: false,
              tenantId: tid,
              httpStatus: resp.status,
            }, null, 2),
          }],
        };
      } catch (err) {
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              authenticated: false,
              tenantId: tid,
              error: err.message,
            }, null, 2),
          }],
        };
      }
    }
  );

  server.tool(
    'run_playwright',
    'Execute arbitrary Playwright code against the test environment. ' +
    'The code runs in an async function with `browser`, `context`, `page` already set up ' +
    '(headless Chromium with HTTPS errors ignored and WebAuthn virtual authenticator). ' +
    'Return a value from your code to get it back as the result. ' +
    'Available globals: browser, context, page, cdp (CDP session), FRONTEND_URL, ADMIN_URL.',
    {
      code: z.string().describe(
        'JavaScript code to execute. Has access to Playwright page, context, browser, cdp objects. ' +
        'Use `return` to return results. Example: `await page.goto(FRONTEND_URL); return await page.title();`'
      ),
      timeout: z.number().optional().describe('Timeout in milliseconds (default: 30000, max: 300000)'),
    },
    async ({ code, timeout }) => {
      const timeoutMs = Math.min(timeout || 30000, 300000);
      let browser;
      try {
        browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
        const context = await browser.newContext({ ignoreHTTPSErrors: true });
        const page = await context.newPage();

        // Set up CDP virtual authenticator
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

        const FRONTEND_URL = env.WALLET_FRONTEND_URL || 'http://wallet-frontend:3000';
        const ADMIN_URL = env.ADMIN_URL || 'http://amity:8080';

        // Execute the user's code
        const fn = new Function(
          'browser', 'context', 'page', 'cdp',
          'FRONTEND_URL', 'ADMIN_URL', 'fetch',
          `return (async () => { ${code} })();`
        );

        const result = await Promise.race([
          fn(browser, context, page, cdp, FRONTEND_URL, ADMIN_URL, fetch),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error(`Playwright execution timed out after ${timeoutMs}ms`)), timeoutMs)
          ),
        ]);

        await browser.close();
        browser = null;

        const resultText = result !== undefined
          ? (typeof result === 'string' ? result : JSON.stringify(result, null, 2))
          : '(no return value)';

        return {
          content: [{ type: 'text', text: resultText }],
        };
      } catch (err) {
        if (browser) await browser.close().catch(() => {});
        return {
          content: [{ type: 'text', text: `Playwright error: ${err.message}\n\n${err.stack || ''}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    'run_android_conformance',
    'Run OID4VCI or OID4VP conformance tests against the Android sample-app in Waydroid. ' +
    'Requires Waydroid running with the sample-app installed, conformance suite TLS cert in Android CA store, ' +
    'and wallet-backend accessible. Returns test results (pass/fail per module).',
    {
      plan: z.enum(['vci', 'vp', 'all']).describe('Which conformance plan to run: vci, vp, or all'),
    },
    async ({ plan }) => {
      const execFileAsync = promisify(execFile);
      const scriptPath = new URL('../../run-android-conformance.mjs', import.meta.url).pathname;

      try {
        const envVars = {
          ...process.env,
          NODE_TLS_REJECT_UNAUTHORIZED: '0',
          CONFORMANCE_URL: env.CONFORMANCE_PUBLIC_URL || env.CONFORMANCE_URL,
          ADMIN_URL: env.ADMIN_URL || 'http://localhost:8081',
          ADMIN_TOKEN: env.ADMIN_TOKEN || '',
        };

        const { stdout, stderr } = await execFileAsync(
          'node',
          [scriptPath, '--plan', plan],
          { env: envVars, timeout: 600_000, maxBuffer: 10 * 1024 * 1024 }
        );

        return {
          content: [{ type: 'text', text: stdout + (stderr ? `\n\nSTDERR:\n${stderr}` : '') }],
        };
      } catch (err) {
        const output = (err.stdout || '') + (err.stderr ? `\n\nSTDERR:\n${err.stderr}` : '');
        return {
          content: [{
            type: 'text',
            text: `Android conformance run failed (exit ${err.code}):\n${output}\n\n${err.message}`,
          }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    'get_r2ps_status',
    'Get R2PS (Remote PAKE-Protected Signing) service status including health, ' +
    'provisioned HSM keys, and status list entries. Use this to verify R2PS is running ' +
    'and to inspect keys generated by wallet clients.',
    {
      client_id: z.string().optional().describe('Filter keys by wallet client ID'),
      kid: z.string().optional().describe('Get a specific key by KID'),
    },
    async ({ client_id, kid }) => {
      const r2psUrl = env.R2PS_URL || 'http://localhost:8443';
      const r2psAdminUrl = env.R2PS_ADMIN_URL || 'http://localhost:8444';
      const result = {};

      // Health check
      try {
        const healthResp = await fetch(`${r2psUrl}/healthz`);
        result.health = { status: healthResp.ok ? 'healthy' : 'unhealthy', httpStatus: healthResp.status };
      } catch (err) {
        result.health = { status: 'unreachable', error: err.message };
      }

      // If asking for a specific key
      if (kid) {
        try {
          const resp = await fetch(`${r2psAdminUrl}/admin/store/keys/${encodeURIComponent(kid)}`);
          result.key = resp.ok ? await resp.json() : { error: `HTTP ${resp.status}` };
        } catch (err) {
          result.key = { error: err.message };
        }
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      // List keys (optionally filtered by client_id)
      try {
        const keysUrl = client_id
          ? `${r2psAdminUrl}/admin/store/keys?client_id=${encodeURIComponent(client_id)}`
          : `${r2psAdminUrl}/admin/store/keys`;
        const keysResp = await fetch(keysUrl);
        result.keys = keysResp.ok ? await keysResp.json() : { error: `HTTP ${keysResp.status}` };
      } catch (err) {
        result.keys = { error: err.message };
      }

      // Status lists
      for (const category of ['ka', 'wia']) {
        try {
          const resp = await fetch(`${r2psAdminUrl}/admin/store/statuses/${category}`);
          result[`status_${category}`] = resp.ok ? await resp.json() : { error: `HTTP ${resp.status}` };
        } catch (err) {
          result[`status_${category}`] = { error: err.message };
        }
      }

      result.urls = { protocol: r2psUrl, admin: r2psAdminUrl };
      result.hsm = { module: '/usr/lib/softhsm/libsofthsm2.so', tokenLabel: 'r2ps-wscd', pin: '1234', keyType: 'EC P-256' };

      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    }
  );
}

/**
 * Create a fresh McpServer instance with all tools registered.
 */
function createServer(opts) {
  const server = new McpServer(SERVER_INFO, { instructions: INSTRUCTIONS });
  registerTools(server, opts);
  return server;
}

/**
 * Mount the MCP Streamable HTTP endpoint on the Express app.
 */
export function setupMcpServer(opts) {
  const { app } = opts;
  const sessions = {}; // sessionId → { transport, server }

  app.all('/mcp', async (req, res) => {
    const sessionId = req.headers['mcp-session-id'];

    if (sessionId && sessions[sessionId]) {
      await sessions[sessionId].transport.handleRequest(req, res, req.body);
    } else if (req.method === 'GET' || req.method === 'DELETE') {
      res.status(400).json({ error: 'No active session. Send a POST with initialize first.' });
    } else {
      // New session — fresh McpServer + transport
      const server = createServer(opts);
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => {
          sessions[id] = { transport, server };
        },
      });

      transport.onclose = () => {
        if (transport.sessionId) {
          delete sessions[transport.sessionId];
        }
      };

      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    }
  });

  console.log('MCP server mounted at /mcp (Streamable HTTP)');
}
