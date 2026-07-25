#!/usr/bin/env python3
"""Render wallet-backend and go-trust (PDP) config from helm-charts/siros-id-stack.

sirosid-dev historically hand-maintained a parallel set of flat env vars
(docker-compose.test.yml's `wallet-backend` service, `--registry`/`--whitelist`
CLI flags for go-trust) that happen to drive the same underlying config schema
as the production Helm chart, but drift independently of it. Both
go-wallet-backend (pkg/config/config.go) and go-trust (pkg/config/config.go)
load one config struct via a YAML file (`--config`); Helm just renders that
YAML into a ConfigMap instead of a hand-written file.

This script renders the real chart with `helm template` (no cluster contact -
this only needs the chart on disk, see HELM_CHARTS_PATH) and extracts the
ConfigMap `data` blocks for wallet-backend and pdp verbatim, so any change to
the chart's templates/values flows into sirosid-dev automatically. The only
hand-written logic here is the small set of rewrites that are genuinely
environment-specific and have no values.yaml toggle to express them:

  - wallet-backend's rp_origins/base_url/cors.allowed_origins/registry_url are
    built by helpers that hardcode an `https://` scheme (siros-id.origins.*,
    04-wallet-backend.yaml) - sirosid-dev serves everything over plain http.
  - wallet-backend's storage.mongodb is rendered for the MongoDB Community
    Operator (x509 mTLS SRV record) - sirosid-dev runs a single plain `mongo`
    container (docker-compose.vc-services.yml), so there's nothing to
    authenticate against and no cert volume to mount.

PDP's whitelist is NOT patched here - see values-dev.yaml's `pdp:` block,
which disables `default_whitelist` (hardcoded https placeholder hostnames)
and supplies an equivalent whitelist of real compose-network URLs via
`pdp.extraRegistries`, using the chart's own extension point instead of
text surgery.

Output goes to fixtures/rendered/ (gitignored - regenerate with
`make render-helm-config` whenever helm-charts or values-dev.yaml change).
Secrets are generated once into fixtures/rendered-secrets/ and reused on
subsequent runs (idempotent, like the chart's own `lookup`-based generator).
"""
import argparse
import secrets
import string
import subprocess
import sys
from pathlib import Path

import yaml

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

# wallet-backend secret file names -> length. Mirrors
# helm-charts/siros-id-stack/config/secret_generator_template.yaml's use of
# `randAlphaNum 32` for these same keys.
WALLET_BACKEND_SECRETS = ["jwtSecret", "adminToken"]


def helm_template(chart_dir: Path, values_files: list[Path], namespace: str) -> str:
    cmd = [
        "helm", "template", "siros-id-stack", str(chart_dir),
        "--namespace", namespace,
    ]
    for f in values_files:
        cmd += ["-f", str(f)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"helm template failed (exit {result.returncode})")
    return result.stdout


def extract_configmap_data(manifest_yaml: str, name: str) -> dict:
    for doc in yaml.safe_load_all(manifest_yaml):
        if not doc:
            continue
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == name:
            return doc["data"]
    raise ValueError(
        f"ConfigMap {name!r} not found in rendered manifest - "
        "has the chart's template/ConfigMap naming changed upstream?"
    )


def gen_secret(path: Path, length: int = 32) -> str:
    """Idempotent: an existing generated secret is reused, never rotated in place."""
    if path.exists():
        return path.read_text().strip()
    alphabet = string.ascii_letters + string.digits
    value = "".join(secrets.choice(alphabet) for _ in range(length))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return value


def patch_wallet_backend(config: dict) -> dict:
    config["server"]["rp_id"] = "localhost"
    # Drop the chart's hardcoded https:// primary frontend origin, keep extraRpOrigins verbatim
    # (android:apk-key-hash:... entries have no scheme, nothing to rewrite there).
    android_origins = [o for o in config["server"]["rp_origins"] if o.startswith("android:")]
    config["server"]["rp_origins"] = ["http://localhost:3000"] + android_origins
    config["server"]["base_url"] = "http://localhost:8080"
    config["server"]["cors"]["allowed_origins"] = ["http://localhost:3000"]
    config["trust"]["registry_url"] = "http://localhost:8080/registry"
    config["storage"]["mongodb"] = {
        "uri": "mongodb://mongodb:27017/wallet-backend",
        "tls_enabled": False,
        "database": "wallet-backend",
    }
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT.parent / "helm-charts" / "siros-id-stack"),
                         help="Path to the siros-id-stack chart (default: ../helm-charts/siros-id-stack)")
    parser.add_argument("--namespace", default="sirosid-dev")
    parser.add_argument("--out-dir", default=str(SIROSID_DEV_ROOT / "fixtures" / "rendered"))
    parser.add_argument("--secrets-dir", default=str(SIROSID_DEV_ROOT / "fixtures" / "rendered-secrets"))
    args = parser.parse_args()

    chart_dir = Path(args.chart_dir)
    if not chart_dir.is_dir():
        raise SystemExit(
            f"Chart not found at {chart_dir} - clone sibling repo "
            f"'helm-charts' next to sirosid-dev, or pass --chart-dir "
            f"(see HELM_CHARTS_PATH in the Makefile)"
        )

    # helm template applies the chart's own values.yaml automatically; only the
    # sirosid-dev override needs to be passed explicitly.
    values_files = [SIROSID_DEV_ROOT / "values-dev.yaml"]
    manifest = helm_template(chart_dir, values_files, args.namespace)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- wallet-backend ---
    wb_data = extract_configmap_data(manifest, "wallet-backend-main")
    backend_cfg = patch_wallet_backend(yaml.safe_load(wb_data["backend.yaml"]))
    (out_dir / "wallet-backend.yaml").write_text(yaml.dump(backend_cfg, sort_keys=False))
    (out_dir / "wallet-backend-registry.yaml").write_text(wb_data["registry.yaml"])
    print(f"wrote {out_dir / 'wallet-backend.yaml'}")
    print(f"wrote {out_dir / 'wallet-backend-registry.yaml'}")

    # registry.yaml's local_overrides always points at /vctms - Helm mounts this
    # from a `vctms` ConfigMap (created even when empty), so the directory must
    # exist here too or the registry provider fails to start (`stat /vctms: no
    # such file or directory`), even with features.credentialTypes: {}.
    (out_dir / "vctms").mkdir(exist_ok=True)

    # registry.yaml's cache.path lives under /cache, which Helm mounts as an
    # emptyDir (writable by the pod's fsGroup). go-wallet-backend runs as
    # uid 65532 (Dockerfile:45), and a bind-mounted dir defaults to the host
    # user's ownership, so it must be made world-writable here explicitly.
    cache_dir = out_dir / "cache"
    cache_dir.mkdir(exist_ok=True)
    cache_dir.chmod(0o777)

    # --- pdp ---
    pdp_data = extract_configmap_data(manifest, "pdp-main")
    (out_dir / "pdp.yaml").write_text(pdp_data["config.yaml"])
    print(f"wrote {out_dir / 'pdp.yaml'}")

    # --- secrets (mirrors config/secret_generator_template.yaml's randAlphaNum 32) ---
    secrets_dir = Path(args.secrets_dir)
    for name in WALLET_BACKEND_SECRETS:
        gen_secret(secrets_dir / name)
    print(f"secrets ready in {secrets_dir} ({', '.join(WALLET_BACKEND_SECRETS)})")


if __name__ == "__main__":
    main()
