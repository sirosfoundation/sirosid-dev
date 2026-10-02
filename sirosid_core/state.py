"""Instance state: the few files that must survive between runs, and how to carry them.

A deploy renders dozens of files into a working directory, because flyctl's
--file-local and create-pki.sh both need real files. Almost all of them are
regenerable output. A handful are STATE: values that were generated once, were
handed to Fly (which will not give them back), and would silently desynchronise
the instance if regenerated - the mongo root password the volume was initialised
with, the wallet-backend session and admin secrets, the API-auth key whose public
half is baked into running configs, and the instance's PKI.

STATE_FILES / STATE_DIRS name exactly those. Everything here works on a directory
plus that declaration, so the same code serves two callers:

  * the CLI, whose working directory is persistent (fixtures/rendered/fly-<env>);
  * a hosted service, which keeps state in a database: `workdir()` seeds a scratch
    directory from a StateStore, hands it to the deploy, and writes the state
    files back when the deploy succeeds.

If a new generated-once value is ever added to the deploy, it MUST be added here;
tests/test_state.py redeploys from exported state alone and fails if a run's
secrets differ, which is how a missed file shows up.
"""
import contextlib
import secrets
import shutil
import string
import tempfile
from pathlib import Path
from typing import Dict, Protocol

STATE_FILES = ("mongoRootPassword", "jwtSecret", "adminToken", "apiAuthKey.pem")
STATE_DIRS = ("vc-pki",)

Blobs = Dict[str, bytes]


def is_state(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    return rel in STATE_FILES or any(rel == d or rel.startswith(d + "/") for d in STATE_DIRS)


def export_state(workdir: Path) -> Blobs:
    """The state files under workdir, as {relative posix path: bytes}."""
    workdir = Path(workdir)
    out = {}
    for p in sorted(workdir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(workdir).as_posix()
            if is_state(rel):
                out[rel] = p.read_bytes()
    return out


def import_state(workdir: Path, blobs: Blobs) -> None:
    """Restore state files into workdir. Refuses anything that is not state or
    that would escape the directory - a store must not be able to plant files."""
    workdir = Path(workdir).resolve()
    for rel, data in blobs.items():
        if not is_state(rel):
            raise ValueError(f"{rel!r} is not an instance state file")
        dest = (workdir / rel).resolve()
        if workdir not in dest.parents:
            raise ValueError(f"{rel!r} escapes the working directory")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(0o600)


class StateStore(Protocol):
    """Where a service keeps instance state between runs."""

    def load(self, instance_id: str) -> Blobs: ...

    def save(self, instance_id: str, blobs: Blobs) -> None: ...


class MemoryStateStore:
    """For tests, and as the reference for what a real store must do."""

    def __init__(self):
        self._data: Dict[str, Blobs] = {}

    def load(self, instance_id: str) -> Blobs:
        return dict(self._data.get(instance_id, {}))

    def save(self, instance_id: str, blobs: Blobs) -> None:
        self._data[instance_id] = dict(blobs)

    def delete(self, instance_id: str) -> None:
        self._data.pop(instance_id, None)


@contextlib.contextmanager
def workdir(store: StateStore, instance_id: str, root: Path = None, subdir: str = ""):
    """A scratch working directory seeded with the instance's saved state.

    Yields the directory. On a clean exit the state files are saved back; if the
    body raises, nothing is saved - a half-finished deploy must not overwrite
    known-good state with values Fly may never have received. The scratch
    directory is always removed.

    subdir: where, inside the scratch directory, the instance's own files live.
    deploy_instance() keeps them in `<rendered_root>/fly-<env>/`, so pass
    subdir=f"fly-{env}" and hand the YIELDED directory to it as rendered_root;
    state is then seeded into and saved from that subdirectory.
    """
    scratch = Path(tempfile.mkdtemp(prefix=f"sirosid-{instance_id}-", dir=str(root) if root else None))
    instance_dir = scratch / subdir if subdir else scratch
    try:
        instance_dir.mkdir(parents=True, exist_ok=True)
        import_state(instance_dir, store.load(instance_id))
        yield scratch
        store.save(instance_id, export_state(instance_dir))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def random_secret(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def persistent_secret(workdir_: Path, name: str) -> str:
    """A secret generated once and cached in the working directory.

    Fly secrets cannot be read back, so without a cached copy a rerun would
    generate a brand-new value that `ensure_secret` then discards (it sees the
    OLD one still set) while nothing else knows what the old one was. `name` must
    be a declared state file.
    """
    if name not in STATE_FILES:
        raise ValueError(f"{name!r} is not a declared state file (see STATE_FILES)")
    path = Path(workdir_) / name
    if path.exists():
        return path.read_text().strip()
    value = random_secret()
    path.write_text(value)
    return value
