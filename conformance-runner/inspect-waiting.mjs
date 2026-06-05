/**
 * Diagnostic script: create a single issuer test module with unique alias,
 * wait for WAITING state, and dump all exposed values.
 */
import { readFileSync } from 'fs';

const CONFORMANCE_URL = 'https://conformance-suite-nginx:8443/';
const ISSUER_URL = process.env.ISSUER_CONFORMANCE_URL || 'https://vc-proxy:8443';

let config = readFileSync('/app/vci-issuer-config.json', 'utf-8');
config = config.replaceAll('${ISSUER_DISCOVERY_URL}', `${ISSUER_URL}/.well-known/openid-credential-issuer`);
config = config.replaceAll('${ISSUER_RESOURCE_URL}', ISSUER_URL);
config = config.replaceAll('${ISSUER_CREDENTIAL_ISSUER_URL}', ISSUER_URL);

const parsed = JSON.parse(config);
parsed.alias = 'inspect-' + Date.now();
config = JSON.stringify(parsed);

const variant = JSON.stringify({
  credential_format: 'sd_jwt_vc',
  sender_constrain: 'dpop',
  client_auth_type: 'private_key_jwt',
  vci_grant_type: 'pre_authorization_code',
  fapi_profile: 'vci',
  fapi_request_method: 'unsigned',
  fapi_response_mode: 'plain_response',
  openid: 'plain_oauth',
  authorization_request_type: 'simple',
  vci_authorization_code_flow_variant: 'issuer_initiated',
  vci_credential_encryption: 'plain',
});

async function main() {
  // Create plan
  const planUrl = `${CONFORMANCE_URL}api/plan?planName=oid4vci-1_0-issuer-test-plan&variant=${encodeURIComponent(variant)}`;
  const planRes = await fetch(planUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: config });
  const plan = await planRes.json();
  console.log('planId:', plan.id);

  // Create happy-flow module
  const modUrl = `${CONFORMANCE_URL}api/runner?test=oid4vci-1_0-issuer-happy-flow&plan=${plan.id}`;
  const modRes = await fetch(modUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  const mod = await modRes.json();
  console.log('moduleId:', mod.id);

  // Poll until WAITING
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const infoRes = await fetch(`${CONFORMANCE_URL}api/info/${mod.id}`);
    const info = await infoRes.json();
    console.log(`poll ${i}: status=${info.status}`);

    if (info.status === 'WAITING') {
      console.log('\n=== MODULE INFO (WAITING) ===');
      const skip = new Set(['config', '_id']);
      for (const [k, v] of Object.entries(info)) {
        if (skip.has(k)) continue;
        const val = typeof v === 'string' ? v : JSON.stringify(v);
        console.log(`  ${k}: ${val.slice(0, 500)}`);
      }

      // Check log entries for exposed values
      console.log('\n=== LOG ENTRIES ===');
      const logRes = await fetch(`${CONFORMANCE_URL}api/log/${mod.id}`);
      const logs = await logRes.json();
      for (const entry of logs) {
        const src = entry.src || '';
        const msg = entry.msg || '';
        const result = entry.result || 'INFO';
        // Show everything near the end
        const skipKeys = new Set(['_id', 'src', 'msg', 'result', 'time', 'requirements', 'testId', 'testOwner']);
        const extras = Object.entries(entry).filter(([k]) => !skipKeys.has(k));
        if (extras.length > 0 || src.includes('Wait') || src.includes('Offer') || result !== 'INFO') {
          console.log(`  [${result}] ${src}: ${msg.slice(0, 200)}`);
          for (const [ek, ev] of extras) {
            const val = typeof ev === 'string' ? ev : JSON.stringify(ev);
            console.log(`    ${ek} = ${val.slice(0, 500)}`);
          }
        }
      }
      break;
    }

    if (info.status === 'FINISHED' || info.status === 'INTERRUPTED') {
      console.log('Terminal:', info.status, info.result);
      // Show failure details
      const logRes = await fetch(`${CONFORMANCE_URL}api/log/${mod.id}`);
      const logs = await logRes.json();
      for (const entry of logs) {
        if (entry.result === 'FAILURE' || entry.result === 'INTERRUPTED') {
          console.log(`  [${entry.result}] ${entry.src}: ${(entry.msg || '').slice(0, 300)}`);
        }
      }
      break;
    }
  }
}

main().catch(console.error);
