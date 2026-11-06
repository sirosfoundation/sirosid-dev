#!/usr/bin/env python3
"""Mint the Rust WMP engine's PDP bearer (see sirosid_core/engine_pdp_token.py
for the contract: an AS-issued token go-wallet-backend's /v1/evaluate and
/v1/resolve TAC gate accepts).

Usage (local stack):
    python3 scripts/mint_engine_pdp_token.py                # print the token
    python3 scripts/mint_engine_pdp_token.py --env-var      # ENGINE_PDP_TOKEN=... form

Then start the engine with it:
    WMP_ENGINE_ENGINE_PDP_TOKEN=<token> make up WMP_ENGINE=yes
(a Fly deploy mints fresh itself - sirosid_core/deploy.py's wmp-engine
branch - so this CLI form is for the local stack and manual secrets.)
"""
import argparse
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sirosid_core.engine_pdp_token import mint_engine_pdp_token  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_KEY = ROOT / "fixtures" / "vc-pki" / "signing_ec_private.pem"
DEFAULT_ISSUER = "http://localhost:8080"          # WALLET_AS_ISSUER (the public origin)
DEFAULT_JWKS = "http://localhost:8080/auth/.well-known/jwks.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", type=pathlib.Path, default=DEFAULT_KEY, help="the AS signing key (PEM)")
    ap.add_argument("--issuer", default=DEFAULT_ISSUER, help="the iss claim (WALLET_AS_ISSUER)")
    ap.add_argument("--jwks", default=DEFAULT_JWKS, help="printed in the reminder, not used to mint")
    ap.add_argument("--audience", default="wallet-backend", help="the aud claim")
    ap.add_argument("--ttl-hours", type=int, default=24 * 365, help="the token's life")
    ap.add_argument("--env-var", action="store_true", help="print ENGINE_PDP_TOKEN=<token> instead")
    args = ap.parse_args()

    token = mint_engine_pdp_token(args.key.read_text(), issuer=args.issuer,
                                  audience=args.audience, ttl_hours=args.ttl_hours)
    print(f"ENGINE_PDP_TOKEN={token}" if args.env_var else token)
    print(f"# verify against {args.jwks}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
