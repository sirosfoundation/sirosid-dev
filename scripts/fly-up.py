#!/usr/bin/env python3
"""Spin up a named Fly.io environment for sirosid-dev: `make fly-up ENV=<name>`.

Deploys 10 Fly apps under sirosfoundation, prefixed `sirosid-<env>-`: mongodb,
mini-oidc, vc-registry, vc-issuer, vc-verifier, vc-apigw, pdp, wallet-backend,
wallet-proxy, wallet-frontend - see scripts/fly_common.py's COMPONENTS table
and scripts/render-helm-config.py's module docstring for the overall design
(images pulled straight from helm-charts/siros-id-stack/values.yaml, config
rendered from the same chart, no local Docker build).

Supports both web (wallet-frontend) and native app clients:
- Android: a single environment can authenticate a *mix* of several debug
  builds and Play Store builds at once - `--android-app package=fingerprint`
  is repeatable (one entry per package+signing-key combination; the same
  package can appear more than once with different fingerprints, e.g. a
  debug key and a Play Store upload key), plus whatever's auto-read from
  .env.android if present, plus the production fingerprints helm-charts'
  wellknownAndroidPackageNamesAndFingerprints already carries. Every
  identity is wired into BOTH wallet-proxy's /.well-known/assetlinks.json
  (Android's OS-level Digital Asset Links check) AND wallet-backend's
  rp_origins (the server-side WebAuthn accept-list) - both are required,
  one without the other passes the OS check but still fails the actual
  passkey ceremony (see android_identities()).
- iOS: wallet-proxy also serves apple-app-site-association (Associated
  Domains / passkey webcredentials at the RP ID's own domain), and
  wallet-frontend gets WELLKNOWN_APPLE_APPIDS set so it serves its own copy
  too (Universal Links on its own domain).
- OIDC-backed issuance (pid/pid_1_5/pid_1_8/ehic scopes): mini-oidc stands
  in for a real government/eIDAS IdP - without it, vc-apigw's OIDC auth
  provider pointed at an Android-emulator-only bridge address, unreachable
  by any client (web or native) once actually deployed on Fly.

Multiple developers can each run their own fully isolated environment
simultaneously (`make fly-up ENV=alice`, `make fly-up ENV=bob`) - app names
are prefixed per-env, so nothing collides. `--images` (or `make fly-up
ENV=alice IMAGES=...`) lets one environment pin different image tags per
component than another - e.g. testing your own branch build of
wallet-backend - without touching values-fly.yaml (which would affect every
environment) or the shared helm-charts pin.

No `depends_on` equivalent on Fly - components are deployed strictly in
COMPONENTS order and each `fly deploy` blocks on its own health checks
(fly.toml `[[http_service.checks]]`) before the next one starts.

Mongo has no persistent volume (ephemeral - data resets on stop/restart, per
the tenant's own decision for what's meant to be a throwaway demo/test
environment). PKI (vc-services signing keys) and the WebAuthn AS signing key
are generated fresh per environment rather than reusing sirosid-dev's shared
local dev PKI, since Fly environments are reachable over the public internet.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fly_common import (  # noqa: E402
    COMPONENTS, FLY_ORG, aasa_json, app_name, app_url, assetlinks_json,
    ensure_app, ensure_running, ensure_secret, mini_oidc_config, wallet_proxy_conf, write_fly_toml,
)
from helm_render_lib import (  # noqa: E402
    extract_configmap_data, extract_deployment_image, extract_init_container_image,
    extract_mongo_version, helm_template, load_manifest_docs,
)

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run(cmd, check=True, **kwargs)


def render_configs(env: str, chart_dir: Path, android_apk_key_hashes: list):
    cmd = [sys.executable, "scripts/render-helm-config.py", "--target", "fly", "--env", env,
           "--chart-dir", str(chart_dir)]
    # Adds each identity's own origin to wallet-backend's rp_origins (the
    # server-side WebAuthn accept-list) - NOT the same thing as
    # assetlinks.json (Android's OS-level Digital Asset Links check,
    # generated separately in generate_android_assets()). Both are required
    # for a debug/sideloaded build's passkeys to actually work; only the
    # assetlinks.json half was wired in the first pass at this.
    for apk_key_hash in android_apk_key_hashes:
        cmd += ["--android-apk-key-hash", apk_key_hash]
    run(cmd, cwd=SIROSID_DEV_ROOT)
    run([sys.executable, "scripts/patch-vc-config-fly.py", "--env", env], cwd=SIROSID_DEV_ROOT)


def generate_pki(env: str) -> Path:
    pki_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{env}" / "vc-pki"
    env_vars = {**os.environ, "PKI_DIR_OVERRIDE": str(pki_dir)}
    run(["bash", "./create-pki.sh"], cwd=SIROSID_DEV_ROOT / "fixtures", env=env_vars)
    return pki_dir


def generate_android_assets(docs: list, out_dir: Path, identities: list) -> Path:
    fe_data = extract_configmap_data(docs, "wallet-frontend-main")
    wellknown = fe_data.get("wellknownAndroidPackageNamesAndFingerprints", "")
    extra = [(i["package"], i["fingerprint_hex"]) for i in identities]
    assetlinks_path = out_dir / "assetlinks.json"
    assetlinks_path.write_text(assetlinks_json(wellknown, extra_identities=extra))
    return assetlinks_path


def generate_ios_assets(docs: list, out_dir: Path) -> Path:
    fe_data = extract_configmap_data(docs, "wallet-frontend-main")
    wellknown = fe_data.get("wellknownAppleAppIds", "")
    aasa_path = out_dir / "apple-app-site-association"
    aasa_path.write_text(aasa_json(wellknown))
    return aasa_path


def _hex_to_apk_key_hash(fingerprint_hex: str) -> str:
    """keytool -list -v prints colon-separated hex; rp_origins needs
    base64url (no padding) - same conversion setup-android.sh does for the
    local docker-compose path: strip colons, hex-decode, base64url-encode."""
    import base64
    raw = bytes.fromhex(fingerprint_hex.replace(":", ""))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _apk_key_hash_to_hex(apk_key_hash: str) -> str:
    """Opposite direction - .env.android stores base64url; assetlinks.json
    needs colon-separated hex."""
    import base64
    raw = base64.urlsafe_b64decode(apk_key_hash + "=" * (-len(apk_key_hash) % 4))
    return ":".join(f"{b:02X}" for b in raw)


def android_identities(args) -> list:
    """Every repeatable --android-app package=fingerprint entry, PLUS
    .env.android (written by `make android-setup` - APK_KEY_HASH/
    ANDROID_PACKAGE) if present, combined - not either/or, since one Fly
    environment is meant to authenticate a *mix* of several debug builds and
    Play Store builds at once. The same package can appear more than once
    (a debug key and a Play Store upload key for the same app, say).

    Each returned dict has {"package", "apk_key_hash", "fingerprint_hex"} -
    two DIFFERENT encodings of the same cert, because assetlinks.json and
    wallet-backend's rp_origins each expect a different one:
    - assetlinks.json wants colon-separated SHA-256 hex (fingerprint_hex).
    - rp_origins wants `android:apk-key-hash:<base64url, no padding>`
      (apk_key_hash) - matching docker-compose.test.yml's
      `android:apk-key-hash:${APK_KEY_HASH:-...}` convention exactly.
    Registering only in assetlinks.json (Android's Digital Asset Links,
    proving the app belongs to this domain) without also adding it to
    rp_origins (wallet-backend's server-side WebAuthn accept-list) would
    satisfy the OS-level check but still fail the actual passkey ceremony -
    a real gap in this fly-up.py's first pass, caught when a user asked how
    RP origins actually get set for a debug build.
    """
    identities = []

    # --android-app is repeatable (nice for direct CLI use) AND each value
    # may itself be a comma-separated list (nice for `make fly-up
    # ANDROID_APPS=pkg1=fp1,pkg2=fp2` - Make can't easily repeat a flag).
    raw_entries = [e for group in (args.android_app or []) for e in group.split(",") if e.strip()]
    for entry in raw_entries:
        if "=" not in entry:
            raise SystemExit(f"--android-app entry {entry!r} must be package=fingerprint (colon-hex)")
        package, fingerprint_hex = entry.split("=", 1)
        identities.append({
            "package": package.strip(),
            "fingerprint_hex": fingerprint_hex.strip(),
            "apk_key_hash": _hex_to_apk_key_hash(fingerprint_hex.strip()),
        })

    env_android = SIROSID_DEV_ROOT / ".env.android"
    if env_android.exists():
        values = {}
        for line in env_android.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"')
        package, apk_key_hash = values.get("ANDROID_PACKAGE"), values.get("APK_KEY_HASH")
        if package and apk_key_hash:
            print(f".env.android found - adding debug identity for {package}")
            identities.append({
                "package": package,
                "apk_key_hash": apk_key_hash,
                "fingerprint_hex": _apk_key_hash_to_hex(apk_key_hash),
            })

    return identities


def deploy_component(env: str, comp: dict, docs: list, mongo_version: str, out_dir: Path, pki_dir: Path,
                      assetlinks_path: Path, aasa_path: Path, image_overrides: dict):
    name = comp["name"]
    app = app_name(env, name)
    ensure_app(app)

    if name in image_overrides:
        # Explicit --images override (e.g. a dev testing their own branch
        # build of one component) always wins, regardless of where the image
        # would otherwise come from - one override point covering all 10
        # components uniformly, not a Helm-values override for some and a
        # separate CLI flag for the two non-Helm ones (mongodb, mini-oidc).
        image = image_overrides[name]
    elif "image_from_helm_deployment" in comp:
        deployment = comp["image_from_helm_deployment"]
        image = (extract_init_container_image(docs, deployment)
                 if name == "wallet-frontend" else extract_deployment_image(docs, deployment))
    else:
        image = comp["image"].format(mongo_version=mongo_version)

    public_ports = [p["internal"] for p in comp["ports"] if p["public"]]
    primary_public_port = public_ports[0] if public_ports else None

    toml_path = out_dir / f"{name}.fly.toml"
    process_cmd = {
        "pdp": "--config /main-config/config.yaml",
        "wallet-backend": "--mode=all --config=/app/config.yaml --registry-config=/app/registry.yaml",
        # mongod binds 0.0.0.0 (IPv4) by default even with --bind_ip_all;
        # Fly's 6PN private network (`.internal` DNS) is IPv6-only, so other
        # apps get "connection refused" dialing it unless IPv6 is explicitly
        # enabled (off by default in mongod) - confirmed by checking
        # /proc/net/tcp{,6} on the machine itself: only tcp4 had a listener.
        "mongodb": "mongod --bind_ip_all --ipv6",
        # ENTRYPOINT [] in mini-oidc's Dockerfile - CMD must be the full
        # binary path (docker-compose.vc-services.yml's `command: ["/usr/local/bin/op"]`
        # for the same reason).
        "mini-oidc": "/usr/local/bin/op",
    }.get(name)
    # vc-registry builds a full in-memory slice of section_size (default 1M)
    # decoy docs before a single bulk InsertMany on first boot (empty status
    # list collection) - confirmed OOM-killed at the default 256MB machine
    # size (anon-rss >150MB and climbing). Also trimmed via
    # patch-vc-config-fly.py's section_size override, but bumping memory too
    # since other vc-services may have similar headroom needs under load.
    memory_mb = 1024 if name == "vc-registry" else 256
    write_fly_toml(toml_path, app, primary_public_port, process_cmd=process_cmd,
                    health_check_path=comp["checks"], memory_mb=memory_mb)

    deploy_args = ["deploy", "-a", app, "-c", str(toml_path), "-i", image,
                   "--ha=false", "--strategy", "immediate", "--yes"]

    if name == "mini-oidc":
        config_path = out_dir / "mini-oidc-config.yaml"
        config_path.write_text(mini_oidc_config(env))
        apigw_redirect = f"{app_url(env, 'vc-apigw')}/oidcrp/callback"
        deploy_args += [
            # mini-oidc's own binary defaults CONFIG_FILE to the relative
            # path configs/config.yaml, which doesn't exist in the image at
            # that cwd - docker-compose.vc-services.yml sets this explicitly
            # too, easy to miss since the file mounted below already lives
            # at the "right" path and looks like it should just be picked up.
            "--env", "CONFIG_FILE=/etc/mini-oidc/configs/config.production.yaml",
            "--env", "USERS_FILE=/etc/mini-oidc/users.yaml",
            "--env", f"ISSUER={app_url(env, 'mini-oidc')}",
            # RP_BASE_URL/CLIENT_ID only back the mini-oidc-rp test client
            # (mini_oidc_config's first `clients` entry) - mini-oidc-rp itself
            # isn't deployed here (a standalone harness for testing the OP,
            # not part of vc-apigw's real flow), so these are unused but must
            # be set to something for ${VAR} expansion to produce valid YAML.
            "--env", f"RP_BASE_URL={app_url(env, 'mini-oidc')}",
            "--env", "CLIENT_ID=mini-oidc-rp",
            # Must match apigw's auth_providers.oidc.redirect_uri, set to the
            # same value in patch-vc-config-fly.py.
            "--env", f"APIGW_REDIRECT_URI={apigw_redirect}",
            "--file-local", f"/etc/mini-oidc/configs/config.production.yaml={config_path}",
        ]
    elif name == "vc-registry":
        deploy_args += _vc_service_files(out_dir, pki_dir, metadata=False)
    elif name == "vc-issuer":
        deploy_args += _vc_service_files(out_dir, pki_dir, metadata=True)
    elif name == "vc-verifier":
        deploy_args += _vc_service_files(out_dir, pki_dir, metadata=True, presentation_requests=True)
    elif name == "vc-apigw":
        deploy_args += _vc_service_files(out_dir, pki_dir, metadata=True)
    elif name == "pdp":
        deploy_args += ["--file-local", f"/main-config/config.yaml={out_dir / 'pdp.yaml'}"]
    elif name == "wallet-backend":
        deploy_args += [
            "--file-local", f"/app/config.yaml={out_dir / 'wallet-backend.yaml'}",
            "--file-local", f"/app/registry.yaml={out_dir / 'wallet-backend-registry.yaml'}",
            "--file-literal", "/vctms/.keep=ok",
        ]
        ensure_secret(app, "jwtSecret", _rand_secret())
        ensure_secret(app, "adminToken", _rand_secret())
        deploy_args += [
            "--file-secret", "/main-secrets/jwtSecret=jwtSecret",
            "--file-secret", "/main-secrets/adminToken=adminToken",
        ]
    elif name == "wallet-proxy":
        conf_path = out_dir / "wallet-proxy.conf"
        conf_path.write_text(wallet_proxy_conf(env))
        deploy_args += [
            "--file-local", f"/etc/nginx/conf.d/default.conf={conf_path}",
            "--file-local", f"/etc/nginx/well-known/assetlinks.json={assetlinks_path}",
            "--file-local", f"/etc/nginx/well-known/apple-app-site-association={aasa_path}",
        ]
    elif name == "wallet-frontend":
        deploy_args += _wallet_frontend_env(env, docs)

    run(["flyctl"] + deploy_args, cwd=SIROSID_DEV_ROOT)
    ensure_running(app)

    if name == "mongodb":
        # mongodb has no [[services]] block (deliberately not publicly exposed),
        # so `fly deploy` has no health check to block on and returns as soon
        # as the machine reports "started" - but Fly's 6PN private DNS/routing
        # for a brand-new machine can take a few seconds to propagate, and
        # vc-registry (deployed right after) connects to it immediately.
        # Observed once as a real "connection refused" crash-loop in vc-registry
        # despite mongod already logging "Waiting for connections" - this fixed
        # delay is a pragmatic buffer, not a substitute for a real check
        # (mongodb can't have an http_service check without also making it public).
        time.sleep(10)

    if primary_public_port is not None:
        print(f"{name}: {app_url(env, name)}")


def _vc_service_files(out_dir: Path, pki_dir: Path, metadata: bool, presentation_requests: bool = False) -> list:
    args = [
        "--env", "VC_CONFIG_YAML=/config.yaml",
        "--env", "SSL_CERT_FILE=/pki/rootCA.crt",
        "--file-local", f"/config.yaml={out_dir / 'vc-config.yaml'}",
        "--file-local", f"/pki/rootCA.crt={pki_dir / 'rootCA.crt'}",
        "--file-local", f"/pki/signing_ec_private.pem={pki_dir / 'signing_ec_private.pem'}",
        "--file-local", f"/pki/signing_ec_chain.pem={pki_dir / 'signing_ec_chain.pem'}",
    ]
    if metadata:
        metadata_dir = SIROSID_DEV_ROOT / "fixtures" / "vc-metadata"
        for f in sorted(metadata_dir.glob("*.json")):
            args += ["--file-local", f"/metadata/{f.name}={f}"]
    if presentation_requests:
        pr_dir = SIROSID_DEV_ROOT / "fixtures" / "vc-presentation-requests"
        for f in sorted(pr_dir.glob("*.yaml")):
            args += ["--file-local", f"/presentation_requests/{f.name}={f}"]
    return args


def _wallet_frontend_env(env: str, docs: list) -> list:
    proxy = app_url(env, "wallet-proxy")
    frontend = app_url(env, "wallet-frontend")
    fe_data = extract_configmap_data(docs, "wallet-frontend-main")
    values = {
        "WALLET_BACKEND_URL": proxy,
        "WALLET_ENGINE_URL": proxy,
        "WEBAUTHN_RPID": f"sirosid-{env}-wallet-proxy.fly.dev",
        "STATIC_PUBLIC_URL": frontend,
        # For Universal Links on wallet-frontend's own domain (separate from
        # the AASA wallet-proxy serves for the passkey RP ID - see
        # fly_common.wallet_proxy_conf). Helm already sets this in production
        # (04-wallet-frontend.yaml:185); this Fly deployment simply never had
        # set it before now.
        "WELLKNOWN_APPLE_APPIDS": fe_data.get("wellknownAppleAppIds", ""),
        "STATIC_NAME": f"SIROS ID (fly-{env})",
        "OPENID4VCI_REDIRECT_URI": f"{frontend}/",
        "VCT_REGISTRY_URL": f"{proxy}/registry/type-metadata",
        "TRANSPORT_PREFERENCE": "websocket",
        "ALLOWED_TRANSPORTS": "http,websocket,wmp",
        "LOG_LEVEL": "info",
        "DISPLAY_CONSOLE": "false",
        "LOGIN_WITH_PASSWORD": "false",
        "DID_KEY_VERSION": "jwk_jcs-pub",
        "OPENID4VCI_PROOF_TYPE_PRECEDENCE": "attestation,jwt",
        "OPENID4VP_SAN_DNS_CHECK": "false",
        "OPENID4VP_SAN_DNS_CHECK_SSL_CERTS": "false",
        "DELEGATE_TRUST_TO_BACKEND": "true",
        "MULTI_LANGUAGE_DISPLAY": "true",
        "DISPLAY_ISSUANCE_WARNINGS": "false",
        "BASE_PATH": "/id/default/",
    }
    args = []
    for k, v in values.items():
        args += ["--env", f"{k}={v}"]
    return args


def _rand_secret(length: int = 32) -> str:
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT.parent / "helm-charts" / "siros-id-stack"))
    parser.add_argument("--android-app", action="append",
                         help="package=fingerprint (SHA-256, colon-separated hex, as printed by "
                              "`keytool -list -v`) for a debug build or Play Store signing key to "
                              "authenticate against this environment, in addition to the production "
                              "fingerprints helm-charts already carries. Repeatable - pass it once per "
                              "package+key combination (the same package can appear more than once, "
                              "e.g. a debug key and a Play Store upload key). .env.android (from "
                              "`make android-setup`) is always included too, alongside any --android-app "
                              "entries, not instead of them.")
    parser.add_argument("--images", default="",
                         help="Comma-separated component=image overrides for this environment only "
                              "(e.g. 'wallet-backend=ghcr.io/sirosfoundation/go-wallet-backend:pr-123'), "
                              "so two developers running their own ENV=<name> can each pin different "
                              "versions without touching values-fly.yaml or colliding with each other. "
                              "Component names match scripts/fly_common.py's COMPONENTS list.")
    args = parser.parse_args()

    image_overrides = {}
    for pair in args.images.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"--images entry {pair!r} must be component=image")
        component, image = pair.split("=", 1)
        component = component.strip()
        if component not in {c["name"] for c in COMPONENTS}:
            raise SystemExit(f"--images: unknown component {component!r} - see fly_common.COMPONENTS")
        image_overrides[component] = image.strip()

    if not shutil.which("flyctl"):
        raise SystemExit("flyctl not found - install it first (https://fly.io/docs/flyctl/install/)")

    chart_dir = Path(args.chart_dir)
    out_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}"
    out_dir.mkdir(parents=True, exist_ok=True)

    identities = android_identities(args)

    print(f"=== Rendering config for environment '{args.env}' ===")
    render_configs(args.env, chart_dir, [i["apk_key_hash"] for i in identities])

    print(f"=== Generating per-environment PKI ===")
    pki_dir = generate_pki(args.env)

    # Second helm template pass to pull image refs + mongo version + the
    # wallet-frontend ConfigMap's Android/iOS wellknown values -
    # render-helm-config.py only extracts wallet-backend/pdp, not the full manifest.
    values_files = [SIROSID_DEV_ROOT / "values-fly.yaml", out_dir / "values.generated.yaml"]
    manifest = helm_template(chart_dir, values_files, f"sirosid-{args.env}")
    docs = load_manifest_docs(manifest)
    mongo_version = extract_mongo_version(docs)

    print(f"=== Generating Android assetlinks.json ===")
    assetlinks_path = generate_android_assets(docs, out_dir, identities)

    print(f"=== Generating iOS apple-app-site-association ===")
    aasa_path = generate_ios_assets(docs, out_dir)

    if image_overrides:
        print(f"=== Image overrides for this environment: {image_overrides} ===")

    print(f"=== Deploying {len(COMPONENTS)} apps to Fly (org: {FLY_ORG}) ===")
    for comp in COMPONENTS:
        print(f"--- {comp['name']} ---")
        deploy_component(args.env, comp, docs, mongo_version, out_dir, pki_dir, assetlinks_path, aasa_path,
                          image_overrides)

    print()
    print(f"=== Environment '{args.env}' is up ===")
    for comp in COMPONENTS:
        if any(p["public"] for p in comp["ports"]):
            print(f"  {comp['name']}: {app_url(args.env, comp['name'])}")
    print()
    print(f"Tear down with: make fly-down ENV={args.env}")


if __name__ == "__main__":
    main()
