#!/usr/bin/env python3
"""Spin up a named Fly.io environment for sirosid-dev: `make fly-up ENV=<name>`.

Deploys 10 Fly apps under sirosfoundation, prefixed `sirosid-<env>-`: mongodb,
mini-oidc, vc-registry, vc-issuer, vc-verifier, vc-apigw, pdp, wallet-backend,
wallet-proxy, wallet-frontend - see scripts/fly_common.py's COMPONENTS table
and scripts/render-helm-config.py's module docstring for the overall design
(images pulled straight from the siros-id-stack chart's values.yaml, config
rendered from the same chart, no local Docker build).

Supports both web (wallet-frontend) and native app clients:
- Android: a single environment can authenticate a *mix* of several debug
  builds and Play Store builds at once, sourced from scripts/android_apps.py
  (shared with local docker-compose testing - see its module docstring for
  the full precedence: --android-app flags / ANDROID_APPS, then
  .android-apps, then .env.android), plus the production fingerprints
  siros-id-stack's wellknownAndroidPackageNamesAndFingerprints already carries.
  Every identity is wired into BOTH wallet-proxy's
  /.well-known/assetlinks.json (Android's OS-level Digital Asset Links
  check) AND wallet-backend's rp_origins (the server-side WebAuthn
  accept-list) - both are required, one without the other passes the OS
  check but still fails the actual passkey ceremony.
- iOS: wallet-frontend gets WELLKNOWN_APPLE_APPIDS set, and its own image
  generates a complete apple-app-site-association from it (both applinks,
  for Universal Links, and webcredentials, for the passkey RP ID check) -
  served at wallet-frontend's own domain, which is also wallet-backend's
  rp_id (see render-helm-config.py's patch_wallet_backend_fly).
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
environment) or the shared siros-id-stack pin.

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
from env_config import load_environment_config, merge_images, merge_list  # noqa: E402
from fly_common import (  # noqa: E402
    COMPONENTS, CONFORMANCE_COMPONENTS, FLY_ORG, FLY_REGION_FALLBACK, detect_region,
    MINI_OIDC_APIGW_CLIENT_ID, MINI_OIDC_APIGW_CLIENT_SECRET,
    app_exists, app_name, app_url, assetlinks_json,
    ensure_app, ensure_running, ensure_secret, existing_secret_names, is_local_docker_image, machine_private_ip,
    mini_oidc_config, network_name, push_local_image, wait_for_checks, wallet_frontend_conf,
    wallet_frontend_dashboard_html, wallet_proxy_conf, write_fly_toml,
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


def render_configs(env: str, chart_dir: Path, android_apk_key_hashes: list, mongo_password: str,
                    conformance: bool = False, extra_trusted_issuers: list = None,
                    wallet_attestation: bool = False, extra_trusted_verifiers: list = None,
                    extra_trusted_verifier_roots: list = None, zk_circuits_sources: list = None,
                    rical_provider_url: str = None, rical_root_certificate_pem: str = None,
                    dc_api_enable: str = "", env_values: dict = None) -> list:
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
                   out_dir=SIROSID_DEV_ROOT / "fixtures" / "rendered", mongo_password=mongo_password,
                   conformance=conformance, extra_trusted_issuers=extra_trusted_issuers,
                   wallet_attestation=wallet_attestation, extra_trusted_verifiers=extra_trusted_verifiers,
                   extra_trusted_verifier_roots=extra_trusted_verifier_roots,
                   rical_provider_url=rical_provider_url,
                   rical_root_certificate_pem=rical_root_certificate_pem,
                   # The vc services' config comes out of this same render
                   # now, rather than a second pass over a hand-written
                   # fixtures/vc-config.yaml (scripts/patch-vc-config-fly.py,
                   # removed) - so a chart change reaches the issuer and
                   # verifier the way it already reached wallet-backend.
                   zk_circuits_sources=zk_circuits_sources,
                   dc_api_enable=dc_api_enable,
                   env_values=env_values)
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


def deploy_component(env: str, comp: dict, docs: list, mongo_version: str, out_dir: Path, pki_dir: Path,
                      assetlinks_path: Path, image_overrides: dict, mongo_password: str, conformance: bool = False,
                      wallet_attestation: bool = False, region: str = ""):
    name = comp["name"]
    app = app_name(env, name)
    ensure_app(app, network=network_name(env), allocate_public_ips=(name == "conformance"))

    if name in image_overrides:
        # Explicit --images override (e.g. a dev testing their own branch
        # build of one component) always wins, regardless of where the image
        # would otherwise come from - one override point covering all 10
        # components uniformly, not a Helm-values override for some and a
        # separate CLI flag for the two non-Helm ones (mongodb, mini-oidc).
        image = image_overrides[name]
        if is_local_docker_image(image):
            # A bare local build tag (e.g. what `make up REBUILD=yes` /
            # docker-compose already produced, like wallet-backend-e2e-test:local)
            # - push it into this app's own registry.fly.io namespace so `-i`
            # below can deploy it like any other ref, with no manual `docker
            # tag`/`docker push`/`flyctl auth docker` from the developer.
            print(f"{name}: {image!r} is a local Docker image - pushing to registry.fly.io/{app}")
            image = push_local_image(app, image)
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
        # Same IPv6 reasoning as mongodb, no --auth (see deploy_args below -
        # this one deliberately isn't authenticated, unlike the main mongodb).
        "conformance-mongodb": "mongod --bind_ip_all --ipv6",
        # ENTRYPOINT [] in mini-oidc's Dockerfile - CMD must be the full
        # binary path (docker-compose.vc-services.yml's `command: ["/usr/local/bin/op"]`
        # for the same reason).
        "mini-oidc": "/usr/local/bin/op",
    }.get(name)
    # vc-registry builds a full in-memory slice of section_size (default 1M)
    # decoy docs before a single bulk InsertMany on first boot (empty status
    # list collection) - confirmed OOM-killed at the default 256MB machine
    # size (anon-rss >150MB and climbing). Also trimmed via
    # issuer.registry.tokenStatusLists.sectionSize in values-base.yaml, but bumping memory too
    # since other vc-services may have similar headroom needs under load.
    #
    # mongodb: the official image's docker-entrypoint.sh bootstraps
    # MONGO_INITDB_ROOT_USERNAME/PASSWORD by shelling out to `mongosh`
    # (Node.js-based, ~80MB RSS on its own) alongside mongod itself already
    # running - confirmed OOM-killed in a repeating loop at 256MB (`dmesg`:
    # "Out of memory: Killed process ... (mongosh)"), which silently never
    # let root-user creation complete. Not needed before mongo auth was added
    # (no entrypoint init logic runs at all with no MONGO_INITDB_ROOT_* set).
    #
    # conformance-server: a Java/Spring Boot app (the whole OpenID conformance
    # suite plus all its test modules) - confirmed OOM-killed at 256MB
    # (dmesg: "Out of memory: Killed process ... (java)", anon-rss >150MB
    # just from startup, before finishing Spring context init).
    # conformance-runner runs headless Chromium via Playwright to drive the
    # actual wallet UI - a real browser rendering real pages needs more than
    # the 256MB default (same OOM risk already hit and fixed for
    # conformance-server's JVM process).
    #
    # vc-verifier (zknative builds only, but memory_mb has no build-tag
    # awareness so this applies unconditionally): a Vega ZK verifier/prover
    # key decompresses to ~110MB on its own, before any of the native
    # NeutronNova-folding verify computation's own working memory -
    # confirmed OOM-killed at 256MB (dmesg: "Out of memory: Killed process
    # ... (vc_service)") mid-request, surfacing to the wallet only as a
    # opaque 502 with an empty body. Bumped 2048->4096 2026-08-27: a live
    # gdc Vega presentation still OOM-killed the whole process
    # (exit_code=137, oom_killed=true) at 2048MB after multiple prior
    # verify calls in the same process - shared-cpu-1x caps at 2048MB, so
    # this also needs 2 cpus (see the `cpus` param below) to unlock the
    # higher ceiling.
    memory_mb = {
        "vc-registry": 1024, "mongodb": 512, "conformance-server": 1024, "conformance-runner": 1024,
        "vc-verifier": 4096,
    }.get(name, 256)
    cpus = 2 if name == "vc-verifier" else 1
    tcp_passthrough_port = None
    if name == "conformance":
        # See CONFORMANCE_COMPONENTS: this image's baked-in nginx.conf
        # insists on terminating its own self-signed TLS on 8443 - the
        # normal [http_service] path (Fly forwards plain HTTP to
        # internal_port) doesn't apply here, so override what the generic
        # "public port -> http_service" computation above would otherwise do.
        primary_public_port = None
        tcp_passthrough_port = 8443
    write_fly_toml(toml_path, app, primary_public_port, region=region, process_cmd=process_cmd,
                    health_check_path=comp["checks"], memory_mb=memory_mb, cpus=cpus,
                    internal_check=comp.get("internal_check"), tcp_passthrough_port=tcp_passthrough_port)

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
            # same value the rendered apigw config carries.
            "--env", f"APIGW_REDIRECT_URI={apigw_redirect}",
            # Explicit, not relying on mini_oidc_config()'s own defaults to
            # coincidentally match what the rendered config sets on apigw's
            # side - see fly_common.MINI_OIDC_APIGW_CLIENT_ID/_SECRET.
            "--env", f"APIGW_CLIENT_ID={MINI_OIDC_APIGW_CLIENT_ID}",
            "--env", f"APIGW_CLIENT_SECRET={MINI_OIDC_APIGW_CLIENT_SECRET}",
            "--file-local", f"/etc/mini-oidc/configs/config.production.yaml={config_path}",
        ]
    elif name == "vc-registry":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, "vc-registry", metadata=False)
    elif name == "vc-issuer":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, "vc-issuer", metadata=True)
    elif name == "vc-verifier":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, "vc-verifier", metadata=True, presentation_requests=True)
    elif name == "vc-apigw":
        deploy_args += _vc_service_files(app, out_dir, pki_dir, "vc-apigw", metadata=True, bootstrapping=True)
    elif name == "pdp":
        deploy_args += ["--file-local", f"/main-config/config.yaml={out_dir / 'pdp.yaml'}"]
    elif name == "wallet-backend":
        deploy_args += [
            "--file-local", f"/app/config.yaml={out_dir / 'wallet-backend.yaml'}",
            "--file-local", f"/app/registry.yaml={out_dir / 'wallet-backend-registry.yaml'}",
            "--file-literal", "/vctms/.keep=ok",
        ]
        # /vctms is created but left empty. The chart's registry.yaml points
        # local_overrides at it and the registry provider refuses to start if
        # it's missing ("stat /vctms: no such file or directory"), so the
        # .keep above is load-bearing even though nothing else goes in: every
        # credential type this stack issues is now published by
        # registry.siros.org (demo-credentials#18 added the last holdout,
        # urn:eudi:pid:arf-1.8:1), and a local override would only shadow the
        # published copy with a stale one. --credential-registries drops
        # local_overrides entirely; the empty dir is harmless there.
        ensure_secret(app, "jwtSecret", _persistent_secret(out_dir, "jwtSecret"))
        ensure_secret(app, "adminToken", _persistent_secret(out_dir, "adminToken"))
        deploy_args += [
            "--file-secret", "/main-secrets/jwtSecret=jwtSecret",
            "--file-secret", "/main-secrets/adminToken=adminToken",
        ]
        # Wallet Provider Key Attestation signing identity (OID4VCI
        # "attestation" proof type) - private key is confidential (Fly
        # secret, like vc-registry's vcSigningKey); the leaf cert and rootCA
        # are public (--file-local, freshly written from pki_dir each deploy,
        # same as vc-registry's rootCA.crt/signing_ec_chain.pem). Chains to
        # the SAME per-environment rootCA vc-issuer/vc-verifier already trust
        # (fixtures/create-pki.sh generates it as one more identity off that
        # root), so a credential issuer that already trusts this
        # environment's rootCA gets a Key Attestation trust anchor for free.
        ensure_secret(app, "walletProviderKey", (pki_dir / "wallet_provider_ec_private.pem").read_text())
        # Reuses this environment's EC signing key rather than minting a
        # separate identity: the AS signs its own access tokens, which nothing
        # outside the environment validates against a published chain.
        ensure_secret(app, "asSigningKey", (pki_dir / "signing_ec_private.pem").read_text())
        deploy_args += [
            "--file-secret", "/main-secrets/walletProviderKey=walletProviderKey",
            # go-wallet-backend's built-in Authorization Server. The chart
            # renders as.signing_key_path=/as-cert/tls.key and
            # as.rules_dir=/as-rules from a cert-manager Certificate and a
            # ConfigMap; on Fly those are a secret and an uploaded file. A
            # missing signing key is fatal at startup, not a fall-back to the
            # image's baked-in rules.
            "--file-secret", "/as-cert/tls.key=asSigningKey",
            "--file-local", f"/main-config/walletProviderCert.pem={pki_dir / 'wallet_provider_ec.crt'}",
            "--file-local", f"/main-config/walletProviderCA.pem={pki_dir / 'rootCA.crt'}",
        ]
        for rules in sorted((out_dir / "as-rules").glob("*")):
            deploy_args += ["--file-local", f"/as-rules/{rules.name}={rules}"]
    elif name == "wallet-proxy":
        conf_path = out_dir / "wallet-proxy.conf"
        conf_path.write_text(wallet_proxy_conf(env))
        deploy_args += [
            "--file-local", f"/etc/nginx/conf.d/default.conf={conf_path}",
            "--file-local", f"/etc/nginx/well-known/assetlinks.json={assetlinks_path}",
        ]
    elif name == "wallet-frontend":
        conf_path = out_dir / "wallet-frontend.conf"
        conf_path.write_text(wallet_frontend_conf(env, conformance))
        dashboard_path = out_dir / "wallet-frontend-dashboard.html"
        # Reuse the exact identities already wired into assetlinks_path
        # (generate_android_assets(), same merge as rp_origins) rather than
        # re-deriving them a second way that could drift out of sync.
        android_entries = json.loads(assetlinks_path.read_text())
        android_identities = {
            e["target"]["package_name"]: e["target"]["sha256_cert_fingerprints"] for e in android_entries
        }
        fe_data = extract_configmap_data(docs, "wallet-frontend-main")
        apple_app_ids = [a.strip() for a in fe_data.get("wellknownAppleAppIds", "").split(",") if a.strip()]
        conformance_url = app_url(env, "conformance") if conformance else None
        dashboard_path.write_text(
            wallet_frontend_dashboard_html(env, android_identities, apple_app_ids, conformance_url))
        deploy_args += [
            "--file-local", f"/etc/nginx/conf.d/default.conf={conf_path}",
            "--file-local", f"/usr/share/nginx/startup.html={dashboard_path}",
        ]
        deploy_args += _wallet_frontend_env(env, docs, android_identities, wallet_attestation)
    elif name == "conformance-server":
        deploy_args += [
            "--env", f"BASE_URL={app_url(env, 'conformance')}",
            "--env", f"MONGODB_HOST={app_name(env, 'conformance-mongodb')}.internal",
            # Matches docker-compose.conformance.yml exactly - devmode means
            # no real OAuth login is needed, so these never actually get used.
            "--env", "SPRING_PROFILES_ACTIVE=",
            "--env", "FINTECHLABS_DEVMODE=true",
            "--env", "OIDC_GOOGLE_CLIENTID=google-client",
            "--env", "OIDC_GOOGLE_SECRET=google-secret",
            "--env", "OIDC_GITLAB_CLIENTID=gitlab-client",
            "--env", "OIDC_GITLAB_SECRET=gitlab-secret",
        ]
    elif name == "conformance":
        # conformance-suite-nginx's baked-in nginx.conf hardcodes
        # `proxy_pass http://server:8080` (no env var to retarget it -
        # confirmed via `docker run --entrypoint cat ... nginx.conf`) - "server"
        # isn't a real Fly hostname, so it's resolved via a /etc/hosts entry
        # instead, using conformance-server's actual 6PN IP (static proxy_pass
        # targets resolve via the system resolver at nginx startup, which
        # checks /etc/hosts before the image's own `resolver 127.0.0.11`
        # directive even applies - that directive only matters for
        # variable-based proxy_pass targets, not this static one).
        server_ip = machine_private_ip(app_name(env, "conformance-server"))
        if not server_ip:
            raise SystemExit(
                f"Could not determine {app_name(env, 'conformance-server')}'s private IP - "
                "it must be deployed (and have a running machine) before 'conformance'."
            )
        hosts_path = out_dir / "conformance-hosts"
        hosts_path.write_text(f"{server_ip} server\n")
        deploy_args += ["--file-local", f"/etc/hosts={hosts_path}"]
    elif name == "conformance-runner":
        # Same FRONTEND_URL/ADMIN_URL/ADMIN_TOKEN values already printed in
        # main()'s "run sirosid-tests manually" summary block below - this
        # just automates the same thing sirosid-tests' own Makefile targets
        # do by hand, from the dashboard. ADMIN_TOKEN goes through
        # ensure_secret() (becomes a real env var once set via `flyctl
        # secrets set`, same as jwtSecret/adminToken on wallet-backend) -
        # not --env, since it's a credential, not a plain URL.
        ensure_secret(app, "ADMIN_TOKEN", _persistent_secret(out_dir, "adminToken"))
        deploy_args += [
            "--env", f"CONFORMANCE_URL={app_url(env, 'conformance')}",
            "--env", f"FRONTEND_URL={app_url(env, 'wallet-frontend')}",
            "--env", f"ADMIN_URL={app_url(env, 'wallet-proxy')}",
            # helpers/vc-services.ts's checkVCServicesHealth() (used by the
            # issuer/verifier specs) defaults to localhost:900x - meaningless
            # from inside a Fly machine. Override with 6PN .internal
            # addresses (reachable regardless of whether the target has a
            # public Fly URL too - vc-issuer doesn't, see COMPONENTS).
            "--env", f"VC_ISSUER_URL=http://{app_name(env, 'vc-issuer')}.internal:8080",
            "--env", f"VC_VERIFIER_URL=http://{app_name(env, 'vc-verifier')}.internal:8080",
            "--env", f"VC_APIGW_URL=http://{app_name(env, 'vc-apigw')}.internal:8080",
            # Conformance suite's self-signed cert - matches docker-compose.conformance.yml locally.
            "--env", "NODE_TLS_REJECT_UNAUTHORIZED=0",
        ]

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

    if primary_public_port is not None or tcp_passthrough_port is not None:
        print(f"{name}: {app_url(env, name)}")


def _vc_service_files(app: str, out_dir: Path, pki_dir: Path, service: str, metadata: bool,
                       presentation_requests: bool = False, bootstrapping: bool = False) -> list:
    # The private key authenticates every credential this environment issues -
    # a Fly secret (encrypted, write-only), not a --file-local file (stored
    # in the app's plain config/release object, readable via e.g. `flyctl
    # config show`). rootCA.crt/signing_ec_chain.pem are public certs, no
    # confidentiality need, so they stay --file-local. See
    # check_pki_consistency() for why ensure_secret's default
    # never-rotate-if-already-set behavior is safe here specifically.
    ensure_secret(app, "vcSigningKey", (pki_dir / "signing_ec_private.pem").read_text())
    # Each service gets its OWN rendered config now, not one file shared by all
    # four - they no longer have to be bumped in lockstep just because a config
    # shape moved (see environments/gdc.yaml's images comment).
    args = [
        "--env", "VC_CONFIG_YAML=/config.yaml",
        "--env", "SSL_CERT_FILE=/pki/rootCA.crt",
        "--file-local", f"/config.yaml={out_dir / f'{service}.yaml'}",
        "--file-local", f"/pki/rootCA.crt={pki_dir / 'rootCA.crt'}",
        "--file-secret", "/pki/signing_ec_private.pem=vcSigningKey",
        "--file-local", f"/pki/signing_ec_chain.pem={pki_dir / 'signing_ec_chain.pem'}",
    ]
    # These three directories are rendered from the chart's own ConfigMaps
    # (vctms, verifier-pres-reqs, issuer-documents) rather than uploaded
    # straight from fixtures/, so their mount points are the chart's names.
    if metadata:
        for f in sorted((out_dir / "vctms").glob("*")):
            args += ["--file-local", f"/vctms/{f.name}={f}"]
    if presentation_requests:
        for f in sorted((out_dir / "pres-reqs").glob("*")):
            args += ["--file-local", f"/pres-reqs/{f.name}={f}"]
    if bootstrapping:
        for f in sorted((out_dir / "documents").glob("*")):
            args += ["--file-local", f"/documents/{f.name}={f}"]
    return args


def _wallet_frontend_env(env: str, docs: list, android_identities: dict[str, list[str]] | None = None,
                          wallet_attestation: bool = False) -> list:
    proxy = app_url(env, "wallet-proxy")
    frontend = app_url(env, "wallet-frontend")
    fe_data = extract_configmap_data(docs, "wallet-frontend-main")
    # Android's Digital Asset Links check (assetlinks.json) must be served at
    # the RP ID's OWN domain (wallet-frontend's, same as WEBAUTHN_RPID below) -
    # wallet-proxy also serves a copy (wallet_proxy_conf), but that's the
    # wrong domain to ever be consulted for THIS rp_id, exactly like the
    # apple-app-site-association situation described below. Reuses the same
    # android_identities dict deploy_component() already built from
    # assetlinks_path (single source of truth, matches rp_origins exactly)
    # rather than re-deriving it a second way that could drift.
    wellknown_android = ",".join(
        f"{package}::{fingerprint}"
        for package, fingerprints in (android_identities or {}).items()
        for fingerprint in fingerprints
    )
    values = {
        # wallet-frontend's OWN origin, not wallet-proxy's directly - see
        # wallet_frontend_conf()'s same-origin API proxy block for why: the
        # AS session cookie is SameSite=Strict, which a browser will never
        # send across the genuinely-different registrable domains of
        # wallet-frontend.fly.dev and wallet-proxy.fly.dev. Routing BACKEND_URL
        # through wallet-frontend's own nginx (which proxies on to
        # wallet-proxy internally) makes every API call same-origin instead.
        # WALLET_ENGINE_URL (websocket) is unaffected - the engine
        # authenticates via a token embedded in the handshake payload
        # (internal/engine/session.go's validateToken), not a cookie, so it
        # has no SameSite exposure and can keep talking to wallet-proxy
        # directly.
        "WALLET_BACKEND_URL": frontend,
        "WALLET_ENGINE_URL": proxy,
        # Must equal wallet-backend's server.rp_id (render-helm-config.py's
        # patch_wallet_backend_fly) - the passkey ceremony runs in the
        # browser at THIS app's own origin, not wallet-proxy's, so rp_id has
        # to be wallet-frontend's domain or every passkey registration fails.
        "WEBAUTHN_RPID": f"sirosid-{env}-wallet-frontend.fly.dev",
        "STATIC_PUBLIC_URL": frontend,
        "WELLKNOWN_ANDROID_PACKAGE_NAMES_AND_FINGERPRINTS": wellknown_android,
        # For Universal Links on wallet-frontend's own domain (separate from
        # the AASA wallet-proxy serves for the passkey RP ID - see
        # fly_common.wallet_proxy_conf). Helm already sets this in production
        # (04-wallet-frontend.yaml:185); this Fly deployment simply never had
        # set it before now.
        "WELLKNOWN_APPLE_APPIDS": fe_data.get("wellknownAppleAppIds", ""),
        "STATIC_NAME": f"SIROS ID (fly-{env})",
        # Must be wallet-frontend's own callback route, not its bare origin -
        # App.tsx registers the OID4VCI callback at "cb/*" (relative to the
        # SPA's BASE_PATH router), so a bare "/" redirect lands on the
        # dashboard instead of OpenIDFlowCallback, and (separately) doesn't
        # match what the rendered apigw config registers as this environment's
        # e2e-test-client redirect_uri - confirmed live as the cause of every
        # web-initiated authorization_code credential issuance failing with
        # vc-apigw's "invalid_client".
        "OPENID4VCI_REDIRECT_URI": f"{frontend}/id/default/cb",
        "VCT_REGISTRY_URL": f"{proxy}/registry/type-metadata",
        "TRANSPORT_PREFERENCE": "websocket",
        # Must be wallet-frontend's own recognized tokens (src/config.ts:
        # ALLOWED_TRANSPORTS.filter(['http_proxy','websocket','direct'])) -
        # "http"/"wmp" aren't valid values and were silently dropped by that
        # filter, leaving NO transport at all once the (also invalid) values
        # were filtered out. websocket-only, no http_proxy fallback - this
        # deployment runs the websocket transport exclusively (plus wmp).
        "ALLOWED_TRANSPORTS": "websocket,wmp",
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
    if wallet_attestation:
        # Pairs with patch_wallet_backend_fly's wia.issuer/omit_x5c and
        # the rendered apigw config's trust.wallet_attestation - without
        # this, wallet-backend never generates/attaches a WIA at all, so the
        # OAuth-Client-Attestation headers vc-apigw is now configured to
        # accept never get sent.
        values["WIA_ENABLED"] = "true"
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


def _personal_region() -> str:
    """This developer's own default Fly region, from a gitignored `.fly-region`.

    Contributors are in different places, and a scratch environment should
    come up near whoever is using it. Same per-developer-dotfile convention as
    .android-apps / .env.android. A named environment's own `region:` still
    wins, so a shared one like gdc does not drift depending on who deployed it.
    """
    path = SIROSID_DEV_ROOT / ".fly-region"
    if not path.is_file():
        return ""
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    return ""


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
            post("/admin/tenants/default/issuers", {
                "credential_issuer_identifier": apigw_url,
                "visible": True,
                # Without this, wallet-backend has no registered client_id for
                # this issuer and falls back to the "unregistered client"
                # convention (client_id = redirect_uri) for the OID4VCI
                # authorization_code flow - which vc-apigw's PAR endpoint
                # rejects (401 invalid_client) since that string isn't a
                # client_id it knows about. "e2e-test-client" is already
                # configured in vc-apigw's own config (fixtures/vc-config.yaml)
                # as a public+PKCE client matching the native app's redirect_uri
                # and every credential scope - the same client local dev and
                # CI conformance tests already use successfully for this exact
                # flow, not a Fly-specific workaround.
                "client_id": "e2e-test-client",
            })
            post("/admin/tenants/default/verifiers", {"name": "VC Verifier", "url": verifier_url})
            print(f"registered vc-apigw ({apigw_url}) and vc-verifier ({verifier_url}) "
                  "with wallet-backend's default tenant")
            return
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(2)
    # Loud and fatal, not a warning: an environment with no registered issuer
    # looks completely healthy - every app up, every check passing - and fails
    # only when a user tries to sign up or add a credential. That is a much
    # worse thing to hand someone than a failed deploy. Seen for real:
    # wallet-backend was crash-looping while this ran, so registration
    # silently did nothing and the deploy still reported "Environment is up".
    raise SystemExit(
        f"\nERROR: could not register VC services with wallet-backend after retries ({last_err}).\n"
        f"  The environment is deployed but the wallet has NO issuers or verifiers, so signup and\n"
        f"  credential issuance will fail. Check wallet-backend is actually serving:\n"
        f"    flyctl logs -a sirosid-{env}-wallet-backend\n"
        f"  then re-run `make fly-up ENV={env}` (idempotent), or register by hand:\n"
        f"    POST {proxy_url}/admin/tenants/default/issuers|verifiers"
        " with 'Authorization: Bearer <adminToken>'")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--region", default="",
                         help="Pin this run's Fly region (e.g. 'arn'), overriding "
                              "environments/<name>.yaml's `region:`, $FLY_REGION, .fly-region, and "
                              "Fly's own detected suggestion - see the region-resolution comment below.")
    parser.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT.parent / "siros-id-stack"))
    parser.add_argument("--android-app", action="append",
                         help="package=fingerprint (SHA-256, colon-separated hex, as printed by "
                              "`keytool -list -v`) for a debug build or Play Store signing key to "
                              "authenticate against this environment, in addition to the production "
                              "fingerprints siros-id-stack already carries. Repeatable, and each value "
                              "may be a comma-separated list. The same package can appear more than "
                              "once (e.g. a debug key and a Play Store upload key). Combined with - "
                              "not instead of - .android-apps and .env.android if present; see "
                              "scripts/android_apps.py for the full precedence.")
    parser.add_argument("--images", default="",
                         help="Comma-separated component=image overrides for this environment only "
                              "(e.g. 'wallet-backend=ghcr.io/sirosfoundation/go-wallet-backend:pr-123'), "
                              "so two developers running their own ENV=<name> can each pin different "
                              "versions without touching values-fly.yaml or colliding with each other. "
                              "Component names match scripts/fly_common.py's COMPONENTS list. A bare, "
                              "unqualified value that's already present in the local Docker daemon (e.g. "
                              "'wallet-backend=wallet-backend-e2e-test:local', what `make up REBUILD=yes` "
                              "already builds) is pushed to this environment's own registry.fly.io "
                              "namespace automatically - no manual docker tag/push/auth needed.")
    parser.add_argument("--conformance", action="store_true",
                         help="Also deploy the OpenID Conformance Suite (matches local dev's "
                              "CONFORMANCE=yes) - 3 extra apps: conformance-mongodb, conformance-server, "
                              "and the public 'conformance' nginx front. See fly_common.CONFORMANCE_COMPONENTS.")
    parser.add_argument("--wallet-attestation", action="store_true",
                         help="Enable OAuth-Client-Attestation-based client authentication "
                              "(draft-ietf-oauth-attestation-based-client-auth): wallets authenticate "
                              "to vc-apigw via their WIA alone, no pre-registered client_id. Configures "
                              "wallet-backend to issue an iss-based WIA (omitting the x5c chain, which "
                              "go-trust's whitelist can't validate for a self-signed cert), whitelists "
                              "this environment's own wallet-backend as a trusted wallet_provider in "
                              "PDP, and enables apigw.trust.wallet_attestation. Unvalidated against real "
                              "hardware/interop as of this flag's introduction.")
    parser.add_argument("--trusted-issuer", action="append",
                         help="Extra credential issuer URL to trust via PDP's whitelist, in addition to "
                              "this environment's own vc-apigw - for interop testing against a "
                              "third-party issuer (e.g. a conference/plugfest mdoc issuer). Repeatable, "
                              "and each value may be a comma-separated list.")
    parser.add_argument("--trusted-verifier", action="append",
                         help="Extra credential verifier identity to trust via PDP's whitelist, in "
                              "addition to this environment's own vc-verifier - for interop testing "
                              "against a third-party DC API/OpenID4VP verifier (e.g. "
                              "digital-credentials.dev, verifier.multipaz.org). Must be the exact "
                              "post-normalization Subject.ID string go-trust's whitelist compares "
                              "against - for an x509_hash:... client_id (the common case for DC API "
                              "test sites) paste it verbatim from the wallet's own 'not trusted' error "
                              "log, since that scheme is left un-normalized; for x509_san_dns:<host>/"
                              "x509_san_uri:<uri> schemes, go-trust normalizes to https://<host>/<uri> "
                              "before matching, so the entry must be written in that normalized form, "
                              "not the original x509_san_dns:/x509_san_uri: form. Repeatable, and each "
                              "value may be a comma-separated list.")
    parser.add_argument("--trusted-verifier-root", action="append",
                         help="Path to a PEM-encoded CA certificate to merge into PDP's system CA pool "
                              "(go-trust's additional_trusted_roots, go-trust#123+), for a verifier whose "
                              "request-signing certificate is issued by a long-lived, self-signed 'reader "
                              "CA' root meant to be trusted out-of-band per ISO 18013-5 convention, rather "
                              "than a public CA - e.g. verifier.multipaz.org's signing cert (distinct from "
                              "its ordinary publicly-CA-issued HTTPS cert), whose root is published at "
                              "https://verifier.multipaz.org/verifier/readerRootCert. Preferred over "
                              "--trusted-verifier's x509_hash leaf-pinning for this case, since the root "
                              "survives future leaf-certificate rotations. Repeatable.")
    parser.add_argument("--zk-circuits-source", action="append",
                         help="Extra verifier.zk_circuits.sources entry, tried ahead of vc's built-in "
                              "https://zk-circuits.fly.dev default - for a circuit not yet published "
                              "there, e.g. https://zk-circuits-test.fly.dev while a Vega circuit variant "
                              "awaits its expert review. Repeatable, and each value may be a "
                              "comma-separated list.")
    parser.add_argument("--rical-provider-url",
                         help="URL to fetch the RICAL (ISO 18013-5 2nd ed. Annex F reader-trust list) "
                              "from - registers PDP's mdocrical registry so BLE/NFC proximity "
                              "presentation's mdoc-reader-auth check can grant trust to a reader whose "
                              "cert chain appears in that list. Must be paired with "
                              "--rical-root-cert. One provider per environment (last-one-wins vs. "
                              "environments/<env>.yaml, unlike the repeatable trust flags above).")
    parser.add_argument("--rical-root-cert",
                         help="Path to the PEM-encoded certificate that signs the RICAL's own "
                              "COSE_Sign1 envelope (the out-of-band trust anchor per Annex F.3.1) - "
                              "must be paired with --rical-provider-url. See "
                              "fixtures/trusted-roots/README.md.")
    parser.add_argument("--dc-api-enable", default="", choices=["", "true", "false"],
                         help="Override verifier.digital_credentials.enable (W3C DC API support) for "
                              "this environment only - fixtures/vc-config.yaml's own default is "
                              "true for every environment. Last-one-wins vs. "
                              "environments/<env>.yaml's dc_api_enable, same as the RICAL flags above.")
    args = parser.parse_args()

    # environments/<env>.yaml (if present) supplies persisted defaults for a
    # named, durable environment - see scripts/env_config.py's module doc
    # for the full precedence (file first, CLI appends/overrides on top).
    env_cfg = load_environment_config(args.env)

    cli_trusted_issuers = []
    for pair in (args.trusted_issuer or []):
        cli_trusted_issuers.extend(v.strip() for v in pair.split(",") if v.strip())
    extra_trusted_issuers = merge_list(env_cfg["trusted_issuers"], cli_trusted_issuers)

    cli_trusted_verifiers = []
    for pair in (args.trusted_verifier or []):
        cli_trusted_verifiers.extend(v.strip() for v in pair.split(",") if v.strip())
    extra_trusted_verifiers = merge_list(env_cfg["trusted_verifiers"], cli_trusted_verifiers)

    cli_trusted_verifier_root_paths = []
    for pair in (args.trusted_verifier_root or []):
        cli_trusted_verifier_root_paths.extend(p.strip() for p in pair.split(",") if p.strip())
    # Merged as PATHS (not yet-read content) so file/CLI de-duplication works
    # on the same representation; each resulting path is read once below.
    trusted_verifier_root_paths = merge_list(env_cfg["trusted_verifier_roots"], cli_trusted_verifier_root_paths)
    extra_trusted_verifier_roots = [Path(p).read_text() for p in trusted_verifier_root_paths]

    cli_zk_circuits_sources = []
    for pair in (args.zk_circuits_source or []):
        cli_zk_circuits_sources.extend(v.strip() for v in pair.split(",") if v.strip())
    zk_circuits_sources = merge_list(env_cfg["zk_circuits_sources"], cli_zk_circuits_sources)

    # Scalar, last-one-wins (CLI overrides file) - one RICAL provider per
    # environment, unlike the repeatable trust flags above.
    rical_provider_url = args.rical_provider_url or env_cfg["rical_provider_url"] or None
    rical_root_cert_path = args.rical_root_cert or env_cfg["rical_root_cert"] or None
    rical_root_certificate_pem = Path(rical_root_cert_path).read_text() if rical_root_cert_path else None
    if bool(rical_provider_url) != bool(rical_root_certificate_pem):
        raise SystemExit("--rical-provider-url and --rical-root-cert must both be set, or neither")

    dc_api_enable = args.dc_api_enable or env_cfg["dc_api_enable"] or ""

    # Region. Every level here is an explicit pin; if none is set we take
    # Fly's own suggestion, which is the right default when contributors are
    # in different places. Most specific first:
    #   --region / REGION=        this run only
    #   environments/<name>.yaml  a named, shared environment pins its own, so
    #                             everyone redeploying gdc lands in one place
    #   $FLY_REGION               an ad-hoc shell default
    #   .fly-region               this developer's default
    #   detect_region()           Fly's suggestion - the anycast edge nearest
    #                             here, i.e. what `fly launch` would pick
    #   FLY_REGION_FALLBACK       only if that is unreachable
    # primary_region is a preference, not a constraint, and changing it does
    # not move machines that already exist - see fly_common.detect_region.
    pinned = (args.region or env_cfg["region"] or os.environ.get("FLY_REGION")
              or _personal_region())
    if pinned:
        region = pinned
        print(f"region: {region} (pinned)")
    else:
        region = detect_region()
        if region:
            print(f"region: {region} (Fly's suggestion for this machine - "
                  f"pin it with REGION=, environments/{args.env}.yaml's `region:`, or .fly-region)")
        else:
            region = FLY_REGION_FALLBACK
            print(f"region: {region} (fallback - could not reach Fly to ask)")

    cli_image_overrides = {}
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
        cli_image_overrides[component] = image.strip()
    image_overrides = merge_images(env_cfg["images"], cli_image_overrides)

    args.conformance = args.conformance or env_cfg["conformance"]
    args.wallet_attestation = args.wallet_attestation or env_cfg["wallet_attestation"]

    if not shutil.which("flyctl"):
        raise SystemExit("flyctl not found - install it first (https://fly.io/docs/flyctl/install/)")

    chart_dir = Path(args.chart_dir)
    out_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}"
    out_dir.mkdir(parents=True, exist_ok=True)

    identities = load_android_apps(extra=merge_list(env_cfg["android_apps"], args.android_app or []))
    # Fresh every run, not persisted/reused - see ensure_secret(force=True)'s
    # docstring for why that's fine specifically for mongo (no persistent
    # volume, so there's no old data a stale password would need to match).
    mongo_password = _rand_secret()

    print(f"=== Rendering config for environment '{args.env}' ===")
    # docs is the full rendered manifest (not just wallet-backend/pdp) -
    # reused below for image refs + mongo version + wallet-frontend's
    # Android/iOS wellknown values, instead of a second `helm template` call.
    docs = render_configs(args.env, chart_dir, [i["apk_key_hash"] for i in identities], mongo_password,
                           args.conformance, extra_trusted_issuers, args.wallet_attestation,
                           extra_trusted_verifiers, extra_trusted_verifier_roots, zk_circuits_sources,
                           rical_provider_url, rical_root_certificate_pem, dc_api_enable,
                           # environments/<name>.yaml's free-form `values:`
                           # block, deep-merged over everything else - the
                           # escape hatch for anything the typed keys above
                           # don't cover (see scripts/env_config.py).
                           env_cfg.get("values") or {})
    mongo_version = extract_mongo_version(docs)

    print(f"=== Generating per-environment PKI ===")
    pki_dir = generate_pki(args.env)

    print(f"=== Generating Android assetlinks.json ===")
    assetlinks_path = generate_android_assets(docs, out_dir, identities)
    # No separate iOS asset here - wallet-frontend's own image generates its
    # complete apple-app-site-association (applinks + webcredentials) from
    # WELLKNOWN_APPLE_APPIDS (_wallet_frontend_env), served at its own
    # domain - see wallet_proxy_conf()'s docstring for why an earlier version
    # of this deployment served a second, wrong-domain copy from wallet-proxy.

    if image_overrides:
        print(f"=== Image overrides for this environment: {image_overrides} ===")

    if args.conformance:
        # conformance-mongodb/conformance-server/conformance-runner MUST
        # deploy before wallet-frontend - its nginx config statically
        # proxy_passes to both conformance-server.internal AND
        # conformance-runner.internal (see wallet_frontend_conf()'s
        # docstring: a static, non-variable proxy_pass target that doesn't
        # resolve yet means nginx refuses to start AT ALL, not just that one
        # location). conformance-runner itself has no such constraint of its
        # own (it's a plain Node process, not nginx) - it only needs
        # FRONTEND_URL/ADMIN_URL to be reachable when a run is actually
        # triggered later, long after everything is up - but it still needs
        # to exist before wallet-frontend deploys, for wallet-frontend's own
        # sake. "conformance" (nginx) deploys last of all - it needs
        # conformance-server's machine to already exist to look up its
        # private IP (see deploy_component()'s "conformance" branch).
        non_frontend = [c for c in COMPONENTS if c["name"] != "wallet-frontend"]
        frontend = [c for c in COMPONENTS if c["name"] == "wallet-frontend"]
        conf_before, conf_after = [], []
        for c in CONFORMANCE_COMPONENTS:
            (conf_after if c["name"] == "conformance" else conf_before).append(c)
        all_components = non_frontend + conf_before + frontend + conf_after
    else:
        all_components = COMPONENTS
    print(f"=== Deploying {len(all_components)} apps to Fly (org: {FLY_ORG}) ===")
    deployed = []
    try:
        for comp in all_components:
            print(f"--- {comp['name']} ---")
            deploy_component(args.env, comp, docs, mongo_version, out_dir, pki_dir, assetlinks_path,
                              image_overrides, mongo_password, args.conformance, args.wallet_attestation,
                              region=region)
            deployed.append(comp["name"])
    except subprocess.CalledProcessError as e:
        # No auto-rollback - components deployed so far are left running
        # (each is independently a fine, working app; only the *sequence* is
        # incomplete), since silently tearing down a partially-up environment
        # the operator may still want to inspect/debug is worse than leaving
        # it and saying so clearly.
        failed = all_components[len(deployed)]["name"]
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
    for comp in all_components:
        if any(p["public"] for p in comp["ports"]):
            print(f"  {comp['name']}: {app_url(args.env, comp['name'])}")
    print()
    print("To run sirosid-tests' CDP-based WebAuthn conformance specs against this")
    print("environment instead of localhost (see sirosid-tests/specs/conformance/):")
    print(f"  export FRONTEND_URL={app_url(args.env, 'wallet-frontend')}")
    print(f"  export ADMIN_URL={app_url(args.env, 'wallet-proxy')}")
    print(f"  export ADMIN_TOKEN={_persistent_secret(out_dir, 'adminToken')}")
    if args.conformance:
        print(f"  export CONFORMANCE_URL={app_url(args.env, 'conformance')}")
        print("  export NODE_TLS_REJECT_UNAUTHORIZED=0  # conformance suite's self-signed cert")
        print()
        print("Or run them from the dashboard's Conformance tab (same specs, driven by")
        print(f"conformance-runner): {app_url(args.env, 'wallet-frontend')}")
    print()
    print(f"Tear down with: make fly-down ENV={args.env}")


if __name__ == "__main__":
    main()
