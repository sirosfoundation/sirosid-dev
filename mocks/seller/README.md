# Mock Seller — eReceipt issuance

A merchant till. Pick items, take a payment, hand the shopper's wallet a
WE BUILD eReceipt for that purchase.

```
http://localhost:9012
```

## Why this exists

PA4 makes the **Seller** the eReceipt issuer. rb-e-receipt §1: an eReceipt
"is issued by a merchant (or its Payment Service Provider) to the customer's
EUDI Wallet **after a payment**".

Every other credential in this stack comes from a document pre-seeded in the
datastore and released after the holder presents a PID. That models a register
lookup — an authentic source holds a record about a registered entity and
attests to it — which is right for EBWOID, EUCC and VAT ID. A receipt is not
one. It did not exist a second before the payment, and no register holds it.

So the demo could show a wallet holding, rendering and presenting an eReceipt,
but nothing showed how one gets there. That gap is
[sirosfoundation/siros-id-stack#29][29], and this is the missing half.

The WE BUILD consortium has no shared sandbox for this either: the WP4
Interoperability Test Bed's `tests/usecases/` directory is an empty
placeholder, and the PA4 onboarding guide's credential chain (stages 8.1–8.8)
lists test issuers for PID, EBWOID and EUCC but none for eReceipt — because in
the pilot the issuer is whichever partner plays Seller.

[29]: https://github.com/sirosfoundation/siros-id-stack/issues/29

## Using it

**At the till.** Open `http://localhost:9012`, set quantities, choose a buyer,
press *Take payment and issue eReceipt*. You get a credential offer; open it in
the wallet or scan it from another device.

**From a script**, for tests:

```bash
curl -s -X POST http://localhost:9012/pay \
  -H 'Content-Type: application/json' \
  --data-binary '{"buyer":"erik-010","cart":{"coffee":3,"lunch":2,"beans":1}}'
```

```json
{
  "receipt_number": "R-2026-11-22-142334",
  "document_id": "document_id_ereceipt_20261122142334",
  "total_price_inc_vat": 6150,
  "buyer": "erik-010",
  "credential_offer_url": "openid-credential-offer://?credential_offer=..."
}
```

The buyers are the demo companies the rest of the stack already uses
(`erik-010`, `maria-011`, `sophie-013`), so a receipt minted here belongs to
the same holder as that company's EBWOID, EUCC and VAT ID. A buyer invented
here would have no identity mapping and would fail the PID-authenticated flow
with "no documents".

| Endpoint | |
| --- | --- |
| `GET /` | the till |
| `POST /pay` | form or JSON; mints the receipt and returns an offer |
| `GET /health` | `degraded` with a reason when the issuer is unreachable |

## How it works

1. **The receipt is computed from the purchase.** Line totals, the per-rate
   VAT breakdown and the receipt totals are derived from the cart at the
   moment of payment — not read from a fixture. Amounts are in minor units
   throughout, as the catalogue's own sample payload is.
2. **It is written to the issuer's datastore** through vc-apigw's admin API,
   keyed to the buyer through `identity_mapping_ids`, so the receipt is
   reachable through the PID-authenticated flow as well as the offer.
3. **A pre-authorized credential offer is minted for it.** The offer carries
   its own code, so the shopper needs no account with the issuer and no prior
   session — which is the whole point of a till. The code expires after five
   minutes.

The credential the wallet receives is therefore signed by the real issuer with
the real key, and verifies against the real trust chain. `receipt.ts` holds
the payload construction; it validates against the catalogue's
`e-receipt-sd-jwt.json`.

## What this is not

- **The seller does not hold issuer keys.** A deployed merchant would run its
  own OpenID4VCI issuer, or its PSP would. Here the merchant writes a document
  and asks the stack's issuer to sign it. Building a second issuer would have
  produced a credential signed by a key nothing trusts, which proves less, not
  more — but it does mean this exercises the *shape* of merchant issuance, not
  a merchant's own trust posture.
- **`authentic_source` is `mini-oidc`, not the merchant.** The datastore
  matches a document's authentic source against the scope's, and the chart
  configures `ereceipt` with the stack's. A merchant would be its own.
- **`verifications[]` carries a placeholder key.** There is no real POS
  signature over the transaction. rb-e-receipt marks the whole array
  not-selectively-disclosable, so it travels in plain text and a verifier can
  see exactly what it is.
- **No payment happens.** "Take payment" is a button. The card attributes are
  the catalogue sample's.
- **`merchant.name` cannot be rendered on a card** until
  [registry-cli#63][63] lands: `merchant` is a `$ref`, and the generator does
  not resolve one, so no claim exists for it.

[63]: https://github.com/sirosfoundation/registry-cli/issues/63

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `PORT` | `9012` | |
| `APIGW_CONFIG` | `/apigw/vc-apigw.yaml` | rendered config; the admin token's issuer, audience and subject are read from it rather than restated |
| `APIGW_KEY` | `/secrets/apiAuthKey.pem` | admin API signing key |
| `APIGW_URL` | from the config | the compose service sets `http://vc-apigw:9003` |
| `AUTHENTIC_SOURCE` | `mini-oidc` | must match the scope's |
| `SCOPE` | `ereceipt` | |
| `MERCHANT_*` | Kafé Bokbindaren AB | name, company id, branch, till, address, currency — see `catalog.ts` |
