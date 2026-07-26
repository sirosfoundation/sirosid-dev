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
  builds and Play Store builds at once, sourced from scripts/android_apps.py
  (shared with local docker-compose testing - see its module docstring for
  the full precedence: --android-app flags / ANDROID_APPS, then
  .android-apps, then .env.android), plus the production fingerprints
  helm-charts' wellknownAndroidPackageNamesAndFingerprints already carries.
  Every identity is wired into BOTH wallet-proxy's
  /.well-known/assetlinks.json (Android's OS-level Digital Asset Links
  check) AND wallet-backend's rp_origins (the server-side WebAuthn
  accept-list) - both are required, one without the other passes the OS
  check but still fails the actual passkey ceremony.
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
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from android_apps import load_android_apps  # noqa: E402
from fly_common import (  # noqa: E402
    COMPONENTS, FLY_ORG, MINI_OIDC_APIGW_CLIENT_ID, MINI_OIDC_APIGW_CLIENT_SECRET,
    aasa_json, app_exists, app_name, app_url, assetlinks_json,
    ensure_app, ensure_running, ensure_secret, existing_secret_names, mini_oidc_config, network_name,
    wait_for_checks, wallet_proxy_conf, write_fly_toml,
)
from helm_render_lib import (  # noqa: E402
    extract_configmap_data, extract_deployment_image, extract_init_container_image, extract_mongo_version,
)

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

# render-helm-config.py's filename has a hyphen (matches `make render-helm-config`
# / long-standing CLI convention across this repo), so it can't be a normal
# `import` target - loaded by path instead, same module singleton either way.
import importlib.util  # noqa: E402
_render_helm_config_spec = importlib.util.spec_from_file_location(
    "render_helm_config", Path(__file__).resolve().parent / "render-helm-config.py")
render_helm_config = importlib.util.module_from_spec(_render_helm_config_spec)
_render_helm_config_spec.loader.exec_module(render_helm_config)
render = render_helm_config.render


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run(cmd, check=True, **kwargs)


def render_configs(env: str, chart_dir: Path, android_apk_key_hashes: list, mongo_password: str) -> list:
    """Calls render-helm-config.py's render() in-process (not a subprocess) so
    its `helm template` output can be reused below for image refs/mongo
    version/wellknown values too - previously a second, independent
    `helm template` invocation duplicated this same render.
    """
    # Adds each identity's own origin to wallet-backend's rp_origins (the
    # server-side WebAuthn accept-list) - NOT the same thing as
    # assetlinks.json (Android's OS-level Digital Asset Links check,
    # generated separately in generate_android_assets()). Both are required
    # for a debug/sideloaded build's passkeys to actually work; only the
    # assetlinks.json half was wired in the first pass at this.
    docs = render("fly", chart_dir, env=env, android_apk_key_hashes=android_apk_key_hashes,
                   out_dir=SIROSID_DEV_ROOT / "fixtures" / "rendered", mongo_password=mongo_password)
    run([sys.executable, "scripts/patch-vc-config-fly.py", "--env", env, "--mongo-password", mongo_password],
        cwd=SIROSID_DEV_ROOT)
    return docs


def check_pki_consistency(env: str, pki_dir: Path):
    """Guards against a real mismatch scenario: fixtures/create-pki.sh's own
    idempotency is purely local-file-based (skips regeneration only if
    signing_ec_private.pem already exists in pki_dir - see that script) and
    has no way to know a Fly secret already exists remotely. If the local PKI
    cache is missing - e.g. a different machine/checkout, or after `make
    clean` - but this environment's vc-registry app already has a
    vcSigningKey secret from a previous deploy, proceeding would generate a
    BRAND NEW keypair and deploy its rootCA/chain (--file-local, always
    freshly written) alongside the OLD private key (ensure_secret never
    rotates an already-set secret) - a real chain/key mismatch that breaks
    verification of anything this environment issues afterwards.
    """
    if (pki_dir / "signing_ec_private.pem").exists():
        return  # create-pki.sh will reuse it as-is - no risk of a mismatch
    app = app_name(env, "vc-registry")
    if not app_exists(app):
        return  # brand-new environment - nothing to mismatch against yet
    if "vcSigningKey" in existing_secret_names(app):
        raise SystemExit(
            f"Refusing to continue: no local PKI cache found at {pki_dir}, but "
            f"{app} already has a signing-key secret from a previous deploy. "
            "Generating a fresh keypair now would deploy a mismatched "
            "rootCA/cert chain alongside the OLD private key (Fly secrets "
            "can't be read back to keep them in sync), breaking verification "
            "of anything already issued by this environment.\n"
            "Either restore the original fixtures/rendered/fly-"
            f"{env}/vc-pki directory (e.g. from wherever this environment was "
            f"first deployed), or fully rotate by tearing it down first: "
            f"make fly-down ENV={env}"
        )


def generate_pki(env: str) -> Path:
    pki_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{env}" / "vc-pki"
    check_pki_consistency(env, pki_dir)
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


def deploy_component(env: str, comp: dict, docs: list, mongo_version: str, out_dir: Path, pki_dir: Path,
                      assetlinks_path: Path, aasa_path: Path, image_overrides: dict, mongo_password: str):
    name = comp["name"]
    app = app_name(env, name)
    ensure_app(app, network=network_name(env))

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
        "mongodb": "mongod --bind_ip_all --ipv6 --auth",
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
    #
    # mongodb: the official image's docker-entrypoint.sh bootstraps
    # MONGO_INITDB_ROOT_USERNAME/PASSWORD by shelling out to `mongosh`
    # (Node.js-based, ~80MB RSS on its own) alongside mongod itself already
    # running - confirmed OOM-killed in a repeating loop at 256MB (`dmesg`:
    # "Out of memory: Killed process ... (mongosh)"), which silently never
    # let root-user creation complete. Not needed before mongo auth was added
    # (no entrypoint init logic runs at all with no MONGO_INITDB_ROOT_* set).
    memory_mb = {"vc-registry": 1024, "mongodb": 512}.get(name, 256)
    write_fly_toml(toml_path, app, primary_public_port, process_cmd=process_cmd,
                    health_check_path=comp["checks"], memory_mb=memory_mb,
                    internal_check=comp.get("internal_check"))

    deploy_args = ["deploy", "-a", app, "-c", str(toml_path), "-i", image,
                   "--ha=false", "--strategy", "immediate", "--yes"]

    if name == "mongodb":
        # force=True: mongo has no persistent volume, so every deploy starts
        # from empty data anyway - regenerating the root password on every
        # run (rather than trying to preserve one across runs, which we
        # couldn't even read back from Fly's write-only secret store) is both
        # simpler and strictly safe, since there's no old data it would need
        # to keep matching.
        ensure_secret(app, "mongoRootPassword", mongo_password, force=True)
        deploy_args += [
            "--env", "MONGO_INITDB_ROOT_USERNAME=root",
            "--env", "MONGO_INITDB_ROOT_PASSWORD_FILE=/run/secrets/mongoRootPassword",
            "--file-secret", "/run/secrets/mongoRootPassword=mongoRootPassword",
        ]
    elif name == "mini-oidc":
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
            # Explicit, not relying on mini_oidc_config()'s own defaults to
            # coincidentally match what patch-vc-config-fly.py sets on apigw's
            # side - see fly_common.MINI_OIDC_APIGW_CLIENT_ID/_SECRET.
            "--env", f"APIGW_CLIENT_ID={MINI_OIDC_APIGW_CLIENT_ID}",
            "--env", f"APIGW_CLIENT_SECRET={MINI_OIDC_APIGW_CLIENT_SECRET}",
            "--file-local", f"/etc/mini-oidc/configs/config.production.yaml={config_path}",
        ]
    elif name == "vc-registry":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, metadata=False)
    elif name == "vc-issuer":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, metadata=True)
    elif name == "vc-verifier":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, metadata=True, presentation_requests=True)
    elif name == "vc-apigw":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, metadata=True)
    elif name == "pdp":
        deploy_args += ["--file-local", f"/main-config/config.yaml={out_dir / 'pdp.yaml'}"]
    elif name == "wallet-backend":
        deploy_args += [
            "--file-local", f"/app/config.yaml={out_dir / 'wallet-backend.yaml'}",
            "--file-local", f"/app/registry.yaml={out_dir / 'wallet-backend-registry.yaml'}",
            "--file-literal", "/vctms/.keep=ok",
        ]
        ensure_secret(app, "jwtSecret", _persistent_secret(out_dir, "jwtSecret"))
        ensure_secret(app, "adminToken", _persistent_secret(out_dir, "adminToken"))
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

    if "internal_check" in comp:
        # Components with no public [http_service] (mongodb, vc-issuer, pdp,
        # wallet-backend) get no health check to block a plain `fly deploy`
        # on - it returns as soon as the machine reports "started," before
        # confirming the process inside is actually ready. The very next
        # component in COMPONENTS order calls several of these directly over
        # 6PN (vc-registry -> mongodb, vc-verifier/vc-apigw -> vc-issuer,
        # wallet-backend -> pdp, wallet-proxy -> wallet-backend), so wait for
        # write_fly_toml's machine-level check to actually report healthy
        # first (was a mongodb-only fixed sleep, generalized after a real
        # "connection refused" crash-loop was observed there once).
        wait_for_checks(app)

    if primary_public_port is not None:
        print(f"{name}: {app_url(env, name)}")


def _vc_service_files(app: str, out_dir: Path, pki_dir: Path, metadata: bool,
                       presentation_requests: bool = False) -> list:
    # The private key authenticates every credential this environment issues -
    # a Fly secret (encrypted, write-only), not a --file-local file (stored
    # in the app's plain config/release object, readable via e.g. `flyctl
    # config show`). rootCA.crt/signing_ec_chain.pem are public certs, no
    # confidentiality need, so they stay --file-local. See
    # check_pki_consistency() for why ensure_secret's default
    # never-rotate-if-already-set behavior is safe here specifically.
    ensure_secret(app, "vcSigningKey", (pki_dir / "signing_ec_private.pem").read_text())
    args = [
        "--env", "VC_CONFIG_YAML=/config.yaml",
        "--env", "SSL_CERT_FILE=/pki/rootCA.crt",
        "--file-local", f"/config.yaml={out_dir / 'vc-config.yaml'}",
        "--file-local", f"/pki/rootCA.crt={pki_dir / 'rootCA.crt'}",
        "--file-secret", "/pki/signing_ec_private.pem=vcSigningKey",
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


def _persistent_secret(out_dir: Path, name: str) -> str:
    """Unlike mongodb's password (regenerated every run - mongo has no
    persistent volume, so there's no old state to match), wallet-backend's
    jwtSecret/adminToken back a long-lived app (real user sessions, and now
    also register_vc_services()'s own Bearer auth) - `ensure_secret()` already
    never rotates an already-set Fly secret, but Fly secrets can't be read
    back, so without this, a rerun would generate a brand-new value that's
    silently discarded (ensure_secret sees the OLD one still set and skips)
    while nothing else knows what the OLD one actually was. Cached the same
    way render-helm-config.py's gen_secret() does for the compose target's
    secrets, just scoped to this Fly environment's own out_dir instead of the
    shared fixtures/rendered-secrets/.
    """
    path = out_dir / name
    if path.exists():
        return path.read_text().strip()
    value = _rand_secret()
    path.write_text(value)
    return value


def register_vc_services(env: str, admin_token: str):
    """Mirrors the local Makefile's register-vc-services target: without
    this, wallet-backend has zero registered issuers/verifiers even though
    every VC service is up and reachable - PDP's whitelist (a separate,
    orthogonal trust-policy mechanism, see build_fly_values_overlay()) governs
    who's TRUSTED to issue/verify, it doesn't populate the wallet's own
    "available issuers/verifiers" list. Tenant "default" is
    go-wallet-backend's domain.DefaultTenantID, auto-initialized on startup -
    same one the local Makefile uses, not Fly-specific.

    Retries for a while since wallet-proxy's public DNS/TLS can take a few
    seconds to become reachable right after its own deploy returns.
    """
    proxy_url = app_url(env, "wallet-proxy")
    apigw_url = app_url(env, "vc-apigw")
    verifier_url = app_url(env, "vc-verifier")

    def post(path: str, body: dict):
        req = urllib.request.Request(
            f"{proxy_url}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status

    last_err = None
    for _ in range(30):
        try:
            post("/admin/tenants/default/issuers", {"credential_issuer_identifier": apigw_url, "visible": True})
            post("/admin/tenants/default/verifiers", {"name": "VC Verifier", "url": verifier_url})
            print(f"registered vc-apigw ({apigw_url}) and vc-verifier ({verifier_url}) "
                  "with wallet-backend's default tenant")
            return
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(2)
    print(f"WARNING: could not register VC services with wallet-backend after retries ({last_err}) - "
          "the wallet UI may show no available issuers/verifiers. Retry manually once the environment "
          f"is up: POST {proxy_url}/admin/tenants/default/issuers|verifiers "
          "with 'Authorization: Bearer <adminToken>'.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT.parent / "helm-charts" / "siros-id-stack"))
    parser.add_argument("--android-app", action="append",
                         help="package=fingerprint (SHA-256, colon-separated hex, as printed by "
                              "`keytool -list -v`) for a debug build or Play Store signing key to "
                              "authenticate against this environment, in addition to the production "
                              "fingerprints helm-charts already carries. Repeatable, and each value "
                              "may be a comma-separated list. The same package can appear more than "
                              "once (e.g. a debug key and a Play Store upload key). Combined with - "
                              "not instead of - .android-apps and .env.android if present; see "
                              "scripts/android_apps.py for the full precedence.")
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

    identities = load_android_apps(extra=args.android_app)
    # Fresh every run, not persisted/reused - see ensure_secret(force=True)'s
    # docstring for why that's fine specifically for mongo (no persistent
    # volume, so there's no old data a stale password would need to match).
    mongo_password = _rand_secret()

    print(f"=== Rendering config for environment '{args.env}' ===")
    # docs is the full rendered manifest (not just wallet-backend/pdp) -
    # reused below for image refs + mongo version + wallet-frontend's
    # Android/iOS wellknown values, instead of a second `helm template` call.
    docs = render_configs(args.env, chart_dir, [i["apk_key_hash"] for i in identities], mongo_password)
    mongo_version = extract_mongo_version(docs)

    print(f"=== Generating per-environment PKI ===")
    pki_dir = generate_pki(args.env)

    print(f"=== Generating Android assetlinks.json ===")
    assetlinks_path = generate_android_assets(docs, out_dir, identities)

    print(f"=== Generating iOS apple-app-site-association ===")
    aasa_path = generate_ios_assets(docs, out_dir)

    if image_overrides:
        print(f"=== Image overrides for this environment: {image_overrides} ===")

    print(f"=== Deploying {len(COMPONENTS)} apps to Fly (org: {FLY_ORG}) ===")
    deployed = []
    try:
        for comp in COMPONENTS:
            print(f"--- {comp['name']} ---")
            deploy_component(args.env, comp, docs, mongo_version, out_dir, pki_dir, assetlinks_path, aasa_path,
                              image_overrides, mongo_password)
            deployed.append(comp["name"])
    except subprocess.CalledProcessError as e:
        # No auto-rollback - components deployed so far are left running
        # (each is independently a fine, working app; only the *sequence* is
        # incomplete), since silently tearing down a partially-up environment
        # the operator may still want to inspect/debug is worse than leaving
        # it and saying so clearly.
        failed = COMPONENTS[len(deployed)]["name"]
        print(file=sys.stderr)
        print(f"=== Deploy failed at '{failed}' (exit {e.returncode}) ===", file=sys.stderr)
        print(f"Already deployed and left running: {', '.join(deployed) or '(none)'}", file=sys.stderr)
        print(f"Re-running 'make fly-up ENV={args.env}' redeploys everything from the top "
              "(safe - already-succeeded components are idempotent), or clean up with: "
              f"make fly-down ENV={args.env}", file=sys.stderr)
        raise SystemExit(1)

    print(f"=== Registering VC services with wallet-backend's default tenant ===")
    # Same value deploy_component()'s wallet-backend branch already wrote/read
    # via _persistent_secret() - guaranteed consistent, not a race (sequential).
    register_vc_services(args.env, _persistent_secret(out_dir, "adminToken"))

    print()
    print(f"=== Environment '{args.env}' is up ===")
    for comp in COMPONENTS:
        if any(p["public"] for p in comp["ports"]):
            print(f"  {comp['name']}: {app_url(args.env, comp['name'])}")
    print()
    print(f"Tear down with: make fly-down ENV={args.env}")


if __name__ == "__main__":
    main()
