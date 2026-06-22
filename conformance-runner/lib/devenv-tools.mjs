/**
 * Dev Environment MCP Tools
 *
 * Exposes the full sirosid-dev Makefile as MCP tools so an AI agent
 * can start, stop, configure, and inspect the entire development environment
 * including tunnels, Android setup, conformance suite, and more.
 *
 * All operations run `make <target> [OPTIONS]` in the project root.
 * Output is streamed and returned as text.
 */

import { z } from 'zod';
import { spawn, exec } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync, readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const execAsync = promisify(exec);
const __dirname = dirname(fileURLToPath(import.meta.url));

/** The sirosid-dev project root — two levels up from conformance-runner/lib/ */
const PROJECT_ROOT = resolve(__dirname, '..', '..');

/** Run a make target with optional variables, return { stdout, stderr, exitCode } */
async function runMake(target, vars = {}, opts = {}) {
  const varArgs = Object.entries(vars)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${v}`);

  return new Promise((resolve) => {
    const args = [target, ...varArgs];
    const proc = spawn('make', ['--no-print-directory', ...args], {
      cwd: PROJECT_ROOT,
      env: { ...process.env, FORCE_COLOR: '0' },
      timeout: opts.timeout || 300_000,
    });

    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });
    proc.on('close', exitCode => {
      resolve({ stdout: stdout.trim(), stderr: stderr.trim(), exitCode });
    });
    proc.on('error', err => {
      resolve({ stdout: '', stderr: err.message, exitCode: -1 });
    });
  });
}

/** Read a generated env file (key=value format) into an object */
function readEnvFile(filename) {
  const path = resolve(PROJECT_ROOT, filename);
  if (!existsSync(path)) return null;
  const lines = readFileSync(path, 'utf-8').trim().split('\n');
  return Object.fromEntries(
    lines
      .filter(l => l.includes('=') && !l.startsWith('#'))
      .map(l => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)])
  );
}

/** Check docker service health via curl */
async function checkService(url, name) {
  try {
    const { stdout } = await execAsync(
      `curl -sf --max-time 3 ${url} -o /dev/null -w "%{http_code}"`,
      { timeout: 5000 }
    );
    return { name, url, status: parseInt(stdout) < 400 ? 'running' : 'unhealthy', httpStatus: parseInt(stdout) };
  } catch {
    return { name, url, status: 'not running' };
  }
}

/** Format make result as MCP content */
function makeResult({ stdout, stderr, exitCode }, label) {
  const lines = [];
  if (label) lines.push(`## ${label}`);
  lines.push(`Exit code: ${exitCode}`);
  if (stdout) { lines.push(''); lines.push(stdout); }
  if (stderr && exitCode !== 0) { lines.push(''); lines.push('STDERR:'); lines.push(stderr); }
  return {
    content: [{ type: 'text', text: lines.join('\n') }],
    isError: exitCode !== 0,
  };
}

/**
 * Register all dev environment tools on an MCP server instance.
 * Call this from setupMcpServer() in mcp-server.mjs.
 */
export function registerDevenvTools(server) {

  // ── Environment status ────────────────────────────────────────────────────

  server.tool(
    'devenv_status',
    'Get the current status of the dev environment: which services are running, ' +
    'active tunnel URLs, generated env files, and whether the e2e-test-network exists.',
    {},
    async () => {
      const [frontend, backend, vcIssuer, vcVerifier, goTrustAllow] = await Promise.all([
        checkService('http://localhost:3000', 'wallet-frontend'),
        checkService('http://localhost:8080/health', 'wallet-backend'),
        checkService('http://localhost:9000/health', 'vc-issuer'),
        checkService('http://localhost:9001/health', 'vc-verifier'),
        checkService('http://localhost:9095/healthz', 'go-trust-allow'),
      ]);

      const tunnel = readEnvFile('.env.tunnel');
      const android = readEnvFile('.env.android');

      let networkExists = false;
      try {
        await execAsync('docker network inspect e2e-test-network', { timeout: 3000 });
        networkExists = true;
      } catch { /* not found */ }

      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            services: { frontend, backend, vcIssuer, vcVerifier, goTrustAllow },
            tunnels: tunnel || 'not active (.env.tunnel not found)',
            android: android || 'not configured (.env.android not found)',
            network: { 'e2e-test-network': networkExists ? 'exists' : 'MISSING — run devenv_setup first' },
            projectRoot: PROJECT_ROOT,
          }, null, 2),
        }],
      };
    }
  );

  // ── Bootstrap / setup ─────────────────────────────────────────────────────

  server.tool(
    'devenv_setup',
    'Clone all required sibling repos and install npm dependencies. ' +
    'This is the one-time bootstrap step equivalent to running make setup && make install. ' +
    'Safe to run repeatedly — already-present repos are skipped.',
    {},
    async () => {
      const setup = await runMake('setup');
      if (setup.exitCode !== 0) return makeResult(setup, 'make setup');
      const install = await runMake('install');
      return makeResult(install, 'make setup && make install');
    }
  );

  // ── Start stack ───────────────────────────────────────────────────────────

  server.tool(
    'devenv_up',
    'Start (or rebuild and restart) the dev stack. Equivalent to make up [OPTIONS]. ' +
    'All parameters are optional — omit them to use defaults. ' +
    'The stack automatically creates e2e-test-network if needed.',
    {
      pdp: z.enum(['allow', 'whitelist', 'deny', 'mock']).optional()
        .describe('Trust PDP mode (default: allow)'),
      vc: z.boolean().optional()
        .describe('Enable VC services: issuer, verifier, apigw, registry (default: false)'),
      transport: z.enum(['wmp', 'http']).optional()
        .describe('Transport protocol override (default: WebSocket)'),
      conformance: z.boolean().optional()
        .describe('Enable OpenID Conformance Suite (implies VC + allow + http)'),
      r2ps: z.boolean().optional()
        .describe('Enable R2PS service with SoftHSM2'),
      domain: z.string().optional()
        .describe('Custom domain for mobile device access (e.g. myhost.local)'),
      golden: z.string().optional()
        .describe('Use pre-built golden release images (pass "yes" or a release name)'),
      rebuild: z.boolean().optional()
        .describe('Force rebuild all images without cache'),
    },
    async ({ pdp, vc, transport, conformance, r2ps, domain, golden, rebuild }) => {
      const vars = {};
      if (pdp) vars.PDP = pdp;
      if (vc) vars.VC = 'yes';
      if (transport) vars.TRANSPORT = transport;
      if (conformance) vars.CONFORMANCE = 'yes';
      if (r2ps) vars.R2PS = 'yes';
      if (domain) vars.DOMAIN = domain;
      if (golden) vars.GOLDEN = golden;
      if (rebuild) vars.REBUILD = 'yes';
      return makeResult(await runMake('up', vars, { timeout: 600_000 }), 'make up');
    }
  );

  // ── Stop stack ────────────────────────────────────────────────────────────

  server.tool(
    'devenv_down',
    'Stop all running containers. Equivalent to make down.',
    {},
    async () => makeResult(await runMake('down'), 'make down')
  );

  // ── Update repos ──────────────────────────────────────────────────────────

  server.tool(
    'devenv_update',
    'Force-update all sibling repositories to their default upstream branches. ' +
    'Equivalent to make update.',
    {},
    async () => makeResult(await runMake('update', {}, { timeout: 120_000 }), 'make update')
  );

  // ── Logs ──────────────────────────────────────────────────────────────────

  server.tool(
    'devenv_logs',
    'Get recent log output from a running container.',
    {
      service: z.string().describe(
        'Container name suffix or full name. Examples: wallet-backend, wallet-frontend, ' +
        'vc-issuer, vc-apigw, go-trust-allow. The -e2e-test or -e2e suffix is added automatically if needed.'
      ),
      lines: z.number().optional().describe('Number of log lines to return (default: 100)'),
    },
    async ({ service, lines }) => {
      const n = lines || 100;
      // Try the name as-is first, then with -e2e-test suffix, then -e2e suffix
      for (const name of [service, `${service}-e2e-test`, `${service}-e2e`]) {
        try {
          const { stdout, stderr } = await execAsync(
            `docker logs --tail ${n} ${name} 2>&1`,
            { timeout: 10_000 }
          );
          return { content: [{ type: 'text', text: `## ${name}\n\n${stdout || stderr}` }] };
        } catch { /* try next */ }
      }
      return {
        content: [{ type: 'text', text: `Container '${service}' not found. Running containers:\n` +
          (await execAsync('docker ps --format "{{.Names}}"').then(r => r.stdout).catch(() => '(error)'))
        }],
        isError: true,
      };
    }
  );

  // ── PKI ───────────────────────────────────────────────────────────────────

  server.tool(
    'devenv_pki',
    'Regenerate PKI (signing keys and certificates). Equivalent to make pki. ' +
    'Required when running VC=yes for the first time or after a clean.',
    {},
    async () => makeResult(await runMake('pki', {}, { timeout: 30_000 }), 'make pki')
  );

  // ── Clean ─────────────────────────────────────────────────────────────────

  server.tool(
    'devenv_clean',
    'Remove all containers, volumes, and build cache. Equivalent to make clean. ' +
    'WARNING: destroys all local data including MongoDB storage for VC services.',
    {},
    async () => makeResult(await runMake('clean', {}, { timeout: 120_000 }), 'make clean')
  );

  // ── Cloudflare tunnels ────────────────────────────────────────────────────

  server.tool(
    'devenv_tunnel_start',
    'Start Cloudflare quick tunnels for frontend, backend, and engine. ' +
    'No Cloudflare account needed — uses trycloudflare.com. ' +
    'Equivalent to make tunnel. URLs are written to .env.tunnel.',
    {},
    async () => makeResult(await runMake('tunnel', {}, { timeout: 60_000 }), 'make tunnel')
  );

  server.tool(
    'devenv_tunnel_stop',
    'Stop all Cloudflare tunnels and clean up .env.tunnel. Equivalent to make tunnel-stop.',
    {},
    async () => makeResult(await runMake('tunnel-stop'), 'make tunnel-stop')
  );

  server.tool(
    'devenv_tunnel_status',
    'Show active Cloudflare tunnel URLs and process status. Equivalent to make tunnel-status.',
    {},
    async () => {
      const result = await runMake('tunnel-status');
      const tunnel = readEnvFile('.env.tunnel');
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            makeOutput: result.stdout || result.stderr,
            envTunnel: tunnel || 'not active',
          }, null, 2),
        }],
      };
    }
  );

  server.tool(
    'devenv_restart_with_tunnels',
    'Restart the dev stack using active Cloudflare tunnel URLs. ' +
    'Equivalent to make restart-with-tunnels. ' +
    'Also re-runs make android-setup to refresh the ADB compat flag for the new tunnel domain. ' +
    'Requires tunnels to be active (devenv_tunnel_start first).',
    {},
    async () => makeResult(
      await runMake('restart-with-tunnels', {}, { timeout: 300_000 }),
      'make restart-with-tunnels'
    )
  );

  // ── Android setup ─────────────────────────────────────────────────────────

  server.tool(
    'devenv_android_setup',
    'Configure the dev environment for Android SDK testing. ' +
    'Extracts the APK key hash from ~/.android/debug.keystore, ' +
    'generates .well-known/assetlinks.json, writes .env.android, ' +
    'and enables DEVELOPMENT_PASSKEY_REGISTRATION on connected ADB device. ' +
    'Equivalent to make android-setup [SDK_PACKAGE=<pkg>].',
    {
      sdkPackage: z.string().optional()
        .describe('Android app package name (default: org.sirosfoundation.sdk.sample)'),
      fingerprint: z.string().optional()
        .describe('Provide SHA256 fingerprint directly instead of reading from keystore (XX:YY:...:ZZ format)'),
    },
    async ({ sdkPackage, fingerprint }) => {
      const vars = {};
      if (sdkPackage) vars.SDK_PACKAGE = sdkPackage;
      const result = await runMake('android-setup', vars, { timeout: 30_000 });
      const android = readEnvFile('.env.android');
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            makeOutput: result.stdout || result.stderr,
            exitCode: result.exitCode,
            envAndroid: android || 'not written (fingerprint extraction may have failed)',
          }, null, 2),
        }],
        isError: result.exitCode !== 0,
      };
    }
  );

  // ── Install tools ─────────────────────────────────────────────────────────

  server.tool(
    'devenv_check_prerequisites',
    'Check which required tools are installed and provide install instructions for any that are missing. ' +
    'Checks: docker, docker compose, node, npm, git, cloudflared, adb, keytool.',
    {},
    async () => {
      const tools = [
        { cmd: 'docker --version', name: 'docker' },
        { cmd: 'docker compose version', name: 'docker compose' },
        { cmd: 'node --version', name: 'node' },
        { cmd: 'npm --version', name: 'npm' },
        { cmd: 'git --version', name: 'git' },
        { cmd: 'cloudflared --version', name: 'cloudflared' },
        { cmd: 'adb --version', name: 'adb (Android SDK)' },
        { cmd: 'keytool -help 2>&1 | head -1', name: 'keytool (JDK)' },
        { cmd: 'curl --version | head -1', name: 'curl' },
      ];

      const results = await Promise.all(tools.map(async ({ cmd, name }) => {
        try {
          const { stdout } = await execAsync(cmd, { timeout: 5000 });
          return { name, installed: true, version: stdout.trim().split('\n')[0] };
        } catch {
          return { name, installed: false };
        }
      }));

      const missing = results.filter(r => !r.installed).map(r => r.name);
      const installHints = [];
      if (missing.some(n => n === 'cloudflared')) {
        installHints.push('cloudflared: brew install cloudflared  OR  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared');
      }
      if (missing.some(n => n.includes('adb'))) {
        installHints.push('adb: Install Android Studio or android-tools-adb (apt), then add platform-tools to PATH');
      }
      if (missing.some(n => n.includes('keytool'))) {
        installHints.push('keytool: Install a JDK — apt install default-jdk  OR  brew install openjdk');
      }

      return {
        content: [{
          type: 'text',
          text: JSON.stringify({ tools: results, missing, installHints }, null, 2),
        }],
      };
    }
  );

  // ── Make help ─────────────────────────────────────────────────────────────

  server.tool(
    'devenv_make_help',
    'Show all available make targets and their descriptions.',
    {},
    async () => {
      const { stdout, stderr } = await execAsync('make --no-print-directory help', {
        cwd: PROJECT_ROOT,
        timeout: 10_000,
        env: { ...process.env, FORCE_COLOR: '0' },
      }).catch(err => ({ stdout: err.stdout || '', stderr: err.message }));
      return { content: [{ type: 'text', text: stdout || stderr }] };
    }
  );

  // ── Arbitrary make target ─────────────────────────────────────────────────

  server.tool(
    'devenv_make',
    'Run any make target with optional variables. Use devenv_make_help to see available targets.',
    {
      target: z.string().describe('The make target to run (e.g. status, pki, update, r2ps-setup)'),
      vars: z.record(z.string()).optional()
        .describe('Key=value pairs to pass to make (e.g. { "VC": "yes", "PDP": "deny" })'),
      timeoutSeconds: z.number().optional()
        .describe('Timeout in seconds (default: 120, max: 600)'),
    },
    async ({ target, vars, timeoutSeconds }) => {
      const timeout = Math.min((timeoutSeconds || 120) * 1000, 600_000);
      return makeResult(
        await runMake(target, vars || {}, { timeout }),
        `make ${target}`
      );
    }
  );
}
