"""Naming - every name an instance has, derived in one place.

An instance's components get names in four different namespaces, and they must
be able to vary independently:

  app       the Fly app name ("sirosid-<env>-<component>"). Global across ALL Fly
            orgs, so a service must generate ids that cannot collide with, or
            squat on, anyone else's.
  internal  the private-network address, always "<app>.internal" - derived, never
            chosen.
  host      the PUBLIC hostname users and wallets see. Today "<app>.fly.dev";
            for a hosted service it is a generated name on a domain we own. It is
            the one that appears in passkey rp_ids, OAuth redirect URIs and
            issuer identifiers, so it must come from here and nowhere else.
  network   the per-instance private network segment.

`Naming(env)` reproduces the scheme sirosid-dev has always used, byte for byte;
tests/test_fly_up_characterization.py holds that. Change a field to change the
scheme: a different `host_pattern` moves every public URL at once.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Naming:
    # The instance's identity: the CLI's environment name, or a generated id.
    env: str
    app_prefix: str = "sirosid"
    # Format string for the public hostname. Fields: {app}, {env}, {component}.
    host_pattern: str = "{app}.fly.dev"

    def app(self, component: str) -> str:
        return f"{self.app_prefix}-{self.env}-{component}"

    def internal(self, component: str) -> str:
        return f"{self.app(component)}.internal"

    def host(self, component: str) -> str:
        return self.host_pattern.format(app=self.app(component), env=self.env, component=component)

    def url(self, component: str) -> str:
        return f"https://{self.host(component)}"

    def network(self) -> str:
        return f"{self.app_prefix}-{self.env}"

    def label(self) -> str:
        """Human-readable instance label used in generated config names."""
        return f"{self.app_prefix}-{self.env}"
