# Testing nym_auth: the `nymauth` environment

`environments/nymauth.yaml` is a **known starting point** for testing the nym_auth pseudonym flow end to end:
the issuer gives a wallet a `nym_auth` credential (and delivers its secret seed), and a verifier asks the wallet for a
**zero-knowledge pseudonym** (and optionally an age range) without seeing any attribute. It is the `gdc` stack reduced to
what that needs; `diff environments/gdc.yaml environments/nymauth.yaml` shows exactly what is nymauth-specific.

**Status.** The flow below was proven live on `gdc` (a Pixel, the Kotlin sample app): issuance with seed delivery and
notification; same-device presentation of both presets; cross-device presentation by scanning the verifier page's QR in a
browser. **Nobody has deployed `ENV=nymauth` yet**; it reproduces gdc's pins on its own hostnames. (The vc images were rebuilt
from vc `c8c5b1ea` - classified refusals and #840 - after the proven run; the Pixel run on exactly that set is the next step.)

> **Every pinned image except the PDP and the frontend is a local build of UNMERGED pull requests.** Re-pin them to
> released images when the PRs land. What went into each build (tags, digests, base commits, PR heads) is the
> `PIN PROVENANCE` comment block at the top of `images:` in `environments/gdc.yaml`.

Who does what: **Operator** steps need Fly access and a checkout of this repo. **Tester** steps need only the phone, the
sample app and a browser.

---

## Part 1 - Operator: deploy the environment and make a credential offer

### Prerequisites
- `flyctl` logged in to the Fly org (`flyctl auth whoami`), `helm`, `docker`, `python3` with PyYAML, and a checkout of
  this repo **on the machine that deploys**: per-environment state (PKI, secrets, the seed secret) lives in the
  gitignored `fixtures/rendered/fly-nymauth/` and must be kept; a deploy from another checkout generates new ones.
- The images are pinned to gdc's registry namespace (`registry.fly.io/sirosid-gdc-*`); the deploy pulls them, so the Fly
  account must be able to read gdc's images. **Not yet exercised.** If it cannot, push the same images to the new apps'
  registries after the first `fly-up` created the apps, and repin in `environments/nymauth.yaml`.

### Deploy
```sh
flyctl auth docker                 # registry login (expires in minutes: run it right before)
make fly-up ENV=nymauth            # a fresh datastore imports the fixtures on first start: nothing else to run
```
`make fly-up ENV=nymauth` would create, in the org's `arn` region: **11 apps** - `sirosid-nymauth-` + `mongodb`, `mini-oidc`,
`pdp`, `vc-registry`, `vc-issuer`, `vc-apigw`, `vc-verifier`, `wallet-backend`, `wallet-proxy`, `wallet-frontend`,
`env-admin` - with a volume for MongoDB, the secrets (signing key, JWT/admin secrets, the permanent seed secret and the
credential-encryption key), deploy tokens for env-admin, and public `*.fly.dev` hostnames. It generates the environment's
own PKI; the signing certificate carries the verifier's host (`signingCertDnsSan`). `make fly-down ENV=nymauth` removes it.
Do not run these unless you mean to create those resources.

### What lives where
| | URL |
|---|---|
| verifier page (operator UI) | `https://sirosid-nymauth-vc-verifier.fly.dev/` |
| issuer | `https://sirosid-nymauth-vc-apigw.fly.dev` |
| wallet backend / engine (what the sample app uses as backend URL) | `https://sirosid-nymauth-wallet-proxy.fly.dev` |
| PDP (internal only) | `http://sirosid-nymauth-pdp.internal:8080` |

### Mint an offer for alice-001
```sh
make datastore-offer SCOPE=nym_auth DOCUMENT_ID=document_id_nym_auth_alice-001 ENV=nymauth
```
It prints an `openid-credential-offer://...` URL (add `QR=yes` for a terminal QR). **The code is valid for 5 minutes and
single use**: mint it when the phone is in your hand. The offer names the `nym_auth` configuration itself: this environment
has no `<type>_offer` twins, because its vc images carry SUNET/vc#840 (`allow_preauth_offer`) and the chart sets
`issuer.allowPreauthOffer` (a standing rule until #840 is merged to vc main: pin vc images built from a SHA that contains it).

---

## Part 2 - Tester: what you see and do

Use the Kotlin sample app (`org.siros.sdk.sample`). Its signing keys are in `android_apps` of the environment file, so
sign-up works against this environment.

### 1. Sign in
Open the app, point it at the environment's wallet backend (`https://sirosid-nymauth-wallet-proxy.fly.dev`), create or
use a passkey, and sign in. The **Credentials** tab shows **Welcome, <your name>**.

### 2. Allow zkp_req requests (once)
**Settings** tab -> **Request policy**:
- switch **zkp_req requests** ON (it is OFF by default);
- leave **FAKE prover (no real proof)** OFF - the verifier cannot accept a fake proof;
- the section says *"Takes effect the next time the wallet is created: after Disconnect, or an app restart."* Do that now
  (Disconnect and reconnect, or restart the app). **A wallet created before you switched this on does not answer
  zkp_req requests**, and has to be recreated.

### 3. Get the credential
The operator gives you an offer link or QR code (Part 1). Either open the link on the phone, or on **Home** tap
**Add a credential**, scan with **Scan QR Code**, or paste the link under **Or paste a credential offer URI** and tap
**Redeem Offer**. The wallet shows progress ("Reading credential offer...", "Issuer trust verified", "Requesting
credential..."), then the credential appears under **Credentials** as **Nym authentication**. The secret seed is stored
silently and never shown.

### 4. Present it to the verifier
On a computer (or the phone's browser), open `https://sirosid-nymauth-vc-verifier.fly.dev/`. Under **Preconfigured
presentation requests** open **Show more presets** if needed; the presets are under **ZK Proofs**:
- **nym_auth_pseudonym** - prove a pseudonym only;
- **nym_auth_pseudonym_age** - also prove the holder's birth date is in a range (1980-01-01 to 2000-12-31, which
  alice-001, born 1990-01-15, satisfies).

Click one: a **QR code** appears with the wallet link under it. On the phone, open **Home** and use the wallet's QR scanner
(**Scan QR Code**) on the screen, or, on the same device, open the link with the wallet.

### 5. Consent in the wallet
The wallet shows **Credential Request**: the verifier's name, the credential **Nym authentication** and **0 data fields
requested** (nothing is disclosed: zero knowledge). **Next** -> **Select which data to share** (nothing to select) ->
**Next** -> **Ready to share** -> **Share**. Then **Processing...** / **Sending response to verifier...** and back to Home.

### 6. Check the result on the verifier page
The page shows **Verification succeeded** and a table **Credential claims - nym_auth** with columns **Name** and **Value**:
the **pseudonym** (`nym`), and for the age preset the **age** result, with the proof system and time. **Present again to the
same verifier: the pseudonym is the same** (it is scoped to this verifier). A different verifier gives a different one.

### 7. What failure looks like
A **Flow Failed** dialog with a reason. The usual ones are in the table below. **Stuck at "Contacting verifier..."** means the
wallet's engine connection is thrashing (two client connections replacing each other): update the app.

### Known gotchas
| Symptom | Cause | Fix |
|---|---|---|
| Wallet never answers a pseudonym request | **zkp_req requests** is OFF (default), or the wallet was created before it was switched on | switch it ON, then Disconnect / restart so the wallet is recreated |
| "no verifier id" / the request is refused | the PDP did not return `context.verifier_ids` for the verifier | the PDP must be go-trust >= 0.26.0 with the `policies:` in `environments/nymauth.yaml` (`credential-verifier` and `verifier` -> `x509_san_dns`) |
| verifier answers HTTP 500 / "issuer key set has 2 elements" | the wallet sent the issuer's certificate chain as `issuer_key_set` | the set must hold ONLY the Document Signer (DS) leaf; use an APK with that fix (siros-sdk-kotlin PR 280) |
| the pseudonym differs from other implementations, or the verifier refuses to start | `verifier.nym_auth.verifier_id` must be the PREFIXED client id `x509_san_dns:<verifier host>` and equal the verifier's client_id | fix `verifier_id` (changing it changes every pseudonym) |
| the preset list is empty, or a request has no `_req` items | the preset key is `req` in config, the WIRE member is `_req`; an old UI also dropped them | use a verifier image with the UI fix (vc `a56c4be9e` or later) |
| wallet refuses: invalid_meta | `meta.source_format` / `doctype_value` missing | they are DERIVED from the scope's `credential_metadata`; the preset's credential key must be that scope (`nym_auth`); do not configure them |
| "Contacting verifier..." forever | the engine keeps ONE session per user; two client connections supersede each other | one wallet connection per user; update the app (single-flight reconnect) |
| a verifier request is refused as untrusted | the verifier's request-signing certificate lacks the verifier host as a DNS SAN | `values.verifier.signingCertDnsSan: true` (set) re-issues it on deploy |
| the debug APK shows a 16 KB page-size alignment dialog on first start | a debug-build notice | dismiss it |
| offer redeem fails with invalid/expired code | the code is single use and valid 5 minutes | mint a new one |
| minting an offer fails with `invalid_scope` | the apigw image lacks SUNET/vc#840 (it ignores `allow_preauth_offer`) | pin vc images built from a SHA that contains #840 |

---

## For developers (appendix)

### Starting a presentation without the page
`GET /ui/metadata` lists the presets. `POST /ui/interaction` takes the DCQL the picker builds from a preset (the preset's
`req` goes on the wire as `_req`):
```sh
curl -s -H 'Content-Type: application/json' https://sirosid-nymauth-vc-verifier.fly.dev/ui/interaction -d '{
  "dcql_query": {"credentials": [{
    "id": "nym_auth", "format": "zkp_req",
    "meta": {"doctype_value": "eu.europa.ec.eudi.nym_auth.1", "source_format": "mso_mdoc"},
    "_req": [{"fn_id": "pseudonym", "return": "nym",
              "params": {"config": "known-offender-matching-ssc", "ver": 1, "prf": "sha-256"}}]}]}}'
```
The reply has `session_id`, `authorization_request` (`openid4vp://cb?client_id=x509_san_dns%3A<host>&request_uri=...`, the
QR and same-device link) and `dc_api_authorization_request`. For the age preset add a second item
`{"fn_id": "age_check", "return": "age", "params": {"ge": "1980-01-01", "le": "2000-12-31"}}` (the range must span at least
365 days). The request object (`GET <request_uri>`) is an ES256 `oauth-authz-req+jwt` with `client_id`
`x509_san_dns:<host>`, `response_mode` `direct_post.jwt` and an `x5c` whose leaf has the verifier host as a DNS SAN.

Deliver the link to a connected phone: `adb shell am start -a android.intent.action.VIEW -d 'openid4vp://cb?...'`.

### Reading the result by API
`GET /ui/completion?session_id=<session_id>` (poll until done), then `GET /ui/result?response_code=<code>`. Expect the
results keyed by the `return` handles (`nym`, `age`), plus `system_id` and `proof_time`.

### Checking issuance
- issuer log: `Delivered a secret seed {namespaces: [org.siros.nym_auth], notification_id: ...}`, then `POST /credential` 200 and
  `POST /notification` 204;
- engine log: `credential notification forwarded to issuer`; `/v1/evaluate` 200 (decision true);
- the issuer's `/.well-known/openid-credential-issuer` must NOT contain the seed (no 32-hex string) and advertises
  `notification_endpoint`.

### Logs
`flyctl logs --no-tail` returns only the **last 100 lines** per app; at debug level that is about a minute. **Run a continuous
capture before you reproduce:**
```sh
for a in wallet-backend wallet-proxy vc-verifier vc-apigw; do flyctl logs -a sirosid-nymauth-$a > /tmp/$a.log & done
```
On the phone: `adb logcat` filtered by the app's PID, started before the repro.

### Asking the PDP what the verifier path returns
```sh
flyctl proxy 18080:8080 -a sirosid-nymauth-pdp &
curl -s -H 'Content-Type: application/json' localhost:18080/evaluation -d '{
  "subject": {"type": "key", "id": "x509_san_dns:sirosid-nymauth-vc-verifier.fly.dev"},
  "resource": {"type": "x5c", "id": "x509_san_dns:sirosid-nymauth-vc-verifier.fly.dev", "key": ["<base64 DER leaf>", "<root>"]},
  "action": {"name": "credential-verifier"}}'
```
Expect `decision: true` and `context.verifier_ids` = `[{"verifier_id_type": "x509_san_dns", "verifier_id":
"sirosid-nymauth-vc-verifier.fly.dev", "basis": "client-id-binding"}]`. The issuer path (`action` `mdoc-issuer`, subject = the issuer
URL, x5c = the DS leaf) is true from the mDOC IACA registry with the leaf alone.

### The pins
| component | pin | built from |
|---|---|---|
| vc-verifier / apigw / issuer / registry | `registry.fly.io/sirosid-gdc-vc-<svc>:local-1793132043` | vc `integration/gdc-nymauth-v5` @ `c8c5b1ea` (includes #840; verifier + circuit-artifact overlay) |
| wallet-backend (engine) | `registry.fly.io/sirosid-gdc-wallet-backend:local-1792928945` | go-wallet-backend local branch @ `5536d36` (#469, #471, #163, #472, #480, #476, #477, #481, #482, #483-#486 + local-only commits; go-wmp v0.10.0; assurance simulation policy): the CLIENT does the encryption, so a client without that support fails issuance and direct_post.jwt |
| pdp | `ghcr.io/sirosfoundation/go-trust:0.26.0` | released (go-trust#218) |

The previously PROVEN vc build was `a56c4be9e` (verifier `local-1792603698`) with apigw/issuer/registry `local-1792473614`; the current pins
add the classified zkp_req refusals (HTTP 400 with a stable code) and #840, and are not yet proven by a Pixel run.
