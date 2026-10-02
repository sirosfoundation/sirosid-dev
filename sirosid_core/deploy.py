"""Deploying an instance to Fly: render, generate PKI, deploy each component, register.

This is `fly-up` as a library. A caller gives it an InstanceSpec (what to deploy),
a FlyClient (as whom, in which org), a Naming (what everything is called) and a
Resources (where the chart, values and fixtures are), and gets a DeployResult
back. It prints nothing and exits nothing: progress goes to a callback and
failure is a DeployError. The CLI (scripts/fly-up.py) and a hosted service are
both thin callers of deploy_instance().

The registration step needs HTTP to the new instance, which this module does not
own: the caller passes `register(admin_url, admin_token, issuer_url, verifier_url)`
and raises RegistrationError for "not ready yet, retry".
"""
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .android import identities_from_entries
from .assets import (assetlinks_json, mini_oidc_config, wallet_frontend_conf, wallet_frontend_dashboard_html,
                     wallet_proxy_conf, write_fly_toml)
from .components import (CONFORMANCE_COMPONENTS, MINI_OIDC_APIGW_CLIENT_ID, MINI_OIDC_APIGW_CLIENT_SECRET,
                         build_components)
from .fly import FlyClient, FlyDeployError, FlyError
from .helm import extract_configmap_data, extract_image
from .naming import Naming
from .render import render
from .resources import Resources
from .spec import InstanceSpec
from .state import persistent_secret


class DeployError(RuntimeError):
    """A deploy could not complete. `component` and `deployed` say where it stopped."""

    def __init__(self, message, component=None, deployed=None, returncode=None):
        super().__init__(message)
        self.component = component
        self.deployed = list(deployed or [])
        self.returncode = returncode


class RegistrationError(RuntimeError):
    """Raised by a `register` callable when the instance is not ready yet."""


@dataclass
class DeployContext:
    fly: FlyClient
    naming: Naming
    resources: Resources
    spec: InstanceSpec
    say: object = print
    docs: list = None
    mongo_version: str = ""
    out_dir: Path = None
    pki_dir: Path = None
    assetlinks_path: Path = None
    mongo_password: str = ""


@dataclass
class DeployResult:
    out_dir: Path
    docs: list
    images: dict
    deployed: list = field(default_factory=list)
    urls: dict = field(default_factory=dict)
    admin_token: str = ""
    mongo_password: str = ""
    rendered_only: bool = False


def check_pki_consistency(ctx: DeployContext, pki_dir: Path):
    fly, naming = ctx.fly, ctx.naming
    env = naming.env
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
    app = naming.app("vc-registry")
    if not fly.app_exists(app):
        return  # brand-new environment - nothing to mismatch against yet
    if "vcSigningKey" in fly.existing_secret_names(app):
        raise DeployError(
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


def generate_pki(ctx: DeployContext) -> Path:
    fly, naming, resources = ctx.fly, ctx.naming, ctx.resources
    env = naming.env
    pki_dir = ctx.out_dir / "vc-pki"
    check_pki_consistency(ctx, pki_dir)
    # The signing cert's URI SAN is the identity mdoc verifiers derive for an
    # mDL's issuer (vc's extractMDocIssuerID), and it has to be the same
    # string build_fly_values_overlay() puts in the PDP's mdociaca allowlist
    # and that credentials carry as `iss`: vc-apigw's public URL. Without it
    # the cert's first DNS SAN (localhost) is the identity, the allowlist
    # never matches, and this environment's own vc-verifier rejects every
    # mDL it issued with "issuer not trusted". create-pki.sh re-issues the
    # cert from the existing key when the SAN is missing, so this is safe to
    # apply to an environment that already has a deployed signing key.
    env_vars = {
        **os.environ,
        "PKI_DIR_OVERRIDE": str(pki_dir),
        "SIGNING_CERT_ISSUER_URL": naming.url("vc-apigw"),
    }
    fly.exec(["bash", "./create-pki.sh"], cwd=resources.fixtures, env=env_vars)
    return pki_dir


def generate_android_assets(ctx: DeployContext, identities: list) -> Path:
    docs, out_dir = ctx.docs, ctx.out_dir
    fe_data = extract_configmap_data(docs, "wallet-frontend-main")
    wellknown = fe_data.get("wellknownAndroidPackageNamesAndFingerprints", "")
    extra = [(i["package"], i["fingerprint_hex"]) for i in identities]
    assetlinks_path = out_dir / "assetlinks.json"
    assetlinks_path.write_text(assetlinks_json(wellknown, extra_identities=extra))
    return assetlinks_path


def _vc_service_files(ctx: DeployContext, app: str, service: str, metadata: bool,
                      presentation_requests: bool = False, bootstrapping: bool = False) -> list:
    fly, out_dir, pki_dir = ctx.fly, ctx.out_dir, ctx.pki_dir
    # The private key authenticates every credential this environment issues -
    # a Fly secret (encrypted, write-only), not a --file-local file (stored
    # in the app's plain config/release object, readable via e.g. `flyctl
    # config show`). rootCA.crt/signing_ec_chain.pem are public certs, no
    # confidentiality need, so they stay --file-local. See
    # check_pki_consistency() for why ensure_secret's default
    # never-rotate-if-already-set behavior is safe here specifically.
    fly.ensure_secret(app, "vcSigningKey", (pki_dir / "signing_ec_private.pem").read_text())
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
    # The chart decodes these from the branding ConfigMap in an initContainer;
    # vc_render.write_branding_assets does it at render time instead, and the
    # mount point is the chart's own /branding-assets so the config the
    # services read is identical either way.
    for f in sorted((out_dir / "branding-assets").glob("*.png")):
        args += ["--file-local", f"/branding-assets/{f.name}={f}"]
    if bootstrapping:
        for f in sorted((out_dir / "documents").glob("*")):
            args += ["--file-local", f"/documents/{f.name}={f}"]
        # apigw is also the one service with an admin API: its Bearer-JWT
        # verification key (public, so --file-local) - scripts/api_auth.py.
        args += ["--file-local", f"/main-config/api_auth_jwks.json={out_dir / 'api_auth_jwks.json'}"]
    return args


def _wallet_frontend_env(ctx: DeployContext, android_identities: dict = None) -> list:
    naming, docs, wallet_attestation = ctx.naming, ctx.docs, ctx.spec.wallet_attestation
    env = naming.env
    proxy = naming.url("wallet-proxy")
    frontend = naming.url("wallet-frontend")
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
        "WEBAUTHN_RPID": f"{naming.host('wallet-frontend')}",
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


def resolve_mongo_password(fly: FlyClient, naming: Naming, out_dir: Path, say=print) -> str:
    env = naming.env
    """The Mongo root password this environment's VOLUME was initialised with.

    Before volumes it was regenerated every run (empty data every time, so
    nothing to match). Now the data persists, and MONGO_INITDB_ROOT_* only
    applies to an empty /data/db, so the password has to be the one already
    in use. Three sources, in order:

      1. this machine's cache (fixtures/rendered/fly-<env>/mongoRootPassword,
         written by the run that created the volume)
      2. the running mongodb machine itself - Fly secrets cannot be read back
         through the API, but the --file-secret is readable from inside via
         `fly ssh console`, which is how a developer who did not do the last
         deploy avoids rendering a mismatched password into every consumer
      3. fresh - only when neither the cache nor a mongodb machine with the
         secret exists (a brand-new environment, or one whose volume was just
         cleared), in which case the data is empty and this run initialises it

    Anything else is a hard stop: deploying a guessed password would leave
    every consumer failing Mongo auth against data nobody can then reach.
    """
    cached = out_dir / "mongoRootPassword"
    if cached.exists():
        return cached.read_text().strip()
    app = naming.app("mongodb")
    has_volume = any(v.get("state") != "destroyed" for v in fly.list_volumes(app)) if fly.app_exists(app) else False
    if has_volume and "mongoRootPassword" in fly.existing_secret_names(app):
        say(f"{app}: no local password cache but a volume exists - reading the password back from the machine")
        fly.ensure_running(app)
        value = fly.read_machine_file(app, "/run/secrets/mongoRootPassword")
        if not value:
            raise DeployError(
                f"\n{app} has a data volume and a root password set, but this machine has no cached copy\n"
                f"and reading it back over `fly ssh console` failed. Deploying a new password would lock\n"
                f"every consumer out of the existing data. Either copy fixtures/rendered/fly-{env}/\n"
                f"from the machine that last deployed this environment, or clear its data first:\n"
                f"  make fly-storage-clear ENV={env}   (or: make fly-down ENV={env} without KEEP_DATA)")
        cached.write_text(value)
        return value
    return persistent_secret(out_dir, "mongoRootPassword")


def deploy_component(ctx: DeployContext, comp: dict):
    fly, naming, resources, spec, say = ctx.fly, ctx.naming, ctx.resources, ctx.spec, ctx.say
    env, docs, mongo_version = naming.env, ctx.docs, ctx.mongo_version
    out_dir, pki_dir, assetlinks_path = ctx.out_dir, ctx.pki_dir, ctx.assetlinks_path
    image_overrides, mongo_password = spec.images, ctx.mongo_password
    conformance, wallet_attestation, region = spec.conformance, spec.wallet_attestation, spec.region
    name = comp["name"]
    app = naming.app(name)
    fly.ensure_app(app, network=naming.network(), allocate_public_ips=(name == "conformance"))

    if name in image_overrides:
        # Explicit --images override (e.g. a dev testing their own branch
        # build of one component) always wins, regardless of where the image
        # would otherwise come from - one override point covering all 10
        # components uniformly, not a Helm-values override for some and a
        # separate CLI flag for the two non-Helm ones (mongodb, mini-oidc).
        image = image_overrides[name]
        if fly.is_local_docker_image(image):
            # A bare local build tag (e.g. what `make up REBUILD=yes` /
            # docker-compose already produced, like wallet-backend-e2e-test:local)
            # - push it into this app's own registry.fly.io namespace so `-i`
            # below can deploy it like any other ref, with no manual `docker
            # tag`/`docker push`/`flyctl auth docker` from the developer.
            say(f"{name}: {image!r} is a local Docker image - pushing to registry.fly.io/{app}")
            image = fly.push_local_image(app, image)
    elif "image_from_values" in comp:
        image = extract_image(docs, comp["image_from_values"])
    else:
        image = comp["image"].format(mongo_version=mongo_version)

    # integrated registry layout (go-wallet-backend#431+): the registry is a
    # block of config.yaml, so there is no registry.yaml to mount or pass.
    registry_integrated = not (out_dir / "wallet-backend-registry.yaml").exists()

    public_ports = [p["internal"] for p in comp["ports"] if p["public"]]
    primary_public_port = public_ports[0] if public_ports else None

    toml_path = out_dir / f"{name}.fly.toml"
    process_cmd = {
        "pdp": "--config /main-config/config.yaml",
        "wallet-backend": ("--mode=all --config=/app/config.yaml"
                           + ("" if registry_integrated else " --registry-config=/app/registry.yaml")),
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
                    internal_check=comp.get("internal_check"), tcp_passthrough_port=tcp_passthrough_port,
                    # The volume mount for the storage apps. Regression note: the
                    # first release generated the toml WITHOUT this, so the volume
                    # existed but nothing used it and every deploy replaced the
                    # machine - fly.assert_volume_mounted() below now fails the deploy
                    # rather than letting that pass silently again.
                    mount=comp.get("mount"), autostart=not spec.scale_to_zero)

    deploy_args = ["deploy", "-a", app, "-c", str(toml_path), "-i", image,
                   "--ha=false", "--strategy", "immediate", "--yes"]

    volume = None
    if "mount" in comp:
        # Volume first: `fly deploy` does not create volumes, and a machine
        # that predates the mount cannot have one attached (see the helper).
        volume = fly.ensure_volume(app, comp["mount"]["volume"], region)
        fly.destroy_machines_without_mount(app)

    if name == "mongodb":
        # Not rotated once data exists: the volume keeps the data the root
        # user was created with (MONGO_INITDB_ROOT_* only applies to an EMPTY
        # /data/db), so the secret must stay what resolve_mongo_password()
        # found. A freshly CREATED volume is the exception and needs force:
        # an environment deployed before volumes existed still carries the
        # last run's rotated secret, which nothing knows any more - without
        # force, ensure_secret would keep it while every consumer gets the
        # new password, and Mongo auth would fail everywhere.
        fly.ensure_secret(app, "mongoRootPassword", mongo_password, force=bool(volume and volume.get("created")))
        deploy_args += [
            "--env", "MONGO_INITDB_ROOT_USERNAME=root",
            "--env", "MONGO_INITDB_ROOT_PASSWORD_FILE=/run/secrets/mongoRootPassword",
            "--file-secret", "/run/secrets/mongoRootPassword=mongoRootPassword",
        ]
    elif name == "mini-oidc":
        config_path = out_dir / "mini-oidc-config.yaml"
        config_path.write_text(mini_oidc_config(env, naming))
        apigw_redirect = f"{naming.url('vc-apigw')}/oidcrp/callback"
        deploy_args += [
            # mini-oidc's own binary defaults CONFIG_FILE to the relative
            # path configs/config.yaml, which doesn't exist in the image at
            # that cwd - docker-compose.vc-services.yml sets this explicitly
            # too, easy to miss since the file mounted below already lives
            # at the "right" path and looks like it should just be picked up.
            "--env", "CONFIG_FILE=/etc/mini-oidc/configs/config.production.yaml",
            "--env", "USERS_FILE=/etc/mini-oidc/users.yaml",
            "--env", f"ISSUER={naming.url('mini-oidc')}",
            # RP_BASE_URL/CLIENT_ID only back the mini-oidc-rp test client
            # (mini_oidc_config's first `clients` entry) - mini-oidc-rp itself
            # isn't deployed here (a standalone harness for testing the OP,
            # not part of vc-apigw's real flow), so these are unused but must
            # be set to something for ${VAR} expansion to produce valid YAML.
            "--env", f"RP_BASE_URL={naming.url('mini-oidc')}",
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
        deploy_args += _vc_service_files(ctx, app, "vc-registry", metadata=False)
    elif name == "vc-issuer":
        deploy_args += _vc_service_files(ctx, app, "vc-issuer", metadata=True)
    elif name == "vc-verifier":
        deploy_args += _vc_service_files(ctx, app, "vc-verifier", metadata=True, presentation_requests=True)
    elif name == "vc-apigw":
        deploy_args += _vc_service_files(ctx, app, "vc-apigw", metadata=True, bootstrapping=True)
    elif name == "pdp":
        deploy_args += ["--file-local", f"/main-config/config.yaml={out_dir / 'pdp.yaml'}"]
    elif name == "wallet-backend":
        deploy_args += [
            "--file-local", f"/app/config.yaml={out_dir / 'wallet-backend.yaml'}",
            *([] if registry_integrated else
              ["--file-local", f"/app/registry.yaml={out_dir / 'wallet-backend-registry.yaml'}"]),
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
        fly.ensure_secret(app, "jwtSecret", persistent_secret(out_dir, "jwtSecret"))
        fly.ensure_secret(app, "adminToken", persistent_secret(out_dir, "adminToken"))
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
        fly.ensure_secret(app, "walletProviderKey", (pki_dir / "wallet_provider_ec_private.pem").read_text())
        # Reuses this environment's EC signing key rather than minting a
        # separate identity: the AS signs its own access tokens, which nothing
        # outside the environment validates against a published chain.
        fly.ensure_secret(app, "asSigningKey", (pki_dir / "signing_ec_private.pem").read_text())
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
        conf_path.write_text(wallet_proxy_conf(env, naming))
        deploy_args += [
            "--file-local", f"/etc/nginx/conf.d/default.conf={conf_path}",
            "--file-local", f"/etc/nginx/well-known/assetlinks.json={assetlinks_path}",
        ]
    elif name == "wallet-frontend":
        conf_path = out_dir / "wallet-frontend.conf"
        conf_path.write_text(wallet_frontend_conf(env, conformance, naming))
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
        conformance_url = naming.url("conformance") if conformance else None
        dashboard_path.write_text(
            wallet_frontend_dashboard_html(env, android_identities, apple_app_ids, conformance_url, naming=naming))
        deploy_args += [
            "--file-local", f"/etc/nginx/conf.d/default.conf={conf_path}",
            "--file-local", f"/usr/share/nginx/startup.html={dashboard_path}",
            # The Storage card - the very same file the local dashboard mounts.
            "--file-local", f"/usr/share/nginx/storage-card.js={resources.root / 'dashboard' / 'storage-card.js'}",
        ]
        deploy_args += _wallet_frontend_env(ctx, android_identities)
    elif name == "conformance-server":
        deploy_args += [
            "--env", f"BASE_URL={naming.url('conformance')}",
            "--env", f"MONGODB_HOST={naming.internal('conformance-mongodb')}",
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
        server_ip = fly.machine_private_ip(naming.app("conformance-server"))
        if not server_ip:
            raise DeployError(
                f"Could not determine {naming.app('conformance-server')}'s private IP - "
                "it must be deployed (and have a running machine) before 'conformance'."
            )
        hosts_path = out_dir / "conformance-hosts"
        hosts_path.write_text(f"{server_ip} server\n")
        deploy_args += ["--file-local", f"/etc/hosts={hosts_path}"]
    elif name == "env-admin":
        # sirosid-dev's own image (env-admin/Dockerfile). The pin comes from
        # values-fly.yaml like mini-oidc's; until the publish workflow has
        # pushed that tag (first release), or when testing a local change,
        # build it here and push into this app's registry.fly.io namespace -
        # the same path `IMAGES=<local tag>` already takes.
        if name not in image_overrides and not fly.image_pullable(image):
            say(f"env-admin: {image} is not pullable - building env-admin/Dockerfile locally instead")
            local_tag = "sirosid-env-admin:local"
            fly.docker("build", "-q", "-f", "env-admin/Dockerfile", "-t", local_tag, ".", cwd=resources.root)
            image = fly.push_local_image(app, local_tag)
            deploy_args[deploy_args.index("-i") + 1] = image
        # One app-scoped deploy token per Mongo consumer, minted fresh every
        # run: enough for the Machines API on that app, nothing else in the
        # org. Stored as ONE secret (a JSON map) so env-admin's config stays a
        # single file.
        consumers = [c for c in ["wallet-backend", "vc-registry", "vc-issuer", "vc-verifier", "vc-apigw"]]
        if conformance:
            consumers.append("conformance-server")
        tokens = {naming.app(c): fly.create_deploy_token(naming.app(c)) for c in consumers if fly.app_exists(naming.app(c))}
        fly.ensure_secret(app, "flyApiTokens", json.dumps(tokens), force=True)
        fly.ensure_secret(app, "envAdminToken", persistent_secret(out_dir, "adminToken"))
        fly.ensure_secret(app, "mongoUri",
                      f"mongodb://root:{mongo_password}@{naming.internal('mongodb')}:27017/?authSource=admin",
                      force=True)
        deploy_args += [
            "--env", "ENV_ADMIN_PLATFORM=fly",
            "--env", f"ENV_ADMIN_ENV_NAME={env}",
            "--env", "ENV_ADMIN_TOKEN_FILE=/run/secrets/envAdminToken",
            "--env", "MONGO_URI_FILE=/run/secrets/mongoUri",
            "--env", "FLY_API_TOKENS_FILE=/run/secrets/flyApiTokens",
            # Every non-system database - this Mongo serves only this environment.
            "--env", "MONGO_DATABASES=*",
            "--env", "CONSUMERS=" + json.dumps([{"name": c, "target": naming.app(c)} for c in consumers]),
            # Bootstrap after a reset - the same three values
            # register_vc_services() uses at deploy time.
            "--env", f"ADMIN_URL={naming.url('wallet-proxy')}",
            "--env", f"ISSUER_URL={naming.url('vc-apigw')}",
            "--env", f"VERIFIER_URL={naming.url('vc-verifier')}",
            "--file-secret", "/run/secrets/envAdminToken=envAdminToken",
            "--file-secret", "/run/secrets/mongoUri=mongoUri",
            "--file-secret", "/run/secrets/flyApiTokens=flyApiTokens",
        ]
    elif name == "conformance-runner":
        # Same FRONTEND_URL/ADMIN_URL/ADMIN_TOKEN values already printed in
        # main()'s "run sirosid-tests manually" summary block below - this
        # just automates the same thing sirosid-tests' own Makefile targets
        # do by hand, from the dashboard. ADMIN_TOKEN goes through
        # fly.ensure_secret() (becomes a real env var once set via `flyctl
        # secrets set`, same as jwtSecret/adminToken on wallet-backend) -
        # not --env, since it's a credential, not a plain URL.
        fly.ensure_secret(app, "ADMIN_TOKEN", persistent_secret(out_dir, "adminToken"))
        deploy_args += [
            "--env", f"CONFORMANCE_URL={naming.url('conformance')}",
            "--env", f"FRONTEND_URL={naming.url('wallet-frontend')}",
            "--env", f"ADMIN_URL={naming.url('wallet-proxy')}",
            # helpers/vc-services.ts's checkVCServicesHealth() (used by the
            # issuer/verifier specs) defaults to localhost:900x - meaningless
            # from inside a Fly machine. Override with 6PN .internal
            # addresses (reachable regardless of whether the target has a
            # public Fly URL too - vc-issuer doesn't, see COMPONENTS).
            "--env", f"VC_ISSUER_URL=http://{naming.internal('vc-issuer')}:8080",
            "--env", f"VC_VERIFIER_URL=http://{naming.internal('vc-verifier')}:8080",
            "--env", f"VC_APIGW_URL=http://{naming.internal('vc-apigw')}:8080",
            # Conformance suite's self-signed cert - matches docker-compose.conformance.yml locally.
            "--env", "NODE_TLS_REJECT_UNAUTHORIZED=0",
        ]

    fly.deploy(deploy_args, cwd=resources.root)
    if "mount" in comp:
        fly.assert_volume_mounted(app, comp["mount"]["volume"])
    fly.ensure_running(app)

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
        fly.wait_for_checks(app)

    if primary_public_port is not None or tcp_passthrough_port is not None:
        say(f"{name}: {naming.url(name)}")


def register_vc_services(naming: Naming, admin_token: str, register, say=print, sleep=None):
    """Register this instance's vc-apigw and vc-verifier with wallet-backend's
    default tenant, through the caller's `register` callable (scripts/bootstrap.py
    in the CLI - the same code `make up` and env-admin's storage reset run, so the
    three cannot drift).

    Why it matters: PDP's whitelist governs who is TRUSTED to issue/verify; it
    does not populate the wallet's own list of available issuers/verifiers. An
    instance with nothing registered looks completely healthy and fails only when
    a user tries to add a credential.

    "Already registered" is the normal outcome of a redeploy, not a failure -
    `register` handles it. It retries for a while since wallet-proxy's public
    DNS/TLS can take a few seconds to become reachable right after its own deploy
    returns. Loud and fatal when it gives up: wallet-backend crash-looping while
    this ran once made registration silently do nothing and the deploy still
    reported success.
    """
    env = naming.env
    proxy_url = naming.url("wallet-proxy")
    apigw_url = naming.url("vc-apigw")
    verifier_url = naming.url("vc-verifier")
    last_err = None
    for _ in range(15):
        try:
            summary = register(proxy_url, admin_token, apigw_url, verifier_url)
            say(f"vc-apigw ({apigw_url}): {summary['issuer']}; vc-verifier ({verifier_url}): {summary['verifier']}")
            return summary
        except RegistrationError as e:
            last_err = e
            (sleep or time.sleep)(4)
    raise DeployError(
        f"\nERROR: could not register VC services with wallet-backend after retries ({last_err}).\n"
        f"  The environment is deployed but the wallet may have NO issuers or verifiers, so signup and\n"
        f"  credential issuance will fail. Check wallet-backend is actually serving:\n"
        f"    flyctl logs -a {naming.app('wallet-backend')}\n"
        f"  then re-run the deploy (idempotent), or register by hand:\n"
        f"    python3 scripts/bootstrap.py --admin-url {proxy_url} --admin-token <adminToken> "
        f"--issuer-url {apigw_url} --verifier-url {verifier_url}")


def resolve_image(comp: dict, spec: InstanceSpec, docs: list, mongo_version: str) -> str:
    """The image a component would run: an explicit override, else the merged
    `images` values, else the component's own default."""
    name = comp["name"]
    if name in spec.images:
        return spec.images[name]
    if "image_from_values" in comp:
        return extract_image(docs, comp["image_from_values"])
    return comp["image"].format(mongo_version=mongo_version)


def deploy_order(components: list, conformance: bool) -> list:
    """Deploy order. There is no `depends_on` on Fly, so components go strictly in
    sequence and each `fly deploy` blocks until its machine is healthy.

    With conformance the three conformance apps are interleaved: they must exist
    before wallet-frontend (its nginx statically proxy_passes to
    conformance-server and conformance-runner, and a static target that does not
    resolve stops nginx starting AT ALL), env-admin goes after them (it mints a
    deploy token for conformance-server), and the public `conformance` nginx front
    goes last (it needs conformance-server's machine to look up its private IP).
    """
    if not conformance:
        return list(components)
    non_frontend = [c for c in components if c["name"] not in ("wallet-frontend", "env-admin")]
    env_admin = [c for c in components if c["name"] == "env-admin"]
    frontend = [c for c in components if c["name"] == "wallet-frontend"]
    conf_before, conf_after = [], []
    for c in CONFORMANCE_COMPONENTS:
        (conf_after if c["name"] == "conformance" else conf_before).append(c)
    return non_frontend + conf_before + env_admin + frontend + conf_after


def deploy_instance(spec: InstanceSpec, fly: FlyClient, naming: Naming, resources: Resources, *,
                    chart_dir: Path = None, rendered_root: Path = None, identities: list = None,
                    components: list = None, register=None, progress=print,
                    render_only: bool = False) -> DeployResult:
    """Deploy the instance `spec` describes.

    identities: Android signing identities (from identities_from_entries or the
    CLI's local files); default is spec.android_apps alone. components: the
    registry to deploy, default built from the pins in values-fly.yaml.
    render_only: render config and resolve images, deploy nothing.

    Idempotent: a redeploy reuses the state in rendered_root/fly-<env> (see
    state.py), creates nothing that exists and never rotates a generated secret.
    Raises DeployError; components that already deployed are left running (each
    is independently a working app; only the sequence is incomplete), and
    DeployError.deployed says which.
    """
    say = progress or (lambda msg: None)
    if not spec.region:
        raise DeployError("InstanceSpec.region is empty - resolve a region before deploying")
    env = naming.env
    rendered_root = Path(rendered_root) if rendered_root else resources.rendered
    out_dir = rendered_root / f"fly-{env}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if identities is None:
        try:
            identities = identities_from_entries(spec.android_apps)
        except ValueError as e:
            raise DeployError(str(e)) from e
    if components is None:
        components = build_components(
            resources.image_pin("miniOidc", "ghcr.io/sirosfoundation/mini-oidc:0.0.4"),
            resources.image_pin("envAdmin", "ghcr.io/sirosfoundation/sirosid-env-admin:0.1.0"))

    # Persisted per environment: the Mongo volume's data was initialised with it
    # and MONGO_INITDB_ROOT_* never re-applies to a non-empty /data/db.
    mongo_password = resolve_mongo_password(fly, naming, out_dir, say)

    say(f"=== Rendering config for environment '{env}' ===")
    # docs is the full rendered manifest (not just wallet-backend/pdp) - reused
    # below for image refs, the mongo version and wallet-frontend's Android/iOS
    # wellknown values, instead of a second `helm template` call. Each identity's
    # own origin is added to wallet-backend's rp_origins (the server-side
    # WebAuthn accept-list) - NOT the same thing as assetlinks.json, generated
    # below; a debug/sideloaded build's passkeys need both.
    docs = render("fly", Path(chart_dir) if chart_dir else resources.chart_dir, env=env,
                  android_apk_key_hashes=[i["apk_key_hash"] for i in identities],
                  out_dir=rendered_root, mongo_password=mongo_password,
                  conformance=spec.conformance, extra_trusted_issuers=spec.trusted_issuers,
                  wallet_attestation=spec.wallet_attestation, extra_trusted_verifiers=spec.trusted_verifiers,
                  extra_trusted_verifier_roots=spec.trusted_verifier_roots,
                  rical_provider_url=spec.rical_provider_url or None,
                  rical_root_certificate_pem=spec.rical_root_pem or None,
                  zk_circuits_sources=spec.zk_circuits_sources, dc_api_enable=spec.dc_api_enable,
                  credential_registries=spec.credential_registries, env_values=spec.values,
                  bbs_secret_key=spec.bbs_secret_key or None, naming=naming, resources=resources,
                  say=say, warn=say)
    mongo_version = extract_image(docs, "mongoCommunityVersion")
    all_components = deploy_order(components, spec.conformance)
    images = {c["name"]: resolve_image(c, spec, docs, mongo_version) for c in all_components}
    result = DeployResult(out_dir=out_dir, docs=docs, images=images, mongo_password=mongo_password)
    if render_only:
        result.rendered_only = True
        return result

    ctx = DeployContext(fly=fly, naming=naming, resources=resources, spec=spec, say=say, docs=docs,
                        mongo_version=mongo_version, out_dir=out_dir, mongo_password=mongo_password)
    say("=== Generating per-environment PKI ===")
    ctx.pki_dir = generate_pki(ctx)
    say("=== Generating Android assetlinks.json ===")
    # No separate iOS asset: wallet-frontend's own image generates its complete
    # apple-app-site-association from WELLKNOWN_APPLE_APPIDS (_wallet_frontend_env),
    # served at its own domain.
    ctx.assetlinks_path = generate_android_assets(ctx, identities)
    if spec.images:
        say(f"=== Image overrides for this environment: {spec.images} ===")

    say(f"=== Deploying {len(all_components)} apps to Fly (org: {fly.org}) ===")
    for comp in all_components:
        say(f"--- {comp['name']} ---")
        try:
            deploy_component(ctx, comp)
        except FlyDeployError as e:
            raise DeployError(f"Deploy failed at '{comp['name']}' (exit {e.returncode})", component=comp["name"],
                              deployed=result.deployed, returncode=e.returncode) from e
        except (FlyError, DeployError) as e:
            raise DeployError(str(e), component=comp["name"], deployed=result.deployed) from e
        result.deployed.append(comp["name"])

    result.admin_token = persistent_secret(out_dir, "adminToken")
    if register is not None:
        say("=== Registering VC services with wallet-backend's default tenant ===")
        register_vc_services(naming, result.admin_token, register, say)
    for comp in all_components:
        if any(p["public"] for p in comp["ports"]):
            result.urls[comp["name"]] = naming.url(comp["name"])
    return result
