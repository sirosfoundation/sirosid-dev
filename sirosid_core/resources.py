"""Resources - where the repo-relative inputs of a deploy live.

Rendering reads the chart, the values layers and the fixtures; deploying runs
create-pki.sh. In the CLI those sit in the repo checkout; a hosted service ships
them inside its own image. Either way the library is told where, instead of
walking up from its own __file__ to find out - which is what would silently break
the first time it ran somewhere the checkout is not.

`root` is a directory laid out like the repository: chart/, values-base.yaml,
values-dev.yaml, values-fly.yaml, fixtures/ (and env-admin/ for a local build).
"""
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Resources:
    root: Path

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))

    @property
    def chart_dir(self) -> Path:
        return self.root / "chart"

    @property
    def values_base(self) -> Path:
        return self.root / "values-base.yaml"

    @property
    def values_dev(self) -> Path:
        return self.root / "values-dev.yaml"

    @property
    def values_fly(self) -> Path:
        return self.root / "values-fly.yaml"

    @property
    def fixtures(self) -> Path:
        return self.root / "fixtures"

    @property
    def rendered(self) -> Path:
        """Default home of rendered output and per-instance state (the CLI's)."""
        return self.fixtures / "rendered"

    @property
    def rendered_secrets(self) -> Path:
        return self.fixtures / "rendered-secrets"

    def missing(self) -> list:
        """Names of required inputs that are not there; empty means usable."""
        need = {"chart/": self.chart_dir, "values-base.yaml": self.values_base,
                "values-fly.yaml": self.values_fly, "fixtures/": self.fixtures}
        return [n for n, p in need.items() if not p.exists()]

    def image_pin(self, key: str, default: str) -> str:
        """One pin from values-fly.yaml's images: block, for the images that are not
        in the chart (mini-oidc, env-admin). `default` if the file or key is absent."""
        try:
            data = yaml.safe_load(self.values_fly.read_text()) or {}
        except OSError:
            return default
        return (data.get("images") or {}).get(key) or default
