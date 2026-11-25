/** The till. Plain HTML so the service needs no build step beyond tsc. */

import { BUYERS, MERCHANT, PRODUCTS } from './catalog';

const money = (minor: number) => (minor / 100).toFixed(2);

const STYLE = `
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto;
         padding: 0 1rem; }
  h1 { font-size: 1.3rem; margin-bottom: .2rem; }
  .sub { color: #666; margin-top: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  th, td { text-align: left; padding: .45rem .5rem; border-bottom: 1px solid #ddd; }
  td.num, th.num { text-align: right; }
  input[type=number] { width: 4rem; }
  button { font: inherit; padding: .5rem 1rem; cursor: pointer; }
  .offer { word-break: break-all; background: #f4f4f4; padding: .75rem;
           border-radius: 6px; font-family: ui-monospace, monospace; font-size: .8rem; }
  @media (prefers-color-scheme: dark) { .offer { background: #222; } th, td { border-color: #444; } }
`;

export function checkoutPage(): string {
  const rows = PRODUCTS.map((p) => `
    <tr>
      <td>${p.name}</td>
      <td class="num">${money(p.unitPriceIncVat)}</td>
      <td class="num">${p.vatRate}%</td>
      <td class="num"><input type="number" name="qty_${p.id}" min="0" max="99" value="0"></td>
    </tr>`).join('');

  const buyers = BUYERS.map((b) =>
    `<option value="${b.id}">${b.person} — ${b.company}</option>`).join('');

  return `<!doctype html>
<meta charset="utf-8"><title>${MERCHANT.name}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>${STYLE}</style>
<h1>${MERCHANT.name}</h1>
<p class="sub">${MERCHANT.streetAddress}, ${MERCHANT.zipCode} ${MERCHANT.city} ·
   ${MERCHANT.companyId} · till ${MERCHANT.posId}</p>
<form method="post" action="pay">
  <table>
    <tr><th>Item</th><th class="num">Price</th><th class="num">VAT</th><th class="num">Qty</th></tr>
    ${rows}
  </table>
  <p>
    <label>Buyer <select name="buyer">${buyers}</select></label>
  </p>
  <p><button type="submit">Take payment and issue eReceipt</button></p>
</form>
<p class="sub">Paying mints a WE BUILD eReceipt for that buyer and returns a
credential offer their wallet can accept.</p>`;
}

export function receiptPage(
  receiptNumber: string, totalIncVat: number, offerUrl: string, buyer: string,
  browserWalletUrl: string | null,
): string {
  // A phone scans the openid-credential-offer:// deep link. A browser cannot
  // follow a custom scheme, so the web wallet gets the same offer through the
  // route its own QR scanner redirects to.
  const browserButton = browserWalletUrl
    ? `<p><a href="${browserWalletUrl.replace(/&/g, '&amp;')}">
         <button type="button">Add to the wallet in this browser</button></a></p>`
    : '';

  return `<!doctype html>
<meta charset="utf-8"><title>Receipt ${receiptNumber}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>${STYLE}</style>
<h1>Payment taken</h1>
<p class="sub">${receiptNumber} · ${money(totalIncVat)} ${MERCHANT.currency} · ${buyer}</p>
<p>The eReceipt is waiting in a credential offer.</p>
${browserButton}
<p><a href="${offerUrl}"><button type="button">Open in a wallet app</button></a></p>
<p class="sub">Or scan / paste this. The pre-authorized code expires in 5 minutes.</p>
<p class="offer">${offerUrl.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</p>
<p><a href=".">Another purchase</a></p>`;
}
