/**
 * The vc-apigw admin API, and the token it needs.
 *
 * The seller does not sign credentials itself. It writes the receipt into the
 * issuer's datastore and asks for a pre-authorized credential offer, so the
 * attestation the wallet receives is signed by the real issuer with the real
 * key and is verifiable against the real trust chain. See README.md,
 * "What this is not", for where that departs from a deployed merchant.
 */

import * as crypto from 'crypto';
import * as fs from 'fs';
import * as yaml from 'js-yaml';

export interface ApigwConfig {
  url: string;
  issuer: string;
  audience: string;
  subject: string;
}

/** Backdated by this much: jwx validates `iat <= now` with no leeway, and a
 *  caller whose clock runs a second ahead would be rejected with
 *  "iat not satisfied". Mirrors scripts/api_auth.py's IAT_LEEWAY. */
const IAT_LEEWAY = 30;
const TOKEN_TTL = 300;

function b64url(buf: Buffer | string): string {
  return Buffer.from(buf).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** RFC 7638 thumbprint, the same kid scripts/api_auth.py derives, so the
 *  JWKS the apigw already serves matches without any shared state. */
function kid(key: crypto.KeyObject): string {
  const jwk = key.export({ format: 'jwk' }) as Record<string, string>;
  const core = { crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y };
  return b64url(crypto.createHash('sha256').update(JSON.stringify(core)).digest());
}

/** Read the admin auth parameters out of the rendered apigw config rather
 *  than restating them: they are generated per target, and a copy here would
 *  be wrong the first time anyone changed the tenant or the port. */
export function readConfig(configPath: string, urlOverride?: string): ApigwConfig {
  const config = yaml.load(fs.readFileSync(configPath, 'utf8')) as any;
  const apiServer = config.apigw.api_server;
  const jwks = apiServer.api_auth.jwks;

  // The SPOCP rule names the one subject allowed on /api/v1/*, e.g.
  //   (vc (service *)(method *)(path /api/v1/*)(subject admin@default) ...)
  const rules: string[] = apiServer.api_auth.rules || [];
  const subject = rules
    .map((rule) => /\(subject\s+([^)]+)\)/.exec(rule)?.[1]?.trim())
    .find((s): s is string => !!s);
  if (!subject) {
    throw new Error(`no (subject ...) in the apigw api_auth rules of ${configPath}`);
  }

  return {
    url: urlOverride || config.apigw.public_url,
    issuer: jwks.issuer,
    audience: jwks.audience,
    subject,
  };
}

export class Apigw {
  private key: crypto.KeyObject;

  constructor(private config: ApigwConfig, keyPath: string) {
    this.key = crypto.createPrivateKey(fs.readFileSync(keyPath, 'utf8'));
  }

  /** A short-lived ES256 admin token. `eppn` is what vc's SPOCP layer reads
   *  as the subject (extractSPOCPSubject: eppn, then email); `sub` carries
   *  the same value for anything else that looks. */
  private token(): string {
    const now = Math.floor(Date.now() / 1000) - IAT_LEEWAY;
    const header = { alg: 'ES256', typ: 'JWT', kid: kid(crypto.createPublicKey(this.key)) };
    const claims = {
      iss: this.config.issuer,
      aud: this.config.audience,
      sub: this.config.subject,
      eppn: this.config.subject,
      iat: now,
      exp: now + TOKEN_TTL,
      jti: crypto.randomUUID(),
    };
    const input = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(claims))}`;
    // dsaEncoding 'ieee-p1363' is the raw r||s JWS wants; node's default is DER.
    const sig = crypto.sign('sha256', Buffer.from(input),
      { key: this.key, dsaEncoding: 'ieee-p1363' });
    return `${input}.${b64url(sig)}`;
  }

  private async request(method: string, path: string, body?: unknown): Promise<any> {
    const response = await fetch(this.config.url.replace(/\/$/, '') + path, {
      method,
      headers: {
        Authorization: `Bearer ${this.token()}`,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`${method} ${path} -> ${response.status}: ${text.slice(0, 400)}`);
    }
    return text ? JSON.parse(text) : {};
  }

  /** Write one document. The bulk endpoint is keyed by holder, so one call
   *  carries at most one document per holder - fine, a receipt has one buyer. */
  async putDocument(holder: string, document: unknown): Promise<void> {
    await this.request('POST', '/api/v1/datastore/bulk', { documents: { [holder]: document } });
  }

  /** Mint a pre-authorized credential offer for a document we just wrote.
   *  The offer carries its own code, so the wallet that scans it needs no
   *  session with the issuer - which is what lets a shopper walk up with a
   *  phone and no prior login. The code expires 5 minutes after minting. */
  async preauthOffer(authenticSource: string, scope: string, documentId: string): Promise<string> {
    const reply = await this.request('POST', '/api/v1/datastore/preauth_offer', {
      authentic_source: authenticSource,
      scope,
      document_id: documentId,
    });
    return reply.credential_offer_url;
  }
}
