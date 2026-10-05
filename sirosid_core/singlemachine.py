"""The single-machine layout: one instance = ONE Fly app with ONE multi-container machine.

The apps layout (deploy.py) gives every component its own Fly app, ~11 of them,
each deployed with `flyctl deploy`. Here the same components - same images, same
rendered configs - become containers of one machine created through the
Machines API (machines.py). What changes, and why:

* Ports. Containers share one network namespace, so every component listens on
  its own port (naming.SINGLE_MACHINE_PORTS) and siblings reach each other on
  127.0.0.1 (Naming.addr). The renderer moves the vc services and the PDP through
  the chart's extraConfig and wallet-backend through its config.
* Routing. One app has one <app>.fly.dev name, so a front nginx server (inside
  wallet-frontend's nginx) is the only public listener, routing by Host (assets.front_nginx_conf).
  wallet-proxy folds into it. Per-component hostnames need a domain routed to
  this app; until then `<app>.fly.dev` answers as wallet-proxy.
* Large files. The whole machine config is capped at ~1 MiB and the vctms alone
  are ~350 KB for three containers, so vctms / pres-reqs / documents / branding
  ship as ONE per-instance config-bundle image (oci.py, pushed to
  registry.fly.io/<app> without docker), mounted read-only at BUNDLE_DIR in the
  containers that need it; the rendered configs are rewritten to point there.
  Everything small (configs, certs, nginx confs, the dashboard) is inlined.
* Secrets. App secrets do not reach the containers of an API-created machine,
  and a file-from-secret (`files[].secret_name`) is not materialised for
  containers either (both verified on real Fly). Each secret is therefore an app
  secret listed in the `secrets` of one tiny init container, which decodes them
  into per-consumer in-memory temp_dir volumes; consumers mount theirs at
  SECRETS_DIR and wait for it (depends_on exited_successfully).
* Health and order. Fly's `depends_on` replaces deploy order: mongo consumers
  wait for mongodb healthy, vc-apigw for vc-issuer and vc-registry, everything
  that asks the PDP for it. Each container's healthcheck is its apps-layout check
  on the remapped port, and every container restarts on its own (an OOM kill
  takes the largest process, Pilot restarts just that container).
* What is NOT supported: env-admin (it restarts per-app machines; a service
  resets an instance itself) and the conformance suite (its nginx image
  hard-codes `server:8080`). Both are refused up front.

An image change to ANY container reboots the whole machine (Fly has no
per-container update), so an unchanged config is detected (a hash in the
machine's metadata) and not re-applied.
"""
import base64
import hashlib
import json
import time
from pathlib import Path

import yaml

from .android import identities_from_entries
from .assets import (assetlinks_json, front_nginx_conf, mini_oidc_config, wallet_frontend_conf,
                     wallet_frontend_dashboard_html)
from .components import build_components, volume_name
from .deploy import (DeployContext, DeployError, DeployResult, generate_android_assets, generate_pki,
                     mini_oidc_env, register_vc_services, resolve_image, wallet_frontend_env)
from .fly import FlyClient, FlyError
from .helm import HelmError, extract_configmap_data, extract_image
from .machines import MachinesClient, MachinesError
from .naming import LAYOUT_SINGLE_MACHINE, Naming
from .oci import Registry, RegistryError
from .render import render
from .resources import Resources
from .spec import InstanceSpec
from .state import persistent_secret

# The Machines API refused a 1,056,412-byte body and took 989,744 (real Fly,
# 2026-10). Stay clearly under the line.
MAX_CONFIG_BYTES = 960 * 1024

BUNDLE_DIR = "/sirosid/bundle"
SECRETS_DIR = "/run/sirosid-secrets"
# Directories of the rendered output that go into the bundle image, and the
# absolute path each one had in the apps layout (what the rendered configs say).
BUNDLE_DIRS = {"vctms": "/vctms", "pres-reqs": "/pres-reqs", "documents": "/documents",
               "branding-assets": "/branding-assets"}

# Every DISTINCT container image, every image volume and every Fly volume is a
# guest block device, and a machine has few: the API refuses a 15th drive ("only
# 14 slots are available (/dev/vdc through /dev/vdp)") and Firecracker already
# fails to boot at 13 (`AttachDevice(MmioTransport(Allocator(ResourceNotAvailable)))`,
# the machine then sits in `created` or stops). Measured on real Fly 2026-10-05:
# 12 images, or 11 images + a volume, boot; 12 + a volume does not; memory
# temp_dirs eat into the same budget (10 images + image volume + 3 temp_dirs +
# volume failed, 9 + the same booted). Hence: the init container reuses the mongo
# image and the front nginx lives inside wallet-frontend's nginx.
MAX_BLOCK_DEVICES = 12
MAX_DEVICES_WITH_TEMP_DIRS = 14
GUEST = {"cpu_kind": "shared", "cpus": 4, "memory_mb": 8192}

# The components that become containers, in the apps layout's deploy order.
# wallet-proxy is served by `front`; env-admin is not supported here.
CONTAINER_COMPONENTS = ("mongodb", "mini-oidc", "vc-registry", "vc-issuer", "vc-verifier", "vc-apigw", "pdp",
                        "wallet-backend", "wallet-frontend")
PUBLIC_COMPONENTS = ("mini-oidc", "vc-registry", "vc-verifier", "vc-apigw", "wallet-proxy", "wallet-frontend")
VC_SERVICES = ("vc-registry", "vc-issuer", "vc-verifier", "vc-apigw")

# Fly secret name -> (state source, [(secret group, file name), ...]). The value
# set on Fly is the base64 of the content; the init container decodes it. A
# "group" is one temp_dir volume: the containers mounting it see only its files.
SECRETS = {
    "SIROSID_MONGO_ROOT_PASSWORD": ("mongoRootPassword", [("mongodb", "mongoRootPassword")]),
    "SIROSID_VC_SIGNING_KEY": ("vc-pki/signing_ec_private.pem",
                               [("vc", "signing_ec_private.pem"), ("wallet_backend", "asSigningKey")]),
    "SIROSID_JWT_SECRET": ("jwtSecret", [("wallet_backend", "jwtSecret")]),
    "SIROSID_ADMIN_TOKEN": ("adminToken", [("wallet_backend", "adminToken")]),
    "SIROSID_WALLET_PROVIDER_KEY": ("vc-pki/wallet_provider_ec_private.pem",
                                    [("wallet_backend", "walletProviderKey")]),
}
SECRET_GROUP = {"mongodb": "mongodb", "wallet-backend": "wallet_backend",
                **{c: "vc" for c in VC_SERVICES}}

# Absolute paths of the apps layout that became something else here.
SECRET_PATHS = {
    "vc": {"/pki/signing_ec_private.pem": f"{SECRETS_DIR}/signing_ec_private.pem"},
    "wallet_backend": {"/main-secrets/jwtSecret": f"{SECRETS_DIR}/jwtSecret",
                       "/main-secrets/adminToken": f"{SECRETS_DIR}/adminToken",
                       "/main-secrets/walletProviderKey": f"{SECRETS_DIR}/walletProviderKey",
                       "/as-cert/tls.key": f"{SECRETS_DIR}/asSigningKey"},
}


def _b64(data) -> str:
    return base64.b64encode(data if isinstance(data, bytes) else data.encode()).decode()


def _file(path: str, content) -> dict:
    return {"guest_path": path, "raw_value": _b64(content)}


def strip_nginx_comments(text: str) -> str:
    """The generators' nginx configs carry long rationale comments written for
    the apps layout (they talk about .internal names and *.fly.dev). Shipped
    without them: smaller, and nothing in a single machine reads as if it
    pointed at another app."""
    return "".join(ln for ln in text.splitlines(keepends=True) if not ln.lstrip().startswith("#"))


def rewrite_paths(config, mapping: dict):
    """Every string in a parsed config that IS a mapped path or lies under one,
    re-pointed. Pure; returns a new structure."""
    def one(s):
        for old, new in mapping.items():
            if s == old or s.startswith(old.rstrip("/") + "/"):
                return new + s[len(old):]
        return s
    if isinstance(config, dict):
        return {k: rewrite_paths(v, mapping) for k, v in config.items()}
    if isinstance(config, list):
        return [rewrite_paths(v, mapping) for v in config]
    return one(config) if isinstance(config, str) else config


def _rewritten_yaml(path: Path, mapping: dict) -> str:
    return yaml.dump(rewrite_paths(yaml.safe_load(path.read_text()), mapping), sort_keys=False)


def bundle_files(out_dir: Path) -> dict:
    """{path inside the bundle image: bytes} - the large shared directories."""
    files = {}
    for d in BUNDLE_DIRS:
        for p in sorted((out_dir / d).glob("*")):
            if p.is_file():
                files[f"{d}/{p.name}"] = p.read_bytes()
    return files


def _http_check(name, port, path, grace=10):
    return {"name": name, "http": {"port": port, "method": "GET", "path": path},
            "interval": 10, "timeout": 5, "grace_period": grace, "failure_threshold": 3, "success_threshold": 1}


def _tcp_check(name, port, grace=10):
    return {"name": name, "tcp": {"port": port}, "interval": 10, "timeout": 5, "grace_period": grace,
            "failure_threshold": 3, "success_threshold": 1}


def _container(name, image, **kw) -> dict:
    c = {"name": name, "image": image, "restart": {"policy": "always"}}
    c.update({k: v for k, v in kw.items() if v})
    return c


def _needs(*pairs) -> list:
    return [{"name": n, "condition": cond} for n, cond in pairs]


def build_machine_config(ctx: DeployContext, images: dict, bundle_ref: str, volume_id: str,
                         android_identities: dict, guest: dict = None) -> dict:
    """The machine config for the instance `ctx` describes. Pure apart from
    reading the rendered files under ctx.out_dir. `images`: component -> ref
    (CONTAINER_COMPONENTS)."""
    naming, spec, out_dir, pki_dir = ctx.naming, ctx.spec, ctx.out_dir, ctx.pki_dir
    p = naming.port
    secret_mount = lambda group: [{"name": f"sec_{group}", "path": SECRETS_DIR}]  # noqa: E731
    bundle_mount = [{"name": "bundle", "path": BUNDLE_DIR}]
    bundle_paths = {old: f"{BUNDLE_DIR}/{d}" for d, old in BUNDLE_DIRS.items()}
    after_secrets = ("secrets-init", "exited_successfully")
    containers = []

    # -- init: decode the app secrets this machine needs into per-group volumes
    groups = sorted({g for _, targets in SECRETS.values() for g, _ in targets})
    script = ["set -e"]
    for secret, (_, targets) in SECRETS.items():
        for group, fname in targets:
            script.append(f'printf %s "${secret}" | base64 -d > /out/{group}/{fname}')
    script += ["chmod -R a+rX /out", "echo secrets-init: done"]
    containers.append({
        # The mongo image (it has sh and base64): a distinct init image would
        # cost one of the machine's few block devices (see MAX_BLOCK_DEVICES).
        "name": "secrets-init", "image": images["mongodb"], "cmd": ["sh", "-c", "\n".join(script)],
        "secrets": [{"env_var": s, "name": s} for s in SECRETS],
        "mounts": [{"name": f"sec_{g}", "path": f"/out/{g}"} for g in groups],
        "restart": {"policy": "no"},
    })

    containers.append(_container(
        "mongodb", images["mongodb"],
        # Loopback only, like every other internal listener (Naming.listen).
        cmd=["mongod", "--bind_ip", "127.0.0.1", "--auth"],
        env={"MONGO_INITDB_ROOT_USERNAME": "root",
             "MONGO_INITDB_ROOT_PASSWORD_FILE": f"{SECRETS_DIR}/mongoRootPassword"},
        mounts=secret_mount("mongodb"), depends_on=_needs(after_secrets),
        healthchecks=[_tcp_check("mongodb", p("mongodb"), grace=15)]))

    containers.append(_container(
        "mini-oidc", images["mini-oidc"], cmd=["/usr/local/bin/op"], env=mini_oidc_env(naming),
        files=[_file("/etc/mini-oidc/configs/config.production.yaml", mini_oidc_config(naming.env, naming))],
        healthchecks=[_http_check("mini-oidc", p("mini-oidc"), "/health")]))

    needs = {
        "vc-registry": [("mongodb", "healthy"), ("pdp", "healthy")],
        "vc-issuer": [("mongodb", "healthy"), ("pdp", "healthy"), ("vc-registry", "healthy")],
        "vc-verifier": [("mongodb", "healthy"), ("pdp", "healthy")],
        "vc-apigw": [("mongodb", "healthy"), ("pdp", "healthy"), ("vc-issuer", "healthy"),
                     ("vc-registry", "healthy")],
    }
    for name in VC_SERVICES:
        cfg = _rewritten_yaml(out_dir / f"{name}.yaml", {**SECRET_PATHS["vc"], **bundle_paths})
        files = [_file("/config.yaml", cfg),
                 _file("/pki/rootCA.crt", (pki_dir / "rootCA.crt").read_bytes()),
                 _file("/pki/signing_ec_chain.pem", (pki_dir / "signing_ec_chain.pem").read_bytes())]
        if name == "vc-apigw":
            files.append(_file("/main-config/api_auth_jwks.json", (out_dir / "api_auth_jwks.json").read_bytes()))
        containers.append(_container(
            name, images[name], env={"VC_CONFIG_YAML": "/config.yaml", "SSL_CERT_FILE": "/pki/rootCA.crt"},
            files=files, mounts=bundle_mount + secret_mount("vc"),
            depends_on=_needs(after_secrets, *needs[name]),
            healthchecks=[_http_check(name, p(name), "/health", grace=20)]))

    containers.append(_container(
        "pdp", images["pdp"], cmd=["--config", "/main-config/config.yaml"],
        files=[_file("/main-config/config.yaml", (out_dir / "pdp.yaml").read_text())],
        healthchecks=[_http_check("pdp", p("pdp"), "/healthz", grace=30)]))

    registry_integrated = not (out_dir / "wallet-backend-registry.yaml").exists()
    wb_files = [_file("/app/config.yaml", _rewritten_yaml(out_dir / "wallet-backend.yaml", SECRET_PATHS["wallet_backend"])),
                # Empty but load-bearing, as in the apps layout (see deploy.py).
                _file("/vctms/.keep", "ok"),
                _file("/main-config/walletProviderCert.pem", (pki_dir / "wallet_provider_ec.crt").read_bytes()),
                _file("/main-config/walletProviderCA.pem", (pki_dir / "rootCA.crt").read_bytes())]
    if not registry_integrated:
        wb_files.append(_file("/app/registry.yaml", (out_dir / "wallet-backend-registry.yaml").read_text()))
    for rules in sorted((out_dir / "as-rules").glob("*")):
        wb_files.append(_file(f"/as-rules/{rules.name}", rules.read_bytes()))
    containers.append(_container(
        "wallet-backend", images["wallet-backend"],
        cmd=["--mode=all", "--config=/app/config.yaml"] + ([] if registry_integrated else ["--registry-config=/app/registry.yaml"]),
        files=wb_files, mounts=secret_mount("wallet_backend"),
        depends_on=_needs(after_secrets, ("mongodb", "healthy"), ("pdp", "healthy")),
        healthchecks=[_http_check("wallet-backend", p("wallet-backend", "http"), "/health", grace=20)]))

    fe_data = extract_configmap_data(ctx.docs, "wallet-frontend-main")
    apple_app_ids = [a.strip() for a in fe_data.get("wellknownAppleAppIds", "").split(",") if a.strip()]
    # wallet-frontend's image is an nginx; the front (the public listener, Host
    # routing, wallet-proxy's routes) is a second server file in the SAME nginx
    # rather than a container of its own - one image less (MAX_BLOCK_DEVICES).
    # No depends_on, deliberately: the edge gives up on a machine whose service
    # port does not answer within seconds of starting. The front is checked on
    # the loopback wallet-proxy listener, which answers whatever the Host (the
    # public port refuses unknown hosts behind an edge).
    containers.append(_container(
        "wallet-frontend", images["wallet-frontend"], env=wallet_frontend_env(ctx, android_identities),
        files=[_file("/etc/nginx/conf.d/default.conf",
                     strip_nginx_comments(wallet_frontend_conf(naming.env, False, naming, env_admin=False))),
               _file("/etc/nginx/conf.d/front.conf",
                     strip_nginx_comments(front_nginx_conf(naming, behind_edge=not spec.public_ips))),
               _file("/etc/nginx/well-known/assetlinks.json", ctx.assetlinks_path.read_bytes()),
               _file("/usr/share/nginx/startup.html",
                     wallet_frontend_dashboard_html(naming.env, android_identities, apple_app_ids, None,
                                                    naming=naming, env_admin=False))],
        healthchecks=[_http_check("wallet-frontend", p("wallet-frontend"), "/"),
                      _http_check("front", p("wallet-proxy"), "/.well-known/assetlinks.json", grace=5)]))

    config = {
        "image": images["wallet-frontend"],
        "guest": dict(guest or GUEST),
        "mounts": [{"volume": volume_id, "path": "/data/db", "name": volume_name("mongodb")}],
        "volumes": [{"name": "bundle", "image": bundle_ref}]
                   + [{"name": f"sec_{g}", "temp_dir": {"storage_type": "memory", "size_mb": 1}} for g in groups],
        "containers": containers,
        "services": [{
            "protocol": "tcp", "internal_port": p("front"),
            "ports": [{"port": 443, "handlers": ["tls", "http"]},
                      {"port": 80, "handlers": ["http"], "force_https": True}],
            "autostop": "off", "autostart": not spec.scale_to_zero, "min_machines_running": 1,
        }],
        "restart": {"policy": "always"},
        # What a caller holding only env + prefix (the CLI's fly-power reset)
        # needs to name the instance's public URLs again: see
        # lifecycle.naming_from_machine.
        "metadata": {"sirosid_layout": LAYOUT_SINGLE_MACHINE, "sirosid_env": naming.env,
                     "sirosid_host_pattern": naming.host_pattern,
                     "sirosid_public_ips": "true" if spec.public_ips else "false"},
    }
    config["metadata"]["sirosid_config_hash"] = config_hash(config)
    return config


def config_hash(config: dict) -> str:
    body = dict(config, metadata={k: v for k, v in (config.get("metadata") or {}).items()
                                  if k != "sirosid_config_hash"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def request_size(config: dict, region: str = "", min_secrets_version: int = None) -> int:
    body = {"config": config, "region": region}
    if min_secrets_version is not None:
        body["min_secrets_version"] = min_secrets_version
    return len(json.dumps(body).encode())


def block_devices(config: dict) -> tuple:
    """(drives, temp_dirs): distinct images + image volumes + Fly volumes, and
    the memory temp_dirs, as the machine will need them (see MAX_BLOCK_DEVICES)."""
    images = {c["image"] for c in config["containers"]}
    vols = config.get("volumes") or []
    drives = len(images) + sum(1 for v in vols if "image" in v) + len(config.get("mounts") or [])
    return drives, sum(1 for v in vols if "temp_dir" in v)


def check_block_devices(config: dict):
    drives, temps = block_devices(config)
    if drives > MAX_BLOCK_DEVICES or drives + temps > MAX_DEVICES_WITH_TEMP_DIRS:
        raise DeployError(
            f"the machine would need {drives} block devices (+{temps} temp_dirs); a Fly machine boots with at "
            f"most {MAX_BLOCK_DEVICES} ({MAX_DEVICES_WITH_TEMP_DIRS} counting temp_dirs). Every distinct image "
            f"is one: an image override that splits a shared image costs a slot.", component="config")


def readiness(config: dict) -> dict:
    """{container: "healthy"|"exited"} - what 'up' means for this config."""
    return {c["name"]: ("exited" if c.get("restart", {}).get("policy") == "no" else "healthy")
            for c in config["containers"] if c.get("healthchecks") or c.get("restart", {}).get("policy") == "no"}


def check_unsupported(spec: InstanceSpec, naming: Naming):
    problems = []
    if not naming.single_machine:
        problems.append("naming.layout must be 'single-machine'")
    if spec.env_admin:
        problems.append("env-admin is not supported in the single-machine layout (it restarts per-app "
                        "machines with per-app deploy tokens; there is one machine here). Deploy with "
                        "env_admin=False (fly-up --no-env-admin, implied by --single-machine) and reset "
                        "the instance from outside (fly-storage-clear / ControlPlane.reset_instance)")
    if spec.conformance:
        problems.append("the conformance suite is not supported in the single-machine layout (its nginx "
                        "image hard-codes server:8080)")
    if naming.flat_host_problem():
        problems.append(naming.flat_host_problem())
    if spec.layout != LAYOUT_SINGLE_MACHINE:
        problems.append(f"spec.layout is {spec.layout!r}")
    if problems:
        raise DeployError("cannot deploy a single-machine instance: " + "; ".join(problems))


def resolve_mongo_password_single(fly: FlyClient, machines: MachinesClient, naming: Naming, out_dir: Path,
                                  say) -> str:
    """As deploy.resolve_mongo_password, for the one app: the cache, else the
    running mongodb container (the password is only readable from inside), else
    fresh - but never fresh while a volume with data exists."""
    cached = out_dir / "mongoRootPassword"
    if cached.exists():
        return cached.read_text().strip()
    app = naming.machine_app()
    if fly.app_exists(app) and any(v.get("state") != "destroyed" for v in fly.list_volumes(app)):
        say(f"{app}: no local password cache but a volume exists - reading the password from the mongodb container")
        value = ""
        for m in machines.list_machines(app):
            try:
                out = machines.exec(app, m["id"], ["cat", f"{SECRETS_DIR}/mongoRootPassword"], container="mongodb")
                if out.get("exit_code") == 0:
                    value = (out.get("stdout") or "").strip()
            except MachinesError:
                pass
        if not value:
            raise DeployError(
                f"{app} has a data volume but this machine has no cached Mongo root password and the mongodb "
                f"container could not be read. Deploying a new password would lock every consumer out of the "
                f"data: restore the instance's state (fly-{naming.env}/mongoRootPassword), or destroy the "
                f"instance's data first (fly-down without --keep-data).", component="mongodb")
        cached.write_text(value)
        return value
    return persistent_secret(out_dir, "mongoRootPassword")


def deploy_instance_single_machine(spec: InstanceSpec, fly: FlyClient, naming: Naming, resources: Resources, *,
                                   machines: MachinesClient, chart_dir: Path = None, rendered_root: Path = None,
                                   identities: list = None, components: list = None, register=None,
                                   progress=print, render_only: bool = False, registry: Registry = None,
                                   guest: dict = None, ready_timeout: float = 900,
                                   push_credential: str = "") -> DeployResult:
    """Deploy `spec` as ONE Fly app with ONE multi-container machine.

    Same contract as deploy.deploy_instance: prints nothing (progress callback),
    raises DeployError with .component (the step or container) and .deployed
    (the steps that completed), is idempotent (an unchanged config is not
    re-applied, so a redeploy does not reboot the machine), and returns a
    DeployResult with the public URLs and the instance's admin token.

    machines: the Machines API client (same org and identity as `fly`).
    registry: where the config bundle is pushed; default registry.fly.io with
    an app-scoped deploy token minted for the push and revoked after it (falling
    back to the Machines client's own token if minting is refused).
    """
    say = progress or (lambda msg: None)
    check_unsupported(spec, naming)
    if not spec.region:
        raise DeployError("InstanceSpec.region is empty - resolve a region before deploying")
    env, app = naming.env, naming.machine_app()
    # Absolute: create-pki.sh runs with the fixtures directory as its cwd.
    rendered_root = (Path(rendered_root) if rendered_root else resources.rendered).resolve()
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
    done = []

    def step(name):
        done.append(name)

    try:
        mongo_password = (persistent_secret(out_dir, "mongoRootPassword") if render_only
                          else resolve_mongo_password_single(fly, machines, naming, out_dir, say))
        say(f"=== Rendering config for single-machine instance '{env}' ===")
        try:
            docs = render("fly", Path(chart_dir) if chart_dir else resources.chart_dir, env=env,
                          android_apk_key_hashes=[i["apk_key_hash"] for i in identities],
                          out_dir=rendered_root, mongo_password=mongo_password, conformance=False,
                          extra_trusted_issuers=spec.trusted_issuers, wallet_attestation=spec.wallet_attestation,
                          extra_trusted_verifiers=spec.trusted_verifiers,
                          extra_trusted_verifier_roots=spec.trusted_verifier_roots,
                          rical_provider_url=spec.rical_provider_url or None,
                          rical_root_certificate_pem=spec.rical_root_pem or None,
                          zk_circuits_sources=spec.zk_circuits_sources, dc_api_enable=spec.dc_api_enable,
                          credential_registries=spec.credential_registries, env_values=spec.values,
                          bbs_secret_key=spec.bbs_secret_key or None, naming=naming, resources=resources,
                          say=say, warn=say)
        except HelmError as e:
            raise DeployError(str(e), component="render") from e
        step("render")
        mongo_version = extract_image(docs, "mongoCommunityVersion")
        by_name = {c["name"]: c for c in components}
        images = {n: resolve_image(by_name[n], spec, docs, mongo_version)
                  for n in CONTAINER_COMPONENTS}
        result = DeployResult(out_dir=out_dir, docs=docs, images=images, mongo_password=mongo_password)
        ctx = DeployContext(fly=fly, naming=naming, resources=resources, spec=spec, say=say, docs=docs,
                            mongo_version=mongo_version, out_dir=out_dir, mongo_password=mongo_password)

        if not render_only:
            _check_signing_key(fly, naming, out_dir / "vc-pki")
        say("=== Generating per-instance PKI ===")
        ctx.pki_dir = generate_pki(ctx)
        ctx.assetlinks_path = generate_android_assets(ctx, identities)
        android_identities = {e["target"]["package_name"]: e["target"]["sha256_cert_fingerprints"]
                              for e in json.loads(ctx.assetlinks_path.read_text())}
        step("pki")
        files = bundle_files(out_dir)

        if render_only:
            config = build_machine_config(ctx, images, "registry.fly.io/<render-only>@sha256:" + "0" * 64,
                                          "vol_render_only", android_identities, guest)
            _write_config(out_dir, config)
            check_block_devices(config)
            result.rendered_only = True
            result.machine = {"app": app, "config_bytes": request_size(config, spec.region)}
            return result

        say(f"=== Creating {app} (org: {fly.org}) ===")
        # public_ips (default): its own network segment and public IPs, so
        # <app>.fly.dev answers. Without: the org's default network and no IPs -
        # reachable only through a shared edge app that fly-replays to it.
        fly.ensure_app(app, network=naming.network() if spec.public_ips else None)
        if spec.public_ips:
            _ensure_public_ips(fly, app)
        for name in spec.images:
            if name in images and fly.is_local_docker_image(images[name]):
                say(f"{name}: {images[name]!r} is a local Docker image - pushing to registry.fly.io/{app}")
                images[name] = fly.push_local_image(app, images[name])
        step("app")
        volume = fly.ensure_volume(app, volume_name("mongodb"), spec.region)
        if not volume.get("id"):
            raise DeployError(f"{app}: could not determine the Mongo volume's id", component="mongodb")
        step("volume")

        say("=== Setting secrets ===")
        version = _set_secrets(fly, machines, app, out_dir, force_mongo=bool(volume.get("created")))
        step("secrets")

        say(f"=== Pushing the config bundle ({len(files)} files) ===")
        bundle_ref = _push_bundle(fly, machines, app, files, registry, push_credential, say)
        step("bundle")

        config = build_machine_config(ctx, images, bundle_ref, volume["id"], android_identities, guest)
        _write_config(out_dir, config)
        check_block_devices(config)
        size = request_size(config, spec.region, version)
        if size > MAX_CONFIG_BYTES:
            raise DeployError(f"machine config is {size} bytes; the Machines API refuses bodies near 1 MiB "
                              f"(limit used here: {MAX_CONFIG_BYTES}). Move large files into the bundle.",
                              component="config")

        existing = machines.list_machines(app)
        if len(existing) > 1:
            raise DeployError(f"{app} has {len(existing)} machines; the single-machine layout expects one. "
                              f"Destroy the extra ones (flyctl machine list -a {app}).", component="machine")
        t0 = time.monotonic()
        if not existing:
            say(f"=== Creating the machine ({size} bytes of config, {len(config['containers'])} containers) ===")
            m = machines.create_machine(app, config, region=spec.region, name=f"{app}-m",
                                        min_secrets_version=version)
        else:
            m = existing[0]
            current = ((m.get("config") or {}).get("metadata") or {}).get("sirosid_config_hash")
            if current == config["metadata"]["sirosid_config_hash"] and version is None:
                say("machine config unchanged - not re-applying (an update reboots every container)")
                if m.get("state") != "started":
                    machines.start_machine(app, m["id"])
            else:
                say(f"=== Updating the machine ({size} bytes; this restarts every container) ===")
                was = m.get("state")
                m = machines.update_machine(app, m["id"], config, min_secrets_version=version)
                if was != "started":
                    # Fly: "machine was in a non-started state prior to the update
                    # so leaving the new version stopped" - start it ourselves.
                    machines.wait(app, m["id"], "stopped", timeout=120, instance_id=m.get("instance_id", ""))
                    machines.start_machine(app, m["id"])
        step("machine")
        machine_id = m["id"]
        machines.wait(app, machine_id, "started", timeout=300, instance_id=m.get("instance_id", ""))
        say("=== Waiting for the containers ===")
        states = machines.wait_containers(app, machine_id, readiness(config), timeout=ready_timeout, progress=say)
        step("containers")
        result.deployed = list(done)
        result.machine = {"app": app, "id": machine_id, "containers": states, "config_bytes": size,
                          "ready_seconds": round(time.monotonic() - t0, 1)}

        result.admin_token = persistent_secret(out_dir, "adminToken")
        if register is not None:
            say("=== Registering VC services with wallet-backend's default tenant ===")
            # <app>.fly.dev answers as wallet-proxy even before any custom domain
            # is in DNS; behind an edge the wallet-proxy host is the way in.
            register_vc_services(naming, result.admin_token, register, say,
                                 admin_url=f"https://{app}.fly.dev" if spec.public_ips else "")
            step("register")
        result.deployed = list(done)
        result.urls = {c: naming.url(c) for c in PUBLIC_COMPONENTS}
        if spec.public_ips:
            result.urls["fly.dev"] = f"https://{app}.fly.dev"
        return result
    except DeployError as e:
        if not e.deployed:
            e.deployed = list(done)
        if e.component is None:
            e.component = _next_step(done)
        raise
    except (FlyError, MachinesError, RegistryError) as e:
        raise DeployError(str(e), component=_next_step(done), deployed=done) from e


_STEPS = ("render", "pki", "app", "volume", "secrets", "bundle", "machine", "containers", "register")


def _next_step(done):
    return next((s for s in _STEPS if s not in done), "register")


def _ensure_public_ips(fly: FlyClient, app: str):
    """A shared v4 and a v6, once. `ips allocate-v6` is NOT idempotent (each call
    adds another dedicated address - seen on real Fly), so look first."""
    result = fly.run("ips", "list", "-a", app, "--json", check=False, capture=True)
    try:
        existing = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except ValueError:
        existing = []
    kinds = {str(ip.get("Type") or ip.get("type") or "").lower() for ip in existing or []}
    if not any(k.startswith("v4") or k == "shared_v4" for k in kinds):
        fly.run("ips", "allocate-v4", "--shared", "-a", app)
    if not any(k.startswith("v6") for k in kinds):
        fly.run("ips", "allocate-v6", "-a", app)


def _write_config(out_dir: Path, config: dict):
    """The exact config sent (secret-free: secrets are names here, values on
    Fly), for `--render-only` diffs and post-mortems. Regenerable, not state."""
    (out_dir / "machine-config.json").write_text(json.dumps(config, indent=1, sort_keys=True))


def _check_signing_key(fly: FlyClient, naming: Naming, pki_dir: Path):
    """deploy.check_pki_consistency for the one app: a missing local PKI next to
    an already-set signing key would deploy a new chain with the old key."""
    if (pki_dir / "signing_ec_private.pem").exists():
        return
    app = naming.machine_app()
    if fly.app_exists(app) and "SIROSID_VC_SIGNING_KEY" in fly.existing_secret_names(app):
        raise DeployError(
            f"Refusing to continue: no local PKI at {pki_dir}, but {app} already has a signing key from an "
            f"earlier deploy. Restore the instance's state (fly-{naming.env}/vc-pki), or destroy it first.",
            component="pki")


def _set_secrets(fly: FlyClient, machines: MachinesClient, app: str, out_dir: Path, force_mongo: bool):
    """Set each secret that is not set yet (never rotate one - Fly cannot give it
    back, and the state files hold the values). Returns the highest secrets
    version written, or None if nothing changed."""
    existing = fly.existing_secret_names(app)
    version = None
    for name, (source, _) in SECRETS.items():
        if name in existing and not (force_mongo and name == "SIROSID_MONGO_ROOT_PASSWORD"):
            continue
        if "/" in source:                       # a PKI file: exact bytes
            content = (out_dir / source).read_bytes()
        else:                                   # a generated-once value (state.STATE_FILES)
            content = persistent_secret(out_dir, source).encode()
        v = machines.set_secret(app, name, _b64(content))
        version = max(version or 0, v)
    return version


def _push_bundle(fly: FlyClient, machines: MachinesClient, app: str, files: dict, registry: Registry,
                 push_credential: str, say) -> str:
    """Push the bundle and return its digest reference. The credential is an
    app-scoped deploy token minted for this push and revoked right after - held
    in memory only, never written anywhere - unless the caller supplies one."""
    tag = "cfg-" + hashlib.sha256(json.dumps(sorted((k, hashlib.sha256(v).hexdigest()) for k, v in files.items()))
                                  .encode()).hexdigest()[:16]
    if registry is not None:
        return registry.push(app, tag, files)
    minted = False
    credential = push_credential
    if not credential:
        try:
            credential = fly.create_deploy_token(app, name="sirosid-bundle-push", expiry="1h")
            minted = True
        except FlyError as e:
            say(f"could not mint a deploy token for the bundle push ({e}); using the deploy's own credential")
            credential = machines.token
    try:
        return Registry("registry.fly.io", "x", credential).push(app, tag, files)
    finally:
        if minted:
            fly.revoke_tokens(app, name="sirosid-bundle-push")
