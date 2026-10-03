"""Shared helpers for rendering config off chart/.

Used by render-helm-config.py (docker-compose / Fly wallet-backend + pdp
config) and fly-up.py (image refs + mongo version for the Fly deployment) -
factored out so both draw from one `helm template` invocation's worth of
parsing logic instead of duplicating it.
"""
import subprocess
import sys
from pathlib import Path

import yaml


class HelmError(RuntimeError):
    """`helm template` failed; the message carries helm's own stderr."""


def helm_template(chart_dir: Path, values_files: list, namespace: str) -> str:
    cmd = [
        "helm", "template", "siros-id-stack", str(chart_dir),
        "--namespace", namespace,
    ]
    for f in values_files:
        cmd += ["-f", str(f)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HelmError(f"helm template failed (exit {result.returncode})\n{result.stderr.strip()}")
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


def extract_image(docs: list, key: str) -> str:
    """One entry of the merged `images:` values (chart/templates/06-images.yaml)."""
    images = extract_configmap_data(docs, "images")
    if key not in images:
        raise ValueError(f"images.{key} is not defined - see chart/values.yaml")
    return images[key]
