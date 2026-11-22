/**
 * Mock Seller - issues a WE BUILD eReceipt after a payment.
 *
 * PA4 makes the Seller the eReceipt issuer: rb-e-receipt section 1 has an
 * eReceipt "issued by a merchant (or its Payment Service Provider) to the
 * customer's EUDI Wallet after a payment". Nothing in this stack played that
 * role - every credential came from a document pre-seeded in the datastore,
 * which is a register lookup, and a receipt is not one. This is the missing
 * half of sirosfoundation/siros-id-stack#29.
 *
 * The flow, which is the point of the service:
 *
 *   1. A shopper picks items at a till and pays.
 *   2. The merchant builds the receipt FROM THAT PURCHASE - totals, VAT
 *      breakdown and line items computed at the moment of payment, not read
 *      from a fixture. The credential did not exist a second earlier.
 *   3. The merchant hands the wallet a pre-authorized credential offer, so
 *      the shopper needs no account with the issuer and no prior session.
 *
 * Usage:
 *   npx ts-node mocks/seller/index.ts
 *
 * Environment variables:
 *   PORT              - server port (default: 9012)
 *   APIGW_CONFIG      - rendered vc-apigw.yaml (default: /apigw/vc-apigw.yaml)
 *   APIGW_KEY         - admin API signing key (default: /secrets/apiAuthKey.pem)
 *   APIGW_URL         - override the apigw base URL from that config
 *   AUTHENTIC_SOURCE  - datastore authentic_source (default: mini-oidc)
 *   SCOPE             - credential scope to issue (default: ereceipt)
 *   MERCHANT_*        - see catalog.ts
 */

import * as http from 'http';
import { Apigw, readConfig } from './apigw';
import { BUYERS, PRODUCTS } from './catalog';
import { checkoutPage, receiptPage } from './page';
import { buildReceipt, CartLine } from './receipt';

const PORT = parseInt(process.env.PORT || '9012', 10);
const APIGW_CONFIG = process.env.APIGW_CONFIG || '/apigw/vc-apigw.yaml';
const APIGW_KEY = process.env.APIGW_KEY || '/secrets/apiAuthKey.pem';
// The datastore matches a document's authentic_source against the scope's,
// so this has to be the one the chart configured for `ereceipt`. A merchant
// would be its own authentic source in a real deployment; see README.md.
const AUTHENTIC_SOURCE = process.env.AUTHENTIC_SOURCE || 'mini-oidc';
const SCOPE = process.env.SCOPE || 'ereceipt';
// Where the browser-based wallet lives, and which tenant to hand the offer
// to. Empty disables the button - a till on a phone has no use for it.
const WALLET_URL = process.env.WALLET_URL ?? 'http://localhost:3000';
const WALLET_TENANT = process.env.WALLET_TENANT || 'default';

let apigw: Apigw | null = null;
let apigwError: string | null = null;
try {
  apigw = new Apigw(readConfig(APIGW_CONFIG, process.env.APIGW_URL), APIGW_KEY);
} catch (err) {
  // Stay up and report the reason on /health and on a purchase attempt.
  // Exiting here would make the container crash-loop with the cause only in
  // a log nobody is tailing yet.
  apigwError = err instanceof Error ? err.message : String(err);
  console.error('[mock-seller] apigw unavailable:', apigwError);
}

/** The web wallet has no custom-scheme handler, so hand it the offer on the
 *  route its own QR scanner redirects to: /id/<tenant>/cb?credential_offer=...
 *  (wallet-frontend QRCodeScanner.jsx). Same offer, no camera. */
function browserWalletLink(offerUrl: string): string | null {
  if (!WALLET_URL) {
    return null;
  }
  const query = offerUrl.includes('?') ? offerUrl.slice(offerUrl.indexOf('?')) : '';
  if (!query) {
    return null;
  }
  return `${WALLET_URL.replace(/\/$/, '')}/id/${WALLET_TENANT}/cb${query}`;
}


function send(res: http.ServerResponse, status: number, type: string, body: string) {
  res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store' });
  res.end(body);
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > 64 * 1024) {
        reject(new Error('request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

/** A cart from the till form, or from a JSON body for scripted use. */
function parseCart(fields: URLSearchParams | Record<string, unknown>): CartLine[] {
  const lines: CartLine[] = [];
  for (const product of PRODUCTS) {
    const raw = fields instanceof URLSearchParams
      ? fields.get(`qty_${product.id}`)
      : (fields as Record<string, unknown>)[product.id];
    const quantity = Number(raw ?? 0);
    if (!Number.isInteger(quantity) || quantity < 0 || quantity > 99) {
      throw new Error(`quantity for ${product.id} must be a whole number from 0 to 99`);
    }
    if (quantity > 0) {
      lines.push({ product, quantity });
    }
  }
  return lines;
}

async function takePayment(lines: CartLine[], buyerId: string) {
  if (!apigw) {
    throw new Error(`the issuer is not reachable: ${apigwError}`);
  }
  const buyer = BUYERS.find((b) => b.id === buyerId);
  if (!buyer) {
    throw new Error(`unknown buyer ${buyerId}`);
  }

  const receipt = buildReceipt(lines);

  // The document the issuer will build the credential from. Keyed to the
  // buyer through identity_mapping_ids, which is also what the
  // PID-authenticated flow resolves the holder by - so this receipt is
  // reachable both ways, not only through the offer below.
  await apigw.putDocument(buyer.id, {
    meta: {
      authentic_source: AUTHENTIC_SOURCE,
      scope: SCOPE,
      document_id: receipt.documentId,
      created_at: new Date().toISOString(),
    },
    identity_mapping_ids: [buyer.id],
    document_data: receipt.payload,
  });

  const offerUrl = await apigw.preauthOffer(AUTHENTIC_SOURCE, SCOPE, receipt.documentId);
  console.log(`[mock-seller] issued ${receipt.documentId} to ${buyer.id} ` +
              `(${receipt.totalIncVat} minor units)`);
  return { receipt, buyer, offerUrl };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://localhost:${PORT}`);

  try {
    // HEAD as well as GET: `wget --spider`, which the compose healthcheck
    // runs, issues a HEAD and reads a 404 as a failing service.
    if ((req.method === 'GET' || req.method === 'HEAD') && url.pathname === '/health') {
      const ok = apigw !== null;
      return send(res, ok ? 200 : 503, 'application/json',
        JSON.stringify({ status: ok ? 'ok' : 'degraded', scope: SCOPE, error: apigwError }));
    }

    if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
      return send(res, 200, 'text/html; charset=utf-8', checkoutPage());
    }

    if (req.method === 'POST' && url.pathname === '/pay') {
      const body = await readBody(req);
      const json = (req.headers['content-type'] || '').includes('application/json');
      const fields = json ? JSON.parse(body) : new URLSearchParams(body);
      const buyerId = json ? fields.buyer : (fields as URLSearchParams).get('buyer');
      const lines = parseCart(json ? (fields.cart || {}) : fields);
      if (lines.length === 0) {
        throw new Error('nothing in the cart');
      }

      const { receipt, buyer, offerUrl } = await takePayment(lines, String(buyerId));
      if (json) {
        return send(res, 200, 'application/json', JSON.stringify({
          receipt_number: receipt.payload.receipt_number,
          document_id: receipt.documentId,
          total_price_inc_vat: receipt.totalIncVat,
          buyer: buyer.id,
          credential_offer_url: offerUrl,
          browser_wallet_url: browserWalletLink(offerUrl),
        }));
      }
      return send(res, 200, 'text/html; charset=utf-8', receiptPage(
        String(receipt.payload.receipt_number), receipt.totalIncVat, offerUrl,
        buyer.person, browserWalletLink(offerUrl)));
    }

    return send(res, 404, 'text/plain; charset=utf-8', 'not found\n');
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error('[mock-seller]', message);
    return send(res, 400, 'text/plain; charset=utf-8', `${message}\n`);
  }
});

server.listen(PORT, () => {
  console.log(`[mock-seller] till open on http://localhost:${PORT}`);
  console.log(`[mock-seller] issuing scope "${SCOPE}" as "${AUTHENTIC_SOURCE}"`);
});
