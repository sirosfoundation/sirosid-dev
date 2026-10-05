#!/usr/bin/env python3
"""Spin up a named Fly.io environment for sirosid-dev: `make fly-up ENV=<name>`.

Deploys 11 Fly apps under sirosfoundation, prefixed `sirosid-<env>-`: mongodb,
mini-oidc, vc-registry, vc-issuer, vc-verifier, vc-apigw, pdp, wallet-backend,
wallet-proxy, env-admin, wallet-frontend - see scripts/fly_common.py's COMPONENTS table
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

Mongo data lives on a Fly volume (fly_common.ensure_volume / the `mount` on
the mongodb and conformance-mongodb components), so it survives redeploys,
image bumps and host maintenance. Consequences handled here: the root
password is persisted per environment instead of rotated per run
(resolve_mongo_password), a volume pins the app to its region (ensure_volume
refuses to move), and `fly-down` deletes the data unless `--keep-data`.
Clearing the data without a teardown is env-admin's job (the dashboard's
"Clear all data", `make fly-storage-clear`). PKI (vc-services signing keys)
and the WebAuthn AS signing key are generated fresh per environment rather
than reusing sirosid-dev's shared local dev PKI, since Fly environments are
reachable over the public internet.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bootstrap  # noqa: E402
import fly_common  # noqa: E402
from android_apps import load_android_apps  # noqa: E402
from env_config import load_environment_config, merge_images, merge_list  # noqa: E402
from fly_common import COMPONENTS, FLY_ORG, FLY_REGION_FALLBACK, detect_region  # noqa: E402
from sirosid_core.deploy import DeployError, RegistrationError, deploy_instance  # noqa: E402
from sirosid_core.fly import FlyClient, FlyError  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402,F401
from sirosid_core.resources import Resources  # noqa: E402
from sirosid_core.spec import InstanceSpec  # noqa: E402
from vc_render import deep_merge  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


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



def _spec_from_args(args, env_cfg) -> InstanceSpec:
    """The CLI's half of building an InstanceSpec: merge environments/<env>.yaml
    with the command line (file first, CLI on top - see scripts/env_config.py),
    and READ every file the spec refers to, so what comes out holds content and
    the deploy code below never touches the developer's disk to find out what
    to deploy. A service builds an InstanceSpec directly instead.

    Region is left empty: resolving it consults the environment, a personal
    dotfile and the network, which is the caller's business.
    """
    # environments/<env>.yaml (if present) supplies persisted defaults for a
    # named, durable environment - see scripts/env_config.py's module doc
    # for the full precedence (file first, CLI appends/overrides on top).
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

    # Ordered: later registries override earlier ones for the same
    # vct/doctype, so the file's list comes first and a CLI one extends it.
    credential_registries = merge_list(
        env_cfg["credential_registries"],
        [u.strip() for u in args.credential_registries.split(",") if u.strip()])

    # The issuer's blind BBS secret key, read from a gitignored local file.
    #
    # It cannot live in environments/<name>.yaml: that file is committed and
    # this is the whole of the issuer's BBS signing capability. It goes to
    # the renderer as a secret OVERRIDE rather than into the values tree,
    # which is how the chart already models it - issuer-core's
    # secrets.yaml.template carries ${BBS_SECRET_KEY}, and render_vc
    # substitutes it exactly the way Kubernetes' secrets-renderer
    # initContainer would. So the key never reaches a values file or a
    # rendered ConfigMap on either target.
    bbs_secret_key = None
    bbs_secret_key_file = env_cfg["bbs_secret_key_file"] or None
    if bbs_secret_key_file:
        path = Path(bbs_secret_key_file)
        if not path.is_absolute():
            path = SIROSID_DEV_ROOT / path
        if not path.exists():
            raise SystemExit(
                f"bbs_secret_key_file {path} does not exist. Run `make bbs-keys` to generate the "
                "issuer's BBS key pair (it is gitignored, so a fresh checkout has none)."
            )
        bbs_secret_key = path.read_text().strip()
        if not bbs_secret_key:
            # An empty string would be dropped by the `if bbs_secret_key`
            # guard in the renderer and the issuer would boot with the
            # chart's blank placeholder - a failure that says nothing about
            # this file.
            raise SystemExit(
                f"bbs_secret_key_file {path} is empty. Re-run `make bbs-keys`."
            )

    # The public half, by contrast, goes straight into the values tree: it is
    # not secret, and the chart wants it as issuer.core.bbs.publicKey. Read
    # from a file for the same reason the secret is - `make bbs-keys` then
    # covers both, and environments/<name>.yaml stays free of a key that
    # differs per developer.
    env_values = env_cfg.get("values") or {}
    bbs_public_key_file = env_cfg["bbs_public_key_file"] or None
    if bbs_public_key_file:
        path = Path(bbs_public_key_file)
        if not path.is_absolute():
            path = SIROSID_DEV_ROOT / path
        if not path.exists():
            raise SystemExit(
                f"bbs_public_key_file {path} does not exist. Run `make bbs-keys` to generate the "
                "issuer's BBS key pair (it is gitignored, so a fresh checkout has none)."
            )
        bbs_public_key = path.read_text().strip()
        if not bbs_public_key:
            raise SystemExit(
                f"bbs_public_key_file {path} is empty. Re-run `make bbs-keys`."
            )
        env_values = deep_merge(
            env_values,
            {"issuer": {"core": {"bbs": {"publicKey": bbs_public_key}}}},
        )

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

    host_pattern = args.host_pattern or env_cfg["host_pattern"] or "{app}.fly.dev"
    if args.single_machine:
        if args.conformance:
            raise SystemExit("--single-machine does not support --conformance (its nginx image hard-codes "
                             "server:8080); deploy it in the default layout")
        if not (args.host_pattern or env_cfg["host_pattern"]):
            # One app has ONE <app>.fly.dev name, so every public component needs
            # its own host on a domain routed to the app. Without one, these
            # synthetic names only work with an explicit Host header against
            # https://<app>.fly.dev (curl -H 'Host: ...').
            host_pattern = "{component}-{id}.sm.invalid"
            print(f"--single-machine: no --host-pattern, using synthetic hosts {host_pattern!r} (not in DNS; "
                  "reach them with a Host header against https://<app>.fly.dev)")

    return InstanceSpec(
        env=args.env,
        images=image_overrides,
        conformance=bool(args.conformance),
        wallet_attestation=bool(args.wallet_attestation),
        trusted_issuers=extra_trusted_issuers,
        trusted_verifiers=extra_trusted_verifiers,
        trusted_verifier_roots=extra_trusted_verifier_roots,
        zk_circuits_sources=zk_circuits_sources,
        rical_provider_url=rical_provider_url or "",
        rical_root_pem=rical_root_certificate_pem or "",
        dc_api_enable=dc_api_enable,
        credential_registries=credential_registries,
        android_apps=merge_list(env_cfg["android_apps"], args.android_app or []),
        values=env_values,
        bbs_secret_key=bbs_secret_key or "",
        app_prefix=args.app_prefix or env_cfg["app_prefix"] or "sirosid",
        host_pattern=host_pattern,
        scale_to_zero=bool(args.scale_to_zero),
        # env-admin restarts per-app machines with per-app deploy tokens; a
        # single machine has neither, so the layout implies --no-env-admin.
        env_admin=not (args.no_env_admin or args.single_machine),
        **({"layout": "single-machine", "public_ips": not args.no_public_ips} if args.single_machine else {}),
    ).validate([c["name"] for c in COMPONENTS])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--region", default="",
                         help="Pin this run's Fly region (e.g. 'arn'), overriding "
                              "environments/<name>.yaml's `region:`, $FLY_REGION, .fly-region, and "
                              "Fly's own detected suggestion - see the region-resolution comment below.")
    parser.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT / "chart"))
    parser.add_argument("--scale-to-zero", action="store_true",
                        help="Create the environment so that it can be scaled to zero AS A UNIT "
                             "(make fly-stop / fly-start): no machine wakes itself on traffic, so a "
                             "stopped environment stays stopped until fly-start brings it all up in "
                             "order. Per-app autostop cannot work for this stack - Fly never wakes a "
                             "machine for an internal 6PN call.")
    parser.add_argument("--no-env-admin", action="store_true",
                        help="Do not deploy env-admin (and drop its dashboard Storage card and nginx proxy). It needs "
                             "one app-scoped deploy token per consumer, which an org-scoped credential cannot mint.")
    parser.add_argument("--single-machine", action="store_true",
                        help="Deploy the instance as ONE Fly app with ONE multi-container machine "
                             "(sirosid_core/singlemachine.py) instead of one app per component. The app is "
                             "<prefix>-<env>; public components are routed by Host, so --host-pattern must be "
                             "flat ('{component}-{id}.<domain>') on a domain routed to the app (default: "
                             "synthetic '*.sm.invalid' names, testable with a Host header against "
                             "https://<app>.fly.dev). Implies --no-env-admin; --conformance is refused.")
    parser.add_argument("--no-public-ips", action="store_true",
                        help="With --single-machine: allocate no public IPs and stay on the org's default "
                             "network - the instance is then only reachable through a shared edge app that "
                             "fly-replays to it, whose forwarded headers its front nginx trusts.")
    parser.add_argument("--org", default="",
                        help="Fly organization to create the apps in (default: sirosfoundation). Use a "
                             "dedicated org for scratch or hosted instances; fly-down needs the same --org.")
    parser.add_argument("--app-prefix", default="",
                        help="Prefix of this environment's Fly app names (default 'sirosid': apps are "
                             "<prefix>-<env>-<component>). Fly app names are global across all orgs.")
    parser.add_argument("--host-pattern", default="",
                        help="Format string for each component's PUBLIC hostname; fields {app}, {env}, "
                             "{component}. Default '{app}.fly.dev'. Use it to serve an environment on a "
                             "domain you own, e.g. '{env}-{component}.dev.example.org' - every URL, "
                             "OAuth redirect, issuer identifier and passkey rp_id follows. The DNS "
                             "records and TLS in front of the apps are NOT created by fly-up.")
    parser.add_argument("--rendered-root", default="",
                        help="Directory under which the environment's working directory (fly-<env>/) is "
                             "created: rendered configs, generated secrets and PKI. Default "
                             "fixtures/rendered. Point it at a scratch directory to deploy without touching "
                             "your persistent state; see sirosid_core/state.py for which files are state.")
    parser.add_argument("--render-only", action="store_true",
                        help="Render config into fixtures/rendered/fly-<env>/ and print the image each "
                             "component would run, then stop before touching Fly. Answers 'what would "
                             "this deploy change?' - diff the output against the running machines.")
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
    parser.add_argument("--credential-registries", default="",
                        help="Comma-separated registry base URLs (the Makefile's "
                             "CREDENTIAL_REGISTRIES, passed only for REGISTRY=external).")
    parser.add_argument("--dc-api-enable", default="", choices=["", "true", "false"],
                         help="Override verifier.digital_credentials.enable (W3C DC API support) for "
                              "this environment only - fixtures/vc-config.yaml's own default is "
                              "true for every environment. Last-one-wins vs. "
                              "environments/<env>.yaml's dc_api_enable, same as the RICAL flags above.")
    args = parser.parse_args()

    env_cfg = load_environment_config(args.env)
    spec = _spec_from_args(args, env_cfg)
    naming = spec.naming()

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
    spec.region = region

    if not shutil.which("flyctl"):
        raise SystemExit("flyctl not found - install it first (https://fly.io/docs/flyctl/install/)")

    chart_dir = Path(args.chart_dir)
    rendered_root = Path(args.rendered_root) if args.rendered_root else SIROSID_DEV_ROOT / "fixtures" / "rendered"
    out_dir = rendered_root / f"fly-{args.env}"
    identities = load_android_apps(extra=spec.android_apps)
    fly_client = FlyClient(args.org) if args.org else fly_common._client

    def register(admin_url, admin_token, issuer_url, verifier_url):
        # scripts/bootstrap.py is the one implementation `make up`, env-admin's
        # storage reset and this share; the library only knows "not ready yet".
        try:
            return bootstrap.register(admin_url, admin_token, issuer_url, verifier_url)
        except bootstrap.BootstrapError as e:
            raise RegistrationError(str(e)) from e

    if spec.layout == "single-machine":
        return _single_machine(args, spec, naming, fly_client, chart_dir, rendered_root, identities, register)

    try:
        result = deploy_instance(spec, fly_client, naming, Resources(SIROSID_DEV_ROOT),
                                 chart_dir=chart_dir, rendered_root=rendered_root, identities=identities,
                                 components=COMPONENTS, register=register, render_only=args.render_only)
    except DeployError as e:
        if e.component and e.returncode is not None:
            # No auto-rollback - components deployed so far are left running (each
            # is independently a fine, working app; only the *sequence* is
            # incomplete), since silently tearing down a partially-up environment
            # the operator may still want to inspect/debug is worse than leaving
            # it and saying so clearly.
            print(file=sys.stderr)
            print(f"=== Deploy failed at '{e.component}' (exit {e.returncode}) ===", file=sys.stderr)
            print(f"Already deployed and left running: {', '.join(e.deployed) or '(none)'}", file=sys.stderr)
            print(f"Re-running 'make fly-up ENV={args.env}' redeploys everything from the top "
                  "(safe - already-succeeded components are idempotent), or clean up with: "
                  f"make fly-down ENV={args.env}", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(str(e))
    except FlyError as e:
        raise SystemExit(str(e))

    if args.render_only:
        print("=== Images (--render-only: nothing deployed) ===")
        for comp in COMPONENTS:
            print(f"  {comp['name']:<18} {result.images[comp['name']]}")
        print(f"config written to {out_dir}")
        return

    print()
    print(f"=== Environment '{args.env}' is up ===")
    for name, url in result.urls.items():
        print(f"  {name}: {url}")
    print()
    print("To run sirosid-tests' CDP-based WebAuthn conformance specs against this")
    print("environment instead of localhost (see sirosid-tests/specs/conformance/):")
    print(f"  export FRONTEND_URL={naming.url('wallet-frontend')}")
    print(f"  export ADMIN_URL={naming.url('wallet-proxy')}")
    print(f"  export ADMIN_TOKEN={result.admin_token}")
    if spec.conformance:
        print(f"  export CONFORMANCE_URL={naming.url('conformance')}")
        print("  export NODE_TLS_REJECT_UNAUTHORIZED=0  # conformance suite's self-signed cert")
        print()
        print("Or run them from the dashboard's Conformance tab (same specs, driven by")
        print(f"conformance-runner): {naming.url('wallet-frontend')}")
    print()
    print(f"Storage: Mongo data persists on a Fly volume across redeploys. Clear it from the dashboard's")
    print(f"Storage card, or: make fly-storage-clear ENV={args.env}")
    print(f"Tear down with: make fly-down ENV={args.env}   (KEEP_DATA=yes keeps the volume for the next fly-up)")


def _single_machine(args, spec, naming, fly_client, chart_dir, rendered_root, identities, register):
    """--single-machine: the same spec, deployed as one app with one machine."""
    import time
    from sirosid_core.singlemachine import deploy_instance_single_machine
    t0 = time.monotonic()
    try:
        result = deploy_instance_single_machine(
            spec, fly_client, naming, Resources(SIROSID_DEV_ROOT), machines=fly_common.machines_client(),
            chart_dir=chart_dir, rendered_root=rendered_root, identities=identities, components=COMPONENTS,
            register=register, render_only=args.render_only)
    except DeployError as e:
        print(file=sys.stderr)
        print(f"=== Single-machine deploy failed at '{e.component}' ===", file=sys.stderr)
        print(f"Completed steps: {', '.join(e.deployed) or '(none)'}", file=sys.stderr)
        raise SystemExit(str(e))
    out_dir = rendered_root / f"fly-{args.env}"
    if args.render_only:
        print("=== Images (--render-only: nothing deployed) ===")
        for name, image in result.images.items():
            print(f"  {name:<18} {image}")
        print(f"machine config ({result.machine['config_bytes']} bytes) written to {out_dir / 'machine-config.json'}")
        return
    app = naming.machine_app()
    print()
    print(f"=== Single-machine instance '{args.env}' is up: app {app}, machine {result.machine['id']} "
          f"({time.monotonic() - t0:.0f}s) ===")
    for name, state in sorted(result.machine["containers"].items()):
        print(f"  container {name:<16} {state}")
    for name, url in result.urls.items():
        print(f"  {name}: {url}")
    if spec.public_ips:
        print(f"Until those hosts are in DNS: curl -H 'Host: {naming.host('vc-apigw')}' https://{app}.fly.dev/health")
    print(f"  export ADMIN_TOKEN={result.admin_token}")
    print(f"Stop/start: make fly-stop/fly-start ENV={args.env} (detected as single-machine); "
          f"tear down: make fly-down ENV={args.env}")


if __name__ == "__main__":
    main()
