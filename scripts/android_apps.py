#!/usr/bin/env python3
"""Shared Android app identity list - one source of truth read by BOTH
`make fly-up` (scripts/fly-up.py) and local docker-compose testing
(the Makefile), regardless of which target you're using.

Reads, in order (later sources add to, never replace, earlier ones - same
package can appear more than once with a different fingerprint, e.g. a
debug key and a Play Store upload key for the same app):

1. `extra` package=fingerprint strings passed by the caller (a CLI flag or
   Makefile var) - comma-separated entries are split, so a single string
   can carry a whole list.
2. `.android-apps` (gitignored, per-developer/per-checkout - see
   .android-apps.example for the format) - the persistent list this was
   built for: several debug builds and/or Play Store signing keys you
   want every environment (Fly or local) to trust, without having to
   repeat --android-app every time.
3. `.env.android` (written by `make android-setup` - ANDROID_PACKAGE/
   APK_KEY_HASH) - the existing single-identity local Android SDK testing
   convention, still honored on top of the other two.

Every identity is returned as {"package", "fingerprint_hex", "apk_key_hash"}
- two different encodings of the same cert, because different consumers
  want different ones:
  - assetlinks.json (Android's OS-level Digital Asset Links check) wants
    colon-separated SHA-256 hex, as printed by `keytool -list -v`.
  - wallet-backend's server.rp_origins (the server-side WebAuthn accept
    list) wants `android:apk-key-hash:<base64url, no padding>`.
Registering an identity in one without the other passes one check while
still failing the actual passkey ceremony - every caller of this module
must wire identities into both, not just one.

Run directly to print what would be loaded (for Makefile/debugging use):
  python3 scripts/android_apps.py                       # human-readable
  python3 scripts/android_apps.py --rp-origins           # comma-joined android:apk-key-hash:... list
  python3 scripts/android_apps.py --apk-key-hashes       # space-joined raw hashes, for building
                                                          # repeated --android-apk-key-hash flags
"""
import argparse
import base64
import sys
from pathlib import Path

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def hex_to_apk_key_hash(fingerprint_hex: str) -> str:
    """keytool -list -v prints colon-separated hex; rp_origins needs
    base64url (no padding) - same conversion setup-android.sh does."""
    raw = bytes.fromhex(fingerprint_hex.replace(":", ""))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def apk_key_hash_to_hex(apk_key_hash: str) -> str:
    """Opposite direction - .env.android/.android-apps may store either
    form; assetlinks.json needs colon-separated hex."""
    raw = base64.urlsafe_b64decode(apk_key_hash + "=" * (-len(apk_key_hash) % 4))
    return ":".join(f"{b:02X}" for b in raw)


def _parse_value(package: str, value: str) -> dict:
    value = value.strip()
    # Accept either encoding in the source files/flags - hex has colons,
    # base64url doesn't (and never contains ':').
    if ":" in value:
        fingerprint_hex, apk_key_hash = value, hex_to_apk_key_hash(value)
    else:
        apk_key_hash, fingerprint_hex = value, apk_key_hash_to_hex(value)
    return {"package": package.strip(), "fingerprint_hex": fingerprint_hex, "apk_key_hash": apk_key_hash}


def _read_pairs_file(path: Path):
    """Yields (package, value) from a `# comment` / `package=value` lines file."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        package, value = line.split("=", 1)
        yield package.strip(), value.strip()


def load_android_apps(extra: list = None, root: Path = None) -> list:
    root = root or SIROSID_DEV_ROOT
    identities = []
    seen = set()

    def add(package: str, value: str):
        ident = _parse_value(package, value)
        key = (ident["package"], ident["apk_key_hash"])
        if key not in seen:
            seen.add(key)
            identities.append(ident)

    for entry in extra or []:
        for part in entry.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise SystemExit(f"android app entry {part!r} must be package=fingerprint")
            package, value = part.split("=", 1)
            add(package, value)

    apps_file = root / ".android-apps"
    if apps_file.exists():
        before = len(identities)
        for package, value in _read_pairs_file(apps_file):
            add(package, value)
        if len(identities) > before:
            print(f".android-apps found - added {len(identities) - before} identities", file=sys.stderr)

    env_android = root / ".env.android"
    if env_android.exists():
        values = dict(_read_pairs_file(env_android))
        package, apk_key_hash = values.get("ANDROID_PACKAGE"), values.get("APK_KEY_HASH")
        if package and apk_key_hash:
            before = len(identities)
            add(package, apk_key_hash)
            if len(identities) > before:
                print(f".env.android found - adding debug identity for {package}", file=sys.stderr)

    return identities


def rp_origins(identities: list) -> list:
    return [f"android:apk-key-hash:{i['apk_key_hash']}" for i in identities]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--android-app", action="append",
                         help="package=fingerprint, repeatable, comma-separated entries also split.")
    parser.add_argument("--rp-origins", action="store_true",
                         help="Print a comma-joined android:apk-key-hash:... list instead of the "
                              "human-readable form (for shell/Makefile consumption).")
    parser.add_argument("--apk-key-hashes", action="store_true",
                         help="Print a space-joined list of raw apk-key-hash values (no "
                              "android:apk-key-hash: prefix) - for building repeated "
                              "--android-apk-key-hash flags to render-helm-config.py.")
    args = parser.parse_args()

    identities = load_android_apps(extra=args.android_app)
    if args.rp_origins:
        print(",".join(rp_origins(identities)))
    elif args.apk_key_hashes:
        print(" ".join(i["apk_key_hash"] for i in identities))
    else:
        for i in identities:
            print(f"{i['package']}: {i['fingerprint_hex']} ({i['apk_key_hash']})")


if __name__ == "__main__":
    main()
