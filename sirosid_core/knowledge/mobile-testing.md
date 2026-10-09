---
title: "Mobile testing - native apps, passkeys, assetlinks, AASA"
summary: "How to point an Android or iOS wallet app at an instance, what android_apps does, where assetlinks.json and apple-app-site-association are served, and why passkey sign-up fails."
digest: "Point a native app's backend URL at the instance's wallet-proxy URL. Android passkeys need the app's package=SHA-256 fingerprint in android_apps (it lands in assetlinks.json AND wallet-backend's accepted origins); a missing entry makes sign-up fail with \"Error validating origin\" while existing logins work. iOS app ids are platform-fixed."
order: 90
tags: [mobile, android, ios, passkeys, assetlinks, aasa, sdk, sample-app]
---
## The URLs an app needs

| setting | value |
|---|---|
| backend URL (SDK sample app "backend URL", wrapper apps' backend setting) | the instance's **wallet-proxy** URL |
| passkey RP ID | the **wallet-frontend** host (no scheme) |
| web wallet | the wallet-frontend URL, under `/id/default/` |

The siros-sdk-kotlin sample app (`org.siros.sdk.sample`) has a runtime backend URL in its Settings, so
no rebuild is needed to point it at a new instance. A new instance has new URLs: update the app after
creating one (stop/start/reset keep them).

## Android: android_apps

Android only lets an app use passkeys for a domain when both of these hold:
1. the domain's `/.well-known/assetlinks.json` lists the app's package and signing-certificate SHA-256
   (Android's Digital Asset Links check), and
2. the server (wallet-backend) accepts the app's origin, `android:apk-key-hash:<base64url of the same
   SHA-256>`, in its WebAuthn origin list.

`android_apps` sets both from one list. To add an app:
1. get the fingerprint: `keytool -list -v -keystore <keystore>` and copy the `SHA256:` line (colon hex);
2. add `"<package>=<fingerprint>"` to `android_apps` (several keys for one package are fine - debug,
   CI and Play upload keys);
3. validate and save the config, then create an instance from it (the list is fixed at deploy time).

```json
{"schema_version": 1, "android_apps": ["org.example.wallet=AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"]}
```

The platform already includes the production identities the SIROS ID chart ships (published apps such
as `org.siros.id` and `io.yubicolabs.wwwwallet`); `android_apps` adds to them. The instance dashboard's
Native App Setup card lists every identity the backend actually accepts.

Symptoms of a missing or wrong entry:
- sign-up fails with "Error validating origin" (HTTP 400 at passkey register finish) while logging in
  with an already-registered passkey keeps working - it looks like a server regression but is a missing
  entry;
- "Origin not allowed": the key hash does not match the keystore that signed the installed build;
- "RP ID cannot be validated": documented for local debug setups, where the developer flag below is
  missing; on a hosted instance, check the entry (unverified there).

Local developer setups (not hosted instances) use `adb shell am compat enable
DEVELOPMENT_PASSKEY_REGISTRATION <package>` for debug builds; see `ANDROID-TESTING.md` in the repo.

## iOS: apple-app-site-association

The web wallet serves `/.well-known/apple-app-site-association` (applinks and webcredentials) at the
**wallet-frontend** host, which is where iOS looks because that is the RP ID. The app ids in it come
from the platform's chart defaults (`X3YM693CZ3.org.siros.id`, `W36S8KLY7S.com.netzarchitekten.funkewallet`,
`X3YM693CZ3.org.siros.wwwallet`). A saved config has no typed key for more iOS app ids; changing them takes
a `values` override (raw_values capability) and is unverified for the hosted layout.

## Passkeys and lifecycle

- A wallet passkey is bound to the wallet-frontend host of ONE instance. A new instance (new id) means
  new passkeys; old ones on the device stay but cannot log in.
- `reset_instance` deletes the wallet accounts: the device keeps a passkey the server no longer knows.
  Delete it on the device and sign up again.
- Cookies: the web wallet and the backend are on different hosts; the backend allows credentialed CORS
  only from the instance's own wallet-frontend origin, so the web wallet must be used from its own URL,
  not embedded elsewhere.

## Issuance redirects on a phone

The issuer's wallet client accepts the web wallet's callback and the sample app's `siros-sample://callback`
deep link, so the authorization-code flow (with the mini-oidc login) works from the native sample app
on the phone itself. The verifier offers the native app through its `openid4vp://cb` scheme.

Sources: `sirosid_core/render.py`, `sirosid_core/assets.py`, `sirosid_core/android.py`, `sirosid_core/policy.py`, `chart/values.yaml`, `values-base.yaml`, `environments/gdc.yaml`, `README.md`, `ANDROID-TESTING.md`, `CLAUDE.md`
