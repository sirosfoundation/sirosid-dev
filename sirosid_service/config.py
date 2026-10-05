"""Service configuration from the environment (12-factor: Fly sets these as env and secrets).

Secrets (FLY_API_TOKEN) are only ever read from the environment, never from a file in the
image or the repo.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


def _env(name, default=""):
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: str
    resources_root: Path
    fly_org: str
    fly_token: str
    rp_id: str
    rp_name: str
    origins: Tuple[str, ...]
    app_prefix: str
    host_pattern: str
    region: str
    tick_seconds: float
    client_ip_header: str
    max_instances: int
    host: str
    port: int
    console_dir: str
    sweep_grace_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(_env("SIROSID_RESOURCES", str(Path(__file__).resolve().parent.parent)))
        origins = tuple(o.strip() for o in _env("SIROSID_ORIGINS", "https://sirosid.dev").split(",") if o.strip())
        s = cls(
            db_path=_env("SIROSID_DB", "/data/sirosid.db"), resources_root=root, fly_org=_env("SIROSID_FLY_ORG", "sirosdev"),
            fly_token=_env("FLY_API_TOKEN"), rp_id=_env("SIROSID_RP_ID", "sirosid.dev"), rp_name=_env("SIROSID_RP_NAME", "SIROS ID Dev"),
            origins=origins, app_prefix=_env("SIROSID_APP_PREFIX", "sid"), host_pattern=_env("SIROSID_HOST_PATTERN", "{app}.fly.dev"),
            region=_env("SIROSID_REGION", "arn"), tick_seconds=float(_env("SIROSID_TICK_SECONDS", "300")),
            client_ip_header=_env("SIROSID_CLIENT_IP_HEADER", "Fly-Client-IP"),
            max_instances=int(_env("SIROSID_MAX_INSTANCES", "10")), host=_env("SIROSID_HOST", "0.0.0.0"), port=int(_env("PORT", "8080")),
            console_dir=_env("SIROSID_CONSOLE_DIR", str(root / "console")),
            sweep_grace_seconds=float(_env("SIROSID_SWEEP_GRACE_SECONDS", "3600")))
        s.validate()
        return s

    def validate(self):
        problems = []
        if not self.fly_token:
            problems.append("FLY_API_TOKEN is required (the service's own org-scoped token)")
        if not self.origins or any(not o.startswith("https://") for o in self.origins):
            problems.append("SIROSID_ORIGINS must be https origins")
        for o in self.origins:
            host = o.split("://", 1)[1].split("/")[0].split(":")[0]
            if not (host == self.rp_id or host.endswith("." + self.rp_id)):
                problems.append(f"origin {o} is not under the RP ID {self.rp_id}")
        if "{app}" not in self.host_pattern and "{env}" not in self.host_pattern:
            problems.append("SIROSID_HOST_PATTERN must contain {app} or {env}, or every instance would share a hostname")
        if self.tick_seconds <= 0:
            problems.append("SIROSID_TICK_SECONDS must be positive")
        if self.sweep_grace_seconds < 60:
            # The sweeper destroys apps that look like ours but have no live row; the
            # grace is what lets a human notice (and a restored database catch up)
            # before anything is deleted. Shorten it for a test, never to nothing.
            problems.append("SIROSID_SWEEP_GRACE_SECONDS must be at least 60")
        if problems:
            raise SystemExit("configuration problems:\n  " + "\n  ".join(problems))
