"""Shared component registry + flyctl helpers for scripts/fly-up.py and fly-down.py.

See scripts/render-helm-config.py's module docstring for the overall Fly
design (one Fly app per component, `sirosid-<env>-<component>` naming,
images pulled straight from the siros-id-stack chart's values.yaml, no local
build). wallet-backend has no public Fly service - `wallet-proxy` (a small
nginx app mirroring fixtures/wallet-proxy.conf) is its public identity,
serving /.well-known/assetlinks.json for Android passkey verification and
proxying everything else through to wallet-backend, matching how local
Android/tunnel testing already works.
"""
import base64
import json
import urllib.error
import urllib.request
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core.assets import (  # noqa: E402,F401  (re-exported: the CLI scripts import these from here)
    assetlinks_json, merge_android_identities, mini_oidc_config, wallet_frontend_conf,
    wallet_frontend_dashboard_html, wallet_proxy_conf, write_fly_toml)
from sirosid_core.components import (  # noqa: E402,F401
    CONFORMANCE_COMPONENTS, FLY_REGION_FALLBACK, MINI_OIDC_APIGW_CLIENT_ID, MINI_OIDC_APIGW_CLIENT_SECRET,
    STORAGE_APPS, VOLUME_SIZE_GB, build_components, volume_name)
from sirosid_core.fly import FlyClient, FlyError, detect_region  # noqa: E402,F401
from sirosid_core.naming import Naming  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def _values_fly_image(key: str, default: str) -> str:
    """Read one pin from values-fly.yaml's images: block.

    Most components get their image through `helm template` with
    values-fly.yaml layered on top, so their pins live in that file. A few
    (mini-oidc, mongodb) aren't in the siros-id-stack chart at all and so
    can't ride that path - but their pins belong in the same file regardless,
    or they end up buried in a Python literal that nobody remembers to bump.
    Hence reading the key directly here rather than via helm.

    Falls back to `default` if the file or key is missing, so a checkout with
    a trimmed values-fly.yaml still deploys.
    """
    try:
        import yaml  # imported lazily: only fly-up needs it, not fly-down
        with open(SIROSID_DEV_ROOT / "values-fly.yaml") as fh:
            data = yaml.safe_load(fh) or {}
        return (data.get("images") or {}).get(key) or default
    except (OSError, ImportError, AttributeError):
        return default


MINI_OIDC_IMAGE = _values_fly_image(
    "miniOidc", "ghcr.io/sirosfoundation/mini-oidc:0.0.4"
)
# env-admin is sirosid-dev's own code (env-admin/), published to GHCR by
# .github/workflows/env-admin-image.yml. Not in the chart, so pinned here
# like mini-oidc. fly-up falls back to building it locally when the pin is
# not pullable yet (see fly-up.py's env-admin branch).
ENV_ADMIN_IMAGE = _values_fly_image(
    "envAdmin", "ghcr.io/sirosfoundation/sirosid-env-admin:0.1.0"
)
FLY_ORG = "sirosfoundation"

# The deployment order; the two pins above are the only repo-file input.
COMPONENTS = build_components(MINI_OIDC_IMAGE, ENV_ADMIN_IMAGE)








# --- flyctl operations -------------------------------------------------------
# The implementations live in sirosid_core.fly.FlyClient. These names are the
# CLI scripts' view of one default client for the default org; FlyError (which a
# library raises) becomes SystemExit here, which is what a script wants.
def _cli(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FlyError as e:
            raise SystemExit(str(e)) from None
    return wrapper


_client = FlyClient(FLY_ORG)
run_fly = _cli(_client.run)
for _name in ("app_exists", "ensure_app", "is_local_docker_image", "push_local_image", "ensure_running",
              "machine_private_ip", "wait_for_checks", "destroy_app", "list_machines", "stop_machines",
              "list_volumes", "ensure_volume", "assert_volume_mounted", "destroy_machines_without_mount",
              "create_deploy_token", "revoke_tokens", "read_machine_file", "existing_secret_names",
              "ensure_secret"):
    globals()[_name] = _cli(getattr(_client, _name))
machine_has_mount = FlyClient.machine_has_mount


def app_name(env: str, component: str) -> str:
    return Naming(env).app(component)


def app_url(env: str, component: str) -> str:
    return Naming(env).url(component)






def network_name(env: str) -> str:
    """A dedicated 6PN network per environment - apps in one org otherwise
    share ONE flat private network by default (any app can resolve/reach any
    other app's `.internal` address), which would mean any other developer's
    environment - or any other app in `sirosfoundation` - could reach this
    one's mongodb/pdp/wallet-backend directly. `--network` on `apps create`
    puts every component for this env in its own segment instead, so naming
    (`sirosid-<env>-*`) isn't the only thing preventing cross-environment
    reachability."""
    return Naming(env).network()
























































