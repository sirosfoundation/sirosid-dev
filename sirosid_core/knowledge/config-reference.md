---
title: "Saved config reference - every key"
summary: "Every key of a saved config with its type, meaning, the validation rules actually enforced, the capability it needs and a worked example."
digest: "A saved config is a JSON object; unknown keys are rejected. Keys - schema_version, name, channel, trusted_issuers, trusted_verifiers, trusted_verifier_roots, credential_registries, android_apps, wallet_attestation, dc_api_enable, images (custom_images capability), values (raw_values capability), conformance. Run validate_config first: it returns every problem at once as 'path: message'."
order: 40
tags: [config, schema, keys, validation, saved-config, policy]
---
A saved config is the document you edit, save (`save_config`) and create instances from. It is
deliberately smaller than what the platform deploys: org, region, hostnames, naming, layout and
scale-to-zero are the platform's and cannot be set. `get_config_schema` returns the same key list as
JSON Schema.

Rules that apply to the whole document:
- It must be a JSON object. **Unknown keys are errors**, never ignored.
- Every problem is reported at once, as `path: message` (for example `trusted_issuers[1]: must be an
  https URL`), so fix them all in one round trip. `validate_config` checks without saving; `save_config`
  validates and refuses an invalid config.
- Capabilities are checked again at `create_instance` time, against the account's current grants.
- The config's name for `save_config`/`create_instance` (`config_name`, at most 64 characters) is
  separate from the `name` key inside it. An account can hold at most 50 saved configs.
- The smallest valid config is `{}` or `{"schema_version": 1}`: the standard stack.

"Public https URL" below means exactly what the validator enforces: scheme `https`, at most 2048
characters, no user name or password in it, a host name containing a dot, not `localhost`, not ending in
`.internal`, `.local`, `.localhost`, `.lan`, `.home`, `.corp` or `.flycast`, a valid port if one is given,
and, if the host is an IP address, a globally routable one.

### `schema_version`

Integer. Must be `1` (a boolean is rejected). Optional; absent means 1.

```json
{"schema_version": 1}
```

### `name`

String, at most 64 characters. A free label for the config, for your own use. Optional.

```json
{"schema_version": 1, "name": "partner interop, week 41"}
```

### `channel`

String. Which released set of component versions to run. It must be one of the channels the platform
operator defined; `default` (the versions pinned by the platform) always exists. An unknown channel is
an error that lists the valid ones. Optional; absent means `default`. Explicit `images` override the
channel's choice per component.

```json
{"schema_version": 1, "channel": "default"}
```

### `trusted_issuers`

Array (at most 50) of public https URLs: extra issuers the instance's PDP trusts. Each must be the
issuer's **credential_issuer identifier** - for a SUNET/vc issuer that is its apigw's public URL - not an
internal host and not a bare host name. Only adds to the issuer list, never to the verifier list. Your
own vc-apigw is always trusted without listing it. Entities without a JWKS endpoint slow the PDP's boot
(see `trust`).

```json
{"schema_version": 1, "trusted_issuers": ["https://issuer.example.org"]}
```

### `trusted_verifiers`

Array (at most 50) of verifier identities to trust. Each entry is either a public https URL, or a string
of at most 512 characters from `A-Z a-z 0-9 : . _ ~ / % + = @ , -` starting with `x509_hash:`,
`x509_san_dns:`, `x509_san_uri:` or `decentralized_identifier:`. Anything else (including a bare
`did:web:...`) is rejected. What actually matches is subtle - write `https://<host>` for x509_san_dns
verifiers and DC API origins, `x509_hash:<hash>` verbatim from the denial, and
`decentralized_identifier:did:web:<host>` for DID verifiers. See `trust` before adding one.

```json
{"schema_version": 1, "trusted_verifiers": ["https://verifier.example.org", "x509_hash:PASTE_HASH_FROM_DENIAL"]}
```

(Replace the placeholder with the real value; the validator accepts it only because it has the right
prefix and characters.)

### `trusted_verifier_roots`

Array (at most 10) of strings, each the PEM text (at most 16 KiB) of ONE CA certificate: it must start
with the PEM certificate header line, end with the certificate footer line, and contain no private key.
The certificates are added to the PDP's root pool, for verifiers whose request-signing certificate is
issued by a self-signed "reader CA" root rather than a public CA (an ISO 18013-5 convention). It does not
trust anyone by itself; the verifier must also be in `trusted_verifiers`.

```text
{"schema_version": 1,
 "trusted_verifiers": ["https://verifier.example.org"],
 "trusted_verifier_roots": ["<the PEM text of the reader CA certificate, newlines as \n>"]}
```

### `credential_registries`

Array (at most 50) of public https base URLs of credential-type registries. When non-empty, every
SD-JWT type's metadata is resolved by its `vct` from these registries instead of the vendored copies, for
the issuer, the verifier and the wallet backend alike; mdoc types stay vendored. Order matters: a later
registry overrides an earlier one for the same type. See `registries`.

```json
{"schema_version": 1, "credential_registries": ["https://registry.siros.org"]}
```

### `android_apps`

Array (at most 20) of strings `package=fingerprint`; one string may hold several, comma-separated. The
fingerprint is the app signing certificate's SHA-256, either colon-separated hex as `keytool -list -v`
prints it, or the base64url "apk-key-hash" form. Each pair is added to the wallet's
`/.well-known/assetlinks.json` and to wallet-backend's accepted WebAuthn origins
(`android:apk-key-hash:...`), so that app can use passkeys against this instance. A malformed entry is an
error. See `mobile-testing`.

```json
{"schema_version": 1, "android_apps": ["org.example.wallet=AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"]}
```

### `wallet_attestation`

Boolean (a string `"true"` is rejected). Turns on wallet-attestation based client authentication: the
wallet backend issues a wallet instance attestation (WIA) and authenticates to the issuer with it, and
the PDP trusts this instance's wallet provider. Default false. See `attestation`.

```json
{"schema_version": 1, "wallet_attestation": true}
```

### `dc_api_enable`

String, exactly `""`, `"true"` or `"false"` (a JSON boolean is rejected). Overrides the verifier's W3C
Digital Credentials API support; `""` (the default) leaves the platform's default. See `presenting`.

```json
{"schema_version": 1, "dc_api_enable": "true"}
```

### `images`

Object of `component -> image reference`. Needs the **`custom_images`** capability whenever it is
non-empty. Keys must be component names (`mongodb`, `mini-oidc`, `vc-registry`, `vc-issuer`,
`vc-verifier`, `vc-apigw`, `pdp`, `wallet-backend`, `wallet-proxy`, `env-admin`, `wallet-frontend`, or a
conformance component). Each reference must be fully qualified (`registry.host/repo:tag` or
`...@sha256:<64 hex>`), have a tag or digest, be at most 300 characters, and not use `registry.fly.io`,
`localhost`, an internal suffix or a private IP. The operator may restrict the allowed registries. The
platform does not check that the image exists or boots. See `custom-images`.

```json
{"schema_version": 1, "images": {"wallet-backend": "ghcr.io/sirosfoundation/go-wallet-backend:0.22.3"}}
```

### `values`

Object of raw chart values, deep-merged **last** over everything the platform renders. Needs the
**`raw_values`** capability whenever it is non-empty. Nothing inside is validated by the policy, so a
wrong key is usually ignored silently by the chart and a wrong shape can stop a service from booting.
It is the escape hatch for anything the typed keys do not cover, for example enabling the PDP's did:web
resolver.

```json
{"schema_version": 1, "values": {"pdp": {"extraRegistries": {"didweb": {"enabled": true, "name": "did:web", "description": "did:web DID document resolution", "timeout": "10s"}}}}}
```

### `conformance`

Boolean. Also deploy the OpenID conformance suite. Refused unless the platform allows it, and always
refused in the single-machine layout the hosted service uses. Default false.

```json
{"schema_version": 1, "conformance": false}
```

## A combined example

```json
{
  "schema_version": 1,
  "name": "interop with a partner",
  "credential_registries": ["https://registry.siros.org"],
  "wallet_attestation": true,
  "dc_api_enable": "true",
  "trusted_issuers": ["https://issuer.example.org"],
  "trusted_verifiers": ["https://verifier.example.org"],
  "android_apps": ["org.example.wallet=AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"]
}
```

Sources: `sirosid_core/policy.py`, `sirosid_core/spec.py`, `sirosid_core/android.py`, `sirosid_core/render.py`, `sirosid_core/vc_render.py`, `sirosid_core/components.py`, `sirosid_service/service.py`, `environments/gdc.yaml`, `environments/README.md`, `CLAUDE.md`
