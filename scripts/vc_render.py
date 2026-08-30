"""Render the four vc services' config from the siros-id-stack chart.

The chart has always emitted these - templates/04-issuer.yaml produces the
ConfigMaps issuer-registry-main, issuer-apigw-main and issuer-core-main, and
04-verifier.yaml produces verifier-main, each with a `config.yaml` in vc's
model.Cfg schema. render-helm-config.py simply never extracted them, so
sirosid-dev hand-maintained fixtures/vc-config.yaml instead and patched it
into five variants with three different scripts. This module extracts them
the same way wallet-backend's and the PDP's config is already extracted.

Three things the chart assumes that neither docker-compose nor Fly provides,
handled here:

  - `helm template`'s Files.Get cannot read outside the chart directory, so a
    VCTM/MDDL/bootstrapping document living in this repo's fixtures/ can't be
    referenced by path from a values file. inline_file_refs() reads them and
    rewrites `{file: ...}` to the `{data: ...}` form the chart also accepts,
    before helm ever sees the values.

  - The chart pairs every config with a `secrets.yaml.template` consumed by a
    `secrets-renderer` initContainer running envsubst. There are no init
    containers here, so render_secrets() does the substitution itself. vc's
    LoadSecrets hard-fails if the file has any group/world permission bit set
    (pkg/configuration/config.go), hence the explicit 0600.

  - Hostnames are built by the chart as https://<tenant>.<svc>.<domain>.
    Pointing `hostnames.*` at a host:port pair gets the right authority for
    docker-compose, but the scheme is hardcoded in the templates; the same
    https->http rewrite patch_wallet_backend_compose() already does is
    applied here for the compose target only.
"""
import re
import shutil
import sys
from pathlib import Path

import yaml

from helm_render_lib import extract_configmap_data

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

# ConfigMap name in the rendered manifest -> the file this writes. The four
# services each get their own config, where sirosid-dev historically mounted
# one shared file into all of them. That split is the chart's model and the
# better one: an IMAGES= override on a single service can no longer crash-loop
# a sibling left on an older binary because the shared config's shape moved
# underneath it (the hazard environments/gdc.yaml documents at length).
VC_CONFIGMAPS = {
    "issuer-apigw-main": "vc-apigw.yaml",
    "issuer-core-main": "vc-issuer.yaml",
    "issuer-registry-main": "vc-registry.yaml",
    "verifier-main": "vc-verifier.yaml",
}

# Every host this repo's docker-compose stack serves over plain http, and
# whose https:// the chart therefore hardcodes wrongly. Host-scoped so a real
# external https:// URL in the same config is never downgraded. The host:port
# pairs must match values-dev.yaml's `hostnames:` and the published ports in
# docker-compose.vc-services.yml - for apigw and the verifier the host and
# container port have to be equal, since one advertised public URL has to work
# both from the browser and from inside the compose network.
COMPOSE_PLAIN_HTTP_HOSTS = {
    "vc-apigw.localhost:9003",
    "vc-verifier.localhost:9001",
    "vc-registry.localhost:9004",
    "localhost:3000",
    "localhost:8080",
}

# Secrets referenced by the chart's secrets.yaml.template blocks, as ${NAME}
# placeholders. Generated once into fixtures/rendered-secrets/ and reused,
# exactly like WALLET_BACKEND_SECRETS - a rotated subject_salt would
# invalidate every previously issued pairwise pseudonym.
VC_SECRETS = {
    "OIDC_PROVIDER_SUBJECT_SALT": "vcOidcProviderSubjectSalt",
    "ADMIN_GUI_PASSWORD": "vcAdminGuiPassword",
    "OIDC_PROVIDER_CLIENT_SECRET": "vcOidcProviderClientSecret",
    "API_AUTH_OIDC_CLIENT_SECRET": "vcApiAuthOidcClientSecret",
}


def _read_ref(ref, root: Path):
    """Resolve one {file: <repo-relative path>} / {data: <literal>} reference."""
    if not isinstance(ref, dict):
        return ref
    if "file" in ref:
        path = root / ref["file"]
        if not path.is_file():
            raise SystemExit(f"values reference a missing file: {ref['file']}")
        return {"data": path.read_text()}
    return ref


def inline_file_refs(values: dict, root: Path = None) -> dict:
    """Rewrite every {file: ...} document reference to {data: ...} in place.

    Covers the three places the chart takes a document: a credential type's
    vctm/mdocSchema, the identity-mapping import and the datastore import.
    Paths are relative to this repo, not the chart.
    """
    root = root or SIROSID_DEV_ROOT
    for _id, ctype in (values.get("features", {}).get("credentialTypes") or {}).items():
        for key in ("vctm", "mdocSchema"):
            if key in ctype:
                ctype[key] = _read_ref(ctype[key], root)
    issuer = values.get("issuer") or {}
    for section, key in (("identitymappingImport", "identities"), ("datastoreImport", "documents")):
        docs = (issuer.get(section) or {}).get(key) or {}
        for name, ref in docs.items():
            docs[name] = _read_ref(ref, root)
    return values


def expand_presentation_request_templates(values: dict, root: Path = None) -> dict:
    """Turn `verifier.presentationRequestTemplatesFrom: <dir>` into the chart's
    own `verifier.presentationRequestTemplates` map.

    The verifier loads a directory of template files; the chart renders one
    combined pres-reqs.yaml from a map keyed by template id. Each source file
    here is in the verifier's own on-disk shape (a top-level `templates:`
    list), so this unwraps that list and re-keys it by id.
    """
    root = root or SIROSID_DEV_ROOT
    verifier = values.get("verifier") or {}
    src = verifier.pop("presentationRequestTemplatesFrom", None)
    if not src:
        return values
    templates = verifier.setdefault("presentationRequestTemplates", {})
    for path in sorted((root / src).glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for entry in doc.get("templates", []):
            entry = dict(entry)
            tid = entry.pop("id", None) or path.stem
            templates[tid] = entry
    return values


def _rewrite_https(value, hosts: set):
    """Rewrite https:// -> http:// for the hosts this target serves plainly.

    Deliberately host-scoped rather than global: a config can legitimately
    reference a real external https:// URL (registry.siros.org, an external
    trusted issuer), and those must not be downgraded.
    """
    if isinstance(value, str):
        m = re.match(r"^https://([^/]+)(.*)$", value)
        if m and m.group(1) in hosts:
            return f"http://{m.group(1)}{m.group(2)}"
        return value
    if isinstance(value, list):
        return [_rewrite_https(v, hosts) for v in value]
    if isinstance(value, dict):
        # Keys too, not just values: an OAuth client is keyed by its client_id,
        # which for the web wallet IS its own origin. Leaving the key on
        # https:// while the wallet presents http:// means every
        # authorization_code flow is rejected with invalid_client.
        return {_rewrite_https(k, hosts): _rewrite_https(v, hosts) for k, v in value.items()}
    return value


def patch_vc_compose(config: dict, plain_http_hosts: set) -> dict:
    """docker-compose serves every one of these over plain http; the chart's
    hostname helpers hardcode an https:// scheme (siros-id.origins.*, and the
    `https://{{ $hostname... }}` literals in each service template). Same
    rewrite patch_wallet_backend_compose() applies to wallet-backend."""
    config = _rewrite_https(config, plain_http_hosts)
    # The chart already uniq's the CORS origin list, but it does so before this
    # rewrite - the frontend origin it derives and an extraAllowedOrigins entry
    # naming the same host differ only by scheme until now.
    cors = ((config.get("apigw") or {}).get("api_server") or {}).get("cors") or {}
    if cors.get("allowed_origins"):
        cors["allowed_origins"] = list(dict.fromkeys(cors["allowed_origins"]))
    return config


def patch_vc_mongo(config: dict, target: str, env: str = None, mongo_password: str = None) -> dict:
    """Replace the chart's MongoDB Community Operator connection with this
    target's real one.

    The chart renders x509 mTLS against an operator-managed replica set (an
    SRV record plus three cert paths). docker-compose runs a single plain
    `mongo` container with nothing to authenticate against; Fly runs one
    mongod on the private 6PN network with a root password rotated per
    fly-up.py invocation. Exactly the swap patch_wallet_backend_compose() /
    _fly() already do for wallet-backend's own storage block.
    """
    if target == "compose":
        uri = "mongodb://mongodb:27017"
    else:
        if not mongo_password:
            print(f"WARNING: no --mongo-password - rendering an UNAUTHENTICATED mongodb URI "
                  f"for env '{env}'. Only consistent within the fly-up.py run that set the "
                  f"matching Fly secret; for a one-off, re-run 'make fly-up ENV={env}'.",
                  file=sys.stderr)
        auth = f"root:{mongo_password}@" if mongo_password else ""
        uri = f"mongodb://{auth}sirosid-{env}-mongodb.internal:27017/?authSource=admin"
    config.setdefault("common", {})["mongo"] = {"uri": uri}
    return config


def render_secrets(docs: list, out_dir: Path, secrets_dir: Path, gen_secret,
                   overrides: dict = None) -> None:
    """Do the secrets-renderer initContainer's job: envsubst each service's
    secrets.yaml.template into one secrets file.

    One file per service rather than one shared, matching the per-service
    configs - and each service only ever reads its own section anyway.
    """
    values = {var: gen_secret(secrets_dir / name) for var, name in VC_SECRETS.items()}
    # A few of these aren't ours to generate: the apigw's OIDC client secret
    # has to be the one mini-oidc was actually configured with, on both
    # targets (fly_common sets the same constants as mini-oidc's own
    # APIGW_CLIENT_ID/_SECRET env vars).
    values.update(overrides or {})
    for cm_name, filename in VC_CONFIGMAPS.items():
        data = extract_configmap_data(docs, cm_name)
        template = data.get("secrets.yaml.template")
        path = out_dir / filename.replace(".yaml", "-secrets.yaml")
        if not template:
            # issuer-core has no secrets of its own, but its config still
            # names a secret_file_path - vc's LoadSecrets errors on a missing
            # file, so write a valid empty one rather than skipping it.
            path.write_text("{}\n")
        else:
            path.write_text(re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), ""), template))
        # vc refuses to read a secrets file with any group/world bit set.
        path.chmod(0o600)


def _write_documents(docs: list, out_dir: Path, cm_name: str, subdir: str,
                     rename=None) -> Path:
    """Materialize a document ConfigMap as a directory to bind-mount/upload."""
    target = out_dir / subdir
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for key, value in extract_configmap_data(docs, cm_name).items():
        if key == ".empty":
            continue
        (target / (rename(key) if rename else key)).write_text(value)
    return target


def render_vc(docs: list, out_dir: Path, target: str, secrets_dir: Path, gen_secret,
              plain_http_hosts: set = None, secret_overrides: dict = None,
              env: str = None, mongo_password: str = None) -> None:
    """Extract every vc service's config plus the directories it mounts."""
    for cm_name, filename in VC_CONFIGMAPS.items():
        config = yaml.safe_load(extract_configmap_data(docs, cm_name)["config.yaml"])
        if target == "compose":
            config = patch_vc_compose(config, plain_http_hosts or set())
        # issuer-core is the one service with no mongo of its own.
        if (config.get("common") or {}).get("mongo"):
            config = patch_vc_mongo(config, target, env, mongo_password)
        (out_dir / filename).write_text(yaml.dump(config, sort_keys=False))
        print(f"wrote {out_dir / filename}")

    render_secrets(docs, out_dir, secrets_dir, gen_secret, secret_overrides)

    # /vctms in the chart, but vc's credential_metadata paths are written by
    # siros-id.vc.credentialMetadata as /vctms/<scope>.json too, so the mount
    # point is the same name in both worlds.
    _write_documents(docs, out_dir, "vctms", "vctms")
    _write_documents(docs, out_dir, "issuer-documents", "documents")

    # The verifier reads presentation_requests_dir as a directory of template
    # files; the chart renders them into a single pres-reqs.yaml.
    pres = _write_documents(docs, out_dir, "verifier-pres-reqs", "pres-reqs")
    print(f"wrote {pres}/ , {out_dir / 'vctms'}/ , {out_dir / 'documents'}/")
