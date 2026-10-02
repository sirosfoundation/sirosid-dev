"""Android signing-key fingerprint encodings. Pure."""
import base64


def hex_to_apk_key_hash(fingerprint_hex: str) -> str:
    """keytool -list -v prints colon-separated hex; rp_origins needs
    base64url (no padding) - same conversion setup-android.sh does."""
    raw = bytes.fromhex(fingerprint_hex.replace(":", ""))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
