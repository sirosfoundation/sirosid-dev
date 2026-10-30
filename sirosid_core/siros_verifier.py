"""Deployment helpers for the optional siros-verifier-core app."""
import base64
import hashlib
import ssl
from pathlib import Path


def x509_hash_identity(certificate_path: Path) -> str:
    """OpenID4VP x509_hash client identity for a PEM leaf certificate."""
    der = ssl.PEM_cert_to_DER_cert(Path(certificate_path).read_text())
    digest = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    return f"x509_hash:{digest}"
