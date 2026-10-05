"""python -m sirosid_service serve | admin-invite

serve         run the HTTP service (and the periodic reaper/sweeper)
admin-invite  print an invite for the FIRST admin, who enrols a passkey in the browser
"""
import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from sirosid_core.fly import FlyClient
from sirosid_core.policy import PlatformPolicy
from sirosid_core.resources import Resources

from .api import ApiConfig, create_app
from .auth import AuthConfig, AuthService
from .config import Settings
from .db import Database
from .service import ControlPlane, Limits, ThreadRunner

log = logging.getLogger("sirosid")


def build(settings: Settings, runner=None, register=None):
    db = Database(settings.db_path)
    fly = FlyClient(org=settings.fly_org, token=settings.fly_token)
    platform = PlatformPolicy(region=settings.region, app_prefix=settings.app_prefix, host_pattern=settings.host_pattern,
                              scale_to_zero=True, env_admin=False, layout=settings.layout,
                              public_ips=settings.public_ips)
    cp = ControlPlane(db, fly, Resources(settings.resources_root), platform=platform,
                      limits=Limits(global_max_instances=settings.max_instances,
                                    sweep_grace_seconds=settings.sweep_grace_seconds),
                      runner=runner or ThreadRunner(),
                      register=register)
    auth = AuthService(cp, AuthConfig(rp_id=settings.rp_id, rp_name=settings.rp_name, origins=settings.origins))
    app = create_app(cp, auth, ApiConfig(origins=settings.origins, client_ip_header=settings.client_ip_header,
                                         console_dir=settings.console_dir))
    return cp, auth, app


def _register_callable(settings: Settings):
    """Registration with a new instance's wallet-backend: scripts/bootstrap.py is the one
    implementation (the env-admin image copies it standalone), so import it from there."""
    sys.path.insert(0, str(settings.resources_root / "scripts"))
    import bootstrap
    from sirosid_core.deploy import RegistrationError

    def register(admin_url, token, issuer_url, verifier_url):
        try:
            return bootstrap.register(admin_url, token, issuer_url, verifier_url)
        except bootstrap.BootstrapError as e:
            raise RegistrationError(str(e)) from e
    return register


def _ticker(cp: ControlPlane, every: float):
    while True:
        try:
            out = cp.tick()
            if out["reaped"] or out["lapsed"] or out["sweep"]["destroyed"]:
                log.info("tick: %s", out)
        except Exception:                                   # noqa: BLE001 - the loop must survive anything
            log.exception("periodic work failed")
        time.sleep(every)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sirosid_service")
    p.add_argument("command", choices=["serve", "admin-invite"])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if args.command == "admin-invite":
        cp, _, _ = build(settings)
        print(cp.bootstrap_invite())
        return
    import uvicorn
    cp, _, app = build(settings, register=_register_callable(settings))
    threading.Thread(target=_ticker, args=(cp, settings.tick_seconds), daemon=True, name="ticker").start()
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
