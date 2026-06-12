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
