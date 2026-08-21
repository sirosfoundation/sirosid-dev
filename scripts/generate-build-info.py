#!/usr/bin/env python3
"""Generate build-info.json for the startup dashboard.

Two sections, answering two different questions:

  "components" - for each sibling repo the stack is BUILT FROM, which
      branch/commit is checked out right now, and whether the working tree is
      dirty. This is git state on disk, independent of whether anything has
      been built from it yet.

  "services"   - for each RUNNING container, which image it is actually
      running and where that image came from. This is the question the git
      section cannot answer: a locally-built image can lag behind its repo
      (someone edited the source but didn't rebuild), and several components
      aren't built from source at all - GOLDEN=yes swaps in pre-built
      ghcr.io images, and mini-oidc/mongodb/nginx are always upstream ones.

Deliberately reports only running containers rather than the full compose
matrix: the dashboard's job is to describe the environment as it IS, and
which services exist depends on flags (VC=, PDP=, CONFORMANCE=, ...) that
this script would otherwise have to re-derive.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

# name -> path, relative to this repo. Every repo `make setup` clones, plus
# this one. Missing ones are skipped rather than reported as errors: most
# are only needed for particular flags (vc for VC=yes, facetec-api for
# FACETEC=yes, siros-id-stack for PDP=helm / fly-up).
REPOS = [
    ("wallet-frontend", "../wallet-frontend"),
    ("wallet-common", "../wallet-common"),
    ("go-wallet-backend", "../go-wallet-backend"),
    ("go-trust", "../go-trust"),
    ("vc", "../vc"),
    ("facetec-api", "../facetec-api"),
    ("siros-id-stack", "../siros-id-stack"),
    ("sirosid-dev", "."),
]


def _git(path: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def collect_components():
    components = []
    for name, rel in REPOS:
        path = (SIROSID_DEV_ROOT / rel).resolve()
        if not (path / ".git").exists():
            continue
        # --quiet exits non-zero when there ARE changes, so a plain returncode
        # check is inverted here on purpose.
        dirty = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", "HEAD"],
            capture_output=True, timeout=10,
        ).returncode != 0
        components.append({
            "name": name,
            "branch": _git(path, "branch", "--show-current") or "detached",
            "commit": _git(path, "rev-parse", "HEAD") or "unknown",
            "built": _git(path, "log", "-1", "--format=%ci") or "unknown",
            "dirty": dirty,
        })
    return components


def _image_origins(images):
    """Classify each image by where it came from: built here, or pulled.

    Keyed on RepoDigests rather than the image NAME. A name-shaped heuristic
    ("no registry host means local") gets official Docker Hub images wrong -
    mongo:7.0.24 and nginx:alpine have no slash at all yet are obviously
    pulled. An image acquired from a registry carries a repo digest; one
    produced by `docker build` on this machine does not.

    Worth surfacing because the two have completely different update
    semantics: a local build tracks whatever the working tree looked like at
    build time (and can silently lag it), while a pulled image tracks
    whatever the tag pointed at when it was fetched.
    """
    origins = {}
    for image in set(images):
        try:
            out = subprocess.run(
                ["docker", "image", "inspect", image,
                 "--format", "{{len .RepoDigests}}"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode != 0:
                origins[image] = "unknown"
                continue
            origins[image] = "registry" if out.stdout.strip() not in ("0", "") else "local build"
        except (OSError, subprocess.SubprocessError):
            origins[image] = "unknown"
    return origins


def collect_services():
    """Image provenance for every running compose container in this stack."""
    fmt = "{{.Name}}\t{{.Config.Image}}\t{{.Image}}\t{{.Created}}\t{{index .Config.Labels \"com.docker.compose.service\"}}"
    try:
        names = subprocess.run(
            ["docker", "ps", "--filter", "label=com.docker.compose.project",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=20,
        )
        if names.returncode != 0:
            return []
        containers = [n for n in names.stdout.split() if n]
        if not containers:
            return []
        out = subprocess.run(
            ["docker", "inspect", "--format", fmt, *containers],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []

    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        rows.append(parts[:5])

    origins = _image_origins([r[1] for r in rows])

    services = []
    for container, image, image_id, created, service in rows:
        services.append({
            "service": service or container.lstrip("/"),
            "container": container.lstrip("/"),
            "image": image,
            "origin": origins.get(image, "unknown"),
            # Short digest is enough to tell two builds of the same tag apart,
            # which is the whole point when every local image is ":local".
            "image_id": image_id.replace("sha256:", "")[:12],
            "created": created[:19].replace("T", " "),
        })
    services.sort(key=lambda s: s["service"])
    return services


def main():
    info = {
        "components": collect_components(),
        "services": collect_services(),
        "generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    out_path = SIROSID_DEV_ROOT / "build-info.json"
    # Truncate in place rather than replacing the file: build-info.json is
    # bind-mounted into wallet-frontend, and swapping the inode would leave
    # the container serving the old content.
    with open(out_path, "w") as fh:
        json.dump(info, fh, indent=2)
        fh.write("\n")
    print(f"Generated {out_path.name}: "
          f"{len(info['components'])} components, {len(info['services'])} running services")


if __name__ == "__main__":
    sys.exit(main())
