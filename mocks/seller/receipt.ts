/**
 * Build an eReceipt payload from a cart, per rb-e-receipt and
 * data-schemas/sd-jwt/e-receipt-sd-jwt.json in the WE BUILD catalogue.
 *
 * Money is in MINOR UNITS throughout, as the catalogue's own sample payload
 * is: 4250 is 42.50. Rates and quantities are strings; amounts are numbers.
 *
 * Totals are computed from the line items rather than taken as input, since
 * a receipt whose totals disagree with its lines is exactly the defect a
 * verifier is there to catch. VAT is derived per rate so vats[] carries one
 * entry per rate present, which is what the rulebook's 3.2.5 example shows.
 */

import { MERCHANT, Product } from './catalog';

export interface CartLine {
  product: Product;
  quantity: number;
}

export interface Receipt {
  documentId: string;
  totalIncVat: number;
  payload: Record<string, unknown>;
}

/** Excluding-VAT amount for an inclusive total at `rate` percent, rounded to
 *  the minor unit. The VAT amount is then the remainder, so the two always
 *  add back to the inclusive total - deriving both independently is how a
 *  receipt ends up one cent out. */
function splitVat(amountIncVat: number, rate: number): { exc: number; vat: number } {
  const exc = Math.round(amountIncVat / (1 + rate / 100));
  return { exc, vat: amountIncVat - exc };
}

export function buildReceipt(lines: CartLine[], now = new Date()): Receipt {
  if (lines.length === 0) {
    throw new Error('an eReceipt needs at least one line item');
  }

  const products = lines.map(({ product, quantity }) => {
    const incVat = product.unitPriceIncVat * quantity;
    const { exc, vat } = splitVat(incVat, product.vatRate);
    return {
      name: product.name,
      quantity: String(quantity),
      // UN/ECE Rec 20 code for "one"/piece, as the catalogue sample uses.
      quantity_code: 'H87',
      unit_price_inc_vat: product.unitPriceIncVat,
      total_amount_exc_vat: exc,
      total_amount_inc_vat: incVat,
      vats: [{
        vat_rate: String(product.vatRate),
        vat_amount: vat,
        total_amount_exc_vat: exc,
        total_amount_inc_vat: incVat,
      }],
      categorization: { category: product.category, department: product.department },
    };
  });

  // One vats[] entry per distinct rate, totalled across the lines at it.
  const byRate = new Map<number, { exc: number; vat: number; inc: number }>();
  lines.forEach(({ product, quantity }) => {
    const incVat = product.unitPriceIncVat * quantity;
    const { exc, vat } = splitVat(incVat, product.vatRate);
    const acc = byRate.get(product.vatRate) || { exc: 0, vat: 0, inc: 0 };
    byRate.set(product.vatRate, { exc: acc.exc + exc, vat: acc.vat + vat, inc: acc.inc + incVat });
  });

  const vats = [...byRate.entries()]
    .sort(([a], [b]) => a - b)
    .map(([rate, t]) => ({
      vat_rate: String(rate),
      total_vat_amount: t.vat,
      total_amount_exc_vat: t.exc,
      total_amount_inc_vat: t.inc,
      vat_code: 'S',
    }));

  const totalExcVat = vats.reduce((sum, v) => sum + v.total_amount_exc_vat, 0);
  const totalVat = vats.reduce((sum, v) => sum + v.total_vat_amount, 0);
  const totalIncVat = vats.reduce((sum, v) => sum + v.total_amount_inc_vat, 0);

  const stamp = now.toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
  const receiptNumber = `R-${now.toISOString().slice(0, 10)}-${stamp.slice(8)}`;

  return {
    documentId: `document_id_ereceipt_${stamp}`,
    totalIncVat,
    payload: {
      attestation_legal_category: 'non-qualified-EAA',
      type: 'PURCHASE',
      receipt_number: receiptNumber,
      receipt_timestamp: now.toISOString().replace(/\.\d{3}/, ''),
      currency_iso_code: MERCHANT.currency,
      total_price_exc_vat: totalExcVat,
      total_vat_amount: totalVat,
      total_price_inc_vat: totalIncVat,
      merchant: {
        name: MERCHANT.name,
        company_id: MERCHANT.companyId,
        // $defs/merchant requires branch; only its sub_name is optional.
        branch: { id: MERCHANT.branchId, pos_id: MERCHANT.posId, name: MERCHANT.name },
        address: {
          street_address: MERCHANT.streetAddress,
          city: MERCHANT.city,
          zip_code: MERCHANT.zipCode,
          country: MERCHANT.country,
        },
      },
      products,
      vats,
      payments: [{
        type: 'CARD',
        amount: totalIncVat,
        attributes: { masked_pan: '411111******1111', authorization_code: 'AB12CD' },
      }],
      // $defs/verification: `type` is a closed code list and the object
      // allows no extra properties, so a card payment fits exactly these
      // four fields. The public key is a placeholder - this mock does not
      // produce a real POS signature, and rb-e-receipt marks the whole
      // array MUST NOT be selectively disclosable regardless.
      verifications: [{
        type: 'EMV',
        version: '1.0',
        public_key:
          'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEmock' + 'A'.repeat(48) + '==',
        transaction_id: `TXN-${stamp}`,
      }],
    },
  };
}
