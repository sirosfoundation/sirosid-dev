"""Shared helpers for rendering config off the siros-id-stack chart.

Used by render-helm-config.py (docker-compose / Fly wallet-backend + pdp
config) and fly-up.py (image refs + mongo version for the Fly deployment) -
factored out so both draw from one `helm template` invocation's worth of
parsing logic instead of duplicating it.
"""
import subprocess
import sys
from pathlib import Path

import yaml


def helm_template(chart_dir: Path, values_files: list, namespace: str) -> str:
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


def load_manifest_docs(manifest_yaml: str) -> list:
    return [d for d in yaml.safe_load_all(manifest_yaml) if d]


def extract_configmap_data(docs: list, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == name:
            return doc["data"]
    raise ValueError(
        f"ConfigMap {name!r} not found in rendered manifest - "
        "has the chart's template/ConfigMap naming changed upstream?"
    )


def extract_deployment_image(docs: list, name: str) -> str:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == name:
            return doc["spec"]["template"]["spec"]["containers"][0]["image"]
    raise ValueError(
        f"Deployment {name!r} not found in rendered manifest - "
        "has the chart's Deployment naming changed upstream?"
    )


def extract_init_container_image(docs: list, deployment_name: str, index: int = 0) -> str:
    """wallet-frontend's Deployment runs two different images: an initContainer
    (`config-gen`, images.walletFrontendConfig - the full app image, which also
    has its own nginx and can run standalone) and a main container
    (images.walletFrontendNginx, a stock nginx image only used to serve the
    initContainer's output in the Helm split). sirosid-dev's Fly deployment
    doesn't replicate that split (see render-helm-config.py's module
    docstring) - it needs the initContainer's image, not the main one.
    """
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == deployment_name:
            return doc["spec"]["template"]["spec"]["initContainers"][index]["image"]
    raise ValueError(f"Deployment {deployment_name!r} not found in rendered manifest")


def extract_mongo_version(docs: list) -> str:
    for doc in docs:
        if doc.get("kind") == "MongoDBCommunity":
            return doc["spec"]["version"]
    raise ValueError("MongoDBCommunity resource not found in rendered manifest")
