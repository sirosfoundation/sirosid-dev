/**
 * What this merchant sells, and who can buy.
 *
 * The buyers are the demo companies sirosid-dev already uses, so a receipt
 * minted here belongs to the same holder as that company's EBWOID, EUCC and
 * VAT ID (fixtures/vc-bootstrapping). That matters for the PID-authenticated
 * flow: the holder is resolved through identity_mappings.json, and a buyer
 * invented here would fail with "no documents".
 */

export interface Product {
  id: string;
  name: string;
  /** Minor units, inclusive of VAT - the unit the eReceipt schema uses. */
  unitPriceIncVat: number;
  /** Whole percent, as the schema's vat_rate string. */
  vatRate: number;
  category: string;
  department: string;
}

export interface Buyer {
  /** The holder id, matching fixtures/vc-bootstrapping/identity_mappings.json. */
  id: string;
  person: string;
  company: string;
}

export const MERCHANT = {
  name: process.env.MERCHANT_NAME || 'Kafé Bokbindaren AB',
  companyId: process.env.MERCHANT_COMPANY_ID || 'SE5569123456',
  branchId: process.env.MERCHANT_BRANCH_ID || 'BR-001',
  posId: process.env.MERCHANT_POS_ID || 'POS-03',
  streetAddress: process.env.MERCHANT_STREET || 'Drottninggatan 71',
  city: process.env.MERCHANT_CITY || 'Stockholm',
  zipCode: process.env.MERCHANT_ZIP || '111 36',
  country: process.env.MERCHANT_COUNTRY || 'SE',
  currency: process.env.MERCHANT_CURRENCY || 'EUR',
};

export const PRODUCTS: Product[] = [
  { id: 'coffee', name: 'Coffee', unitPriceIncVat: 150, vatRate: 25, category: 'F&B', department: 'Café' },
  { id: 'sandwich', name: 'Sandwich', unitPriceIncVat: 650, vatRate: 25, category: 'F&B', department: 'Café' },
  { id: 'lunch', name: 'Lunch of the day', unitPriceIncVat: 1450, vatRate: 25, category: 'F&B', department: 'Restaurant' },
  { id: 'notebook', name: 'Notebook', unitPriceIncVat: 1200, vatRate: 25, category: 'Retail', department: 'Shop' },
  { id: 'beans', name: 'Coffee beans, 1 kg', unitPriceIncVat: 2800, vatRate: 12, category: 'Retail', department: 'Shop' },
];

export const BUYERS: Buyer[] = [
  { id: 'erik-010', person: 'Erik Lindström', company: 'Nordic Tech Solutions AB' },
  { id: 'maria-011', person: 'Maria Schneider', company: 'Grünberg Consulting GmbH' },
  { id: 'sophie-013', person: 'Sophie Dubois', company: 'Transport Dubois SARL' },
];
