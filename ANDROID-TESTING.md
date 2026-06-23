# Android SDK Testing with sirosid-dev

## Overview

The sirosid-dev environment provides complete support for testing the siros-sdk-kotlin sample app with all WSCD plugins enabled (softkey, R2PS, FIDO2) against a running conformance suite.

## Quick Start (Android SDK Testing)

```bash
cd sirosid-dev

# Step 1: Start full environment
make up VC=yes CONFORMANCE=yes

# Step 2 (Optional): Set custom app package name
# export ANDROID_PACKAGE=com.example.myapp

# Step 3: Generate Android configuration
make android-setup

# Step 4: Verify APK key hash was generated
cat .env.android
# Should show: APK_KEY_HASH=xAfybcO0yPlUA_Gko2tH6oIRb9Y-qa3uVtm0m_qW0w

# Step 5: Start Android helpers
make android-up

# Step 6: Install sample app APK and run conformance tests
adb install -r ../siros-sdk-kotlin/app/build/outputs/apk/debug/app-debug.apk

# Step 7: Send test credential offer
make android-launch
```

## Prerequisites

### Host System
- Docker & Docker Compose
- `adb` (Android Debug Bridge) for device communication
- `keytool` for keystore inspection
- Waydroid or Android emulator running

### Android Device / Emulator
- Android 11+ (API 30+)
- Network access to host at 192.168.240.1
- Development mode enabled
- USB debugging enabled (if physical device)

### Certificates
- Conformance suite TLS certificate installed in Android CA store
- App's APK signed with debug.keystore (created during build)

## Custom Package Name Configuration

By default, the sample app uses `org.sirosfoundation.sdk.sample`. To test with different package names:

```bash
# Before running android-setup, export custom package name
export ANDROID_PACKAGE=com.example.customwallet

# Then run setup (generates assetlinks.json with correct package)
make android-setup

# Verify in assetlinks.json
cat .well-known/assetlinks.json | jq '.[] | .target.package_name'
# Output: "com.example.customwallet"

# Build sample app (it reads ANDROID_PACKAGE from env)
cd ../siros-sdk-kotlin
./gradlew build
# Or modify BuildConfig.PACKAGE_NAME manually

# Deploy and test
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

**Why This Matters**:
- App Link verification requires package name match
- Credential offers with deep links require correct package
- Each package name variant needs separate APK signing cert
- Test multiple wallet implementations side-by-side

## Configuration Details

### What `make android-setup` Does

```bash
# 1. Extracts APK key hash from ~/.android/debug.keystore
keytool -list -v -alias androiddebugkey -keystore ~/.android/debug.keystore \
  -storepass android -keypass android | grep "SHA256:"
# Output: SHA256: C4 07 F2 6D C3 B4 C8 F9 54 03 F1 A4 A3 6B 47 EA 82 11 6F D6 3E A9 AD EE 56 D9 B4 9B FA 96 D3 B4

# 2. Converts hex format to base64 (Android's assetlinks.json format)
# C4:07:F2:6D:C3:B4:C8:F9:54:03:F1:A4:A3:6B:47:EA:82:11:6F:D6:3E:A9:AD:EE:56:D9:B4:9B:FA:96:D3:B4
#   →  xAfybcO0yPlUA_Gko2tH6oIRb9Y-qa3uVtm0m_qW0w

# 3. Generates .well-known/assetlinks.json
cat > .well-known/assetlinks.json <<EOF
[{
  "relation": ["delegate_permission/common.handle_all_urls"],
  "target": {
    "namespace": "android_app",
    "package_name": "org.sirosfoundation.sdk.sample",
    "sha256_cert_fingerprints": ["xAfybcO0yPlUA_Gko2tH6oIRb9Y-qa3uVtm0m_qW0w"]
  }
}]
EOF

# 4. Creates .env.android for container environment
cat > .env.android <<EOF
APK_KEY_HASH=xAfybcO0yPlUA_Gko2tH6oIRb9Y-qa3uVtm0m_qW0w
EOF
```

### Why This Matters

- **App Link Verification**: Android requires matching certificate signatures for deep links
- **Credential Offer Handling**: When issuer sends `openid-credential-offer://` deep link, Android verifies:
  1. URL domain matches app's URL schemes
  2. Domain's assetlinks.json matches APK's signing certificate
  3. Only then does it launch the app with the credential offer

## Architecture: Waydroid vs Physical Device

### Option 1: Waydroid (Recommended for CI/CD)

```bash
# Waydroid runs Android in a container alongside docker-compose services
# All services accessible at 192.168.240.1 from Waydroid's perspective

docker-compose -f docker-compose.android.yml up -d
waydroid shell  # Opens shell in Waydroid environment
adb devices     # Lists connected Waydroid instance
```

**Advantages**:
- No physical device required
- Fast iteration
- Reproducible in CI

**Limitations**:
- May be slower than native
- Requires adequate RAM

### Option 2: Physical Device / Local Emulator

```bash
# Connect via USB or run local Android emulator
adb devices    # List connected devices

# Forward network traffic if not on same network
adb forward tcp:8080 tcp:8080  # Backend
adb forward tcp:3000 tcp:3000  # Frontend
```

**Advantages**:
- Realistic performance testing
- Hardware capabilities available

**Limitations**:
- Requires physical device or emulator setup
- Network configuration may be needed

## WSCD Plugins Configuration

The siros-wscd-manager AAR includes three signing backends:

### 1. plugin-softkey (Default)

```kotlin
// JWE-encrypted container, stored locally on device
val plugin = SoftkeyPlugin()
plugin.createKey("key-1", algorithm = "P-256")
val signature = plugin.sign("key-1", data)
// Uses Android Keystore for encryption
```

**When to use**: Development, testing without HSM
**Security**: Medium (encrypted locally, no attestation)

### 2. plugin-r2ps (Remote PAKE-Protected Signing)

```kotlin
// Requires R2PS service running (make up R2PS=yes)
val plugin = R2psPlugin(
  serverUrl = "http://192.168.240.1:8443",  // or 9443 with conformance
  clientId = "sdk-test",
  context = "wallet"
)

// Flow:
// 1. Register: client registers credential with OPAQUE
// 2. Authenticate: client sends OPAQUE response to get session key
// 3. Generate Key: wallet sends P256Generate request
// 4. Sign: wallet sends sign request with session key
val signature = plugin.sign("remote-key-1", data)
```

**When to use**: Production simulation, HSM-backed keys, audit trails
**Security**: High (keys never leave HSM, PAKE authentication)

**Environment Setup**:
```bash
# Start with R2PS overlay
make up R2PS=yes

# Configure sample app
export R2PS_ENABLED=true
export DEFAULT_R2PS_URL="http://192.168.240.1:9443"  # if with conformance

# View R2PS status
curl -s http://r2ps-server:8443/admin/store/keys --cert ...
```

### 3. plugin-fido2 (YubiKey rawSign/previewSign)

```kotlin
// Requires FIDO2 device (YubiKey, etc.)
val plugin = PreviewSignPlugin()

// Uses Yubico's previewSign & rawSign extensions for credential signing
val preview = plugin.previewSign(...)  // Get preview of what will be signed
val signature = plugin.rawSign(...)     // Perform actual signing
```

**When to use**: Hardware-backed signing, high security requirements
**Security**: Highest (keys on hardware device, user-verified)

**Prerequisites**:
- YubiKey 5 or later with FIDO2
- NFC/USB connection to Android device

## Test Flow: Complete Credential Issuance

```
1. Start environment with all plugins:
   make up VC=yes CONFORMANCE=yes
   make android-setup

2. Deploy sample app with R2PS enabled:
   export R2PS_ENABLED=true
   export DEFAULT_R2PS_URL="http://192.168.240.1:9443"
   ./gradlew installDebug

3. Initiate credential offer from conformance suite:
   - Navigate to https://localhost.emobix.co.uk:8443/
   - Create OID4VCI wallet test plan
   - Conformance suite sends: openid-credential-offer://...

4. Android app receives offer:
   - App Link verification succeeds (assetlinks.json matches APK)
   - App launched with credential offer URI
   - SDK parses offer and discovers issuer metadata

5. Wallet shows credential details:
   - User confirms credential acceptance
   - SDK selects signing backend:
     a) plugin-softkey: uses local key (default)
     b) plugin-r2ps: contacts R2PS server, registers, authenticates, generates key, signs
     c) plugin-fido2: prompts YubiKey NFC/USB for signature

6. Backend receives credential request:
   - Verifies signature using public key
   - Issues credential (SD-JWT, mdoc, etc.)

7. Conformance suite validates:
   - Receives issued credential
   - Checks format, signatures, expiration
   - Marks test module PASSED or FAILED
```

## Debugging Android Tests

### Check App Logs

```bash
# Stream app logcat
adb logcat | grep siros

# Save to file for analysis
adb logcat > android-test.log &

# Specific tag filtering
adb logcat | grep "siros-wscd-manager"
```

### Verify Certificate Installation

```bash
# On device/Waydroid
adb shell

# Inside shell:
ls -la /system/etc/security/cacerts/
# Should include conformance suite CA cert

# Test HTTPS connection
curl -v https://localhost.emobix.co.uk:8443/ 2>&1 | grep "SSL"
```

### Check R2PS Connectivity

```bash
# From Android device/Waydroid
adb shell curl -v http://192.168.240.1:9443/admin/store/keys

# Should return list of provisioned keys
```

### View Backend Audit Logs

```bash
# Backend logs all wallet operations via SET framework
docker logs wallet-backend-e2e-test --follow | grep "Event"

# Look for audit events like:
# EventWICreated, EventWIDeactivated
# EventInviteCreated, EventInviteUpdated
```

### Network Debugging

```bash
# If device cannot reach host:
# 1. Verify host IP from device perspective
adb shell getprop ro.hardware | grep -q emulator && \
  BRIDGE_IP=$(hostname -I | awk '{print $1}') || \
  BRIDGE_IP=192.168.1.X

# 2. Test connectivity
adb shell ping -c 1 192.168.240.1

# 3. Port forwarding (if on separate network)
adb reverse tcp:8080 tcp:8080
adb reverse tcp:8443 tcp:8443
```

## Integration with CI/CD

### GitHub Actions Workflow Example

```yaml
name: Android Conformance Tests

on: [push, pull_request]

jobs:
  android-conformance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Start environment
        run: |
          cd sirosid-dev
          make up VC=yes CONFORMANCE=yes
          make android-setup

      - name: Build sample app
        run: |
          cd ../siros-sdk-kotlin
          ./gradlew build

      - name: Run conformance tests
        run: |
          cd ../sirosid-dev
          make test-wallet  # Via Playwright in Waydroid
```

## Known Issues & Workarounds

### Issue: assetlinks.json Returns 404

**Symptom**: App Link verification fails, deep link doesn't launch app

**Solution**:
```bash
# Verify file exists and is served
curl -s http://localhost:3000/.well-known/assetlinks.json | jq

# Check nginx config serves it
docker exec wallet-proxy-e2e curl -s http://wallet-frontend:3000/.well-known/assetlinks.json

# Regenerate
make android-setup
```

### Issue: R2PS Connection Refused

**Symptom**: Plugin fails with "connection refused" to R2PS

**Solution**:
```bash
# Check R2PS container is running
docker ps | grep r2ps

# Verify port is exposed (should be 9443 with conformance)
docker port r2ps-server | grep 8443

# From device, test connectivity
adb shell curl -v http://192.168.240.1:9443/admin/store/keys
```

### Issue: Credential Signing Fails (All Plugins)

**Symptom**: "Sign operation failed" error in app

**Debugging**:
```bash
# Check backend logs for key lookup errors
docker logs wallet-backend-e2e-test | grep -i "key\|sign"

# Verify key exists in WSCD
adb shell  # On device
java -cp /system/app/siros-wscd-manager/siros-wscd-manager.jar \
  org.sirosfoundation.wscd.ListKeys

# Check R2PS key inventory
curl -s http://r2ps-server:8443/admin/store/keys | jq '.[] | .kid'
```

## Advanced: Custom Test Scenarios

### Test 1: Verify All Three Plugins Work

```bash
# In sample app, modify test to cycle through plugins:
val plugins = listOf(
  SoftkeyPlugin(),
  R2psPlugin(...),
  PreviewSignPlugin()
)

for (plugin in plugins) {
  val key = plugin.createKey(...)
  val signature = plugin.sign(key, testData)
  assert(verifySignature(signature)) { "Plugin ${plugin.name} failed" }
}
```

### Test 2: Measure Signing Performance

```kotlin
val start = SystemClock.elapsedRealtime()
val signature = plugin.sign(keyId, data)
val elapsed = SystemClock.elapsedRealtime() - start
println("Sign time: ${elapsed}ms")
```

### Test 3: Concurrent Signings (Stress Test)

```kotlin
// Each plugin has different concurrency limits
// SoftkeyPlugin: unlimited (local)
// R2psPlugin: limited by R2PS session pool
// PreviewSignPlugin: 1 at a time (FIDO2 device)

val jobs = (1..100).map { i ->
  launchAsync {
    plugin.sign(keyId, data)
  }
}
jobs.awaitAll()
```

## Support & Diagnostics

```bash
# Full environment status
make status

# Show all running containers
docker ps -a

# Deep dive: specific service logs
docker logs <service> --follow

# Network inspection
docker network ls
docker network inspect sirosid-dev_default

# Database queries (VC)
docker exec -it conformance-suite-mongodb mongo

# Database queries (Wallet)
docker exec -it wallet-db psql -U wallet -c "SELECT * FROM wallet_instance;"
```

---

**Last Updated**: 2026-06-23  
**Sample App**: siros-sdk-kotlin, branch fix/private-data-issues-27-31  
**WSCD Manager**: 0.1.0 with all plugins (softkey, r2ps, fido2)
