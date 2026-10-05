#!/usr/bin/env python3
"""The admin token facetec-api presents to vc-apigw's datastore API.

facetec-api uploads each accepted scan to `/api/v1/datastore` and asks for a
pre-authorized offer, both behind `api_server.api_auth` (see api_auth.py).
Unlike scripts/datastore.py it cannot mint a fresh token per request - it
reads one static Bearer value from ISSUER_API_KEY_PATH - so this writes a
token with the same issuer, audience and subject, but a lifetime long enough
to outlast a working session. `make up FACETEC=yes` re-mints it every run;
the key it is signed with stays local to fixtures/rendered-secrets/.

    python3 scripts/facetec_issuer_token.py [--ttl SECONDS]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_auth  # noqa: E402
from datastore import ROOT, Target  # noqa: E402

TOKEN_PATH = ROOT / "fixtures" / "rendered-secrets" / "facetec-issuer-token"
DEFAULT_TTL = 30 * 24 * 3600


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="token lifetime in seconds")
    args = parser.parse_args()

    target = Target()
    token = api_auth.mint(target.key, target.auth["issuer"], target.auth["audience"],
                          target.subject, ttl=args.ttl)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    print(f"wrote {TOKEN_PATH.relative_to(ROOT)} (valid {args.ttl // 3600}h)")


if __name__ == "__main__":
    main()
