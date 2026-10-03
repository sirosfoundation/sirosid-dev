"""Android signing-key fingerprint encodings. Pure."""
import base64


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


def parse_identity(package: str, value: str) -> dict:
    value = value.strip()
    # Accept either encoding in the source files/flags - hex has colons,
    # base64url doesn't (and never contains ':').
    if ":" in value:
        fingerprint_hex, apk_key_hash = value, hex_to_apk_key_hash(value)
    else:
        apk_key_hash, fingerprint_hex = value, apk_key_hash_to_hex(value)
    return {"package": package.strip(), "fingerprint_hex": fingerprint_hex, "apk_key_hash": apk_key_hash}


def identities_from_entries(entries) -> list:
    """`package=fingerprint` entries (each may itself be comma-separated) to
    identities, de-duplicated by (package, key hash). ValueError on a malformed
    entry; callers decide whether that is a usage error or a bad request."""
    identities, seen = [], set()
    for entry in entries or []:
        for part in entry.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(f"android app entry {part!r} must be package=fingerprint")
            package, value = part.split("=", 1)
            ident = parse_identity(package, value)
            key = (ident["package"], ident["apk_key_hash"])
            if key not in seen:
                seen.add(key)
                identities.append(ident)
    return identities


def rp_origins(identities: list) -> list:
    return [f"android:apk-key-hash:{i['apk_key_hash']}" for i in identities]
