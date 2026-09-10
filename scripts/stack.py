#!/usr/bin/env python3
"""The local stack's option matrix, in one place: options -> compose files.

The Makefile grew its `COMPOSE_FILES +=` blocks one flag at a time, each with
the rationale in a comment next to it. That is the right place for the
rationale and the wrong place for the logic: nothing but `make` can read it,
so every other consumer (the boot manager TUI, `make plan`, a parity check)
would have to re-derive the same matrix. This module is that matrix as data,
with the Makefile's comments carried over as per-option help strings.

Read-only for now: `make up` still builds COMPOSE_FILES itself, and
tests/test_stack_parity.py asserts the two agree for every combination that
matters. Once that has held for a while, the Makefile's blocks can call
`compose-files` here instead of duplicating it.

An environment's local options live under a `local:` block in
environments/<name>.yaml (see OPTIONS for the keys), the same file that
already carries its Fly-side image pins and trust config - so one file
describes an environment for both targets:

    local:
      pdp: helm
      vc: true
      transport: websocket

`make up ENV=<name>` reads that block as defaults; `make up ENV=<name>
PDP=deny` overrides one key for that run only, exactly like the Fly flags.

Usage:
    python3 scripts/stack.py plan [--env NAME] [--set KEY=VALUE ...]
    python3 scripts/stack.py compose-files [--env NAME] [--set KEY=VALUE ...]
    python3 scripts/stack.py options            # JSON schema of every option
"""
import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_config import load_environment_config  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

TRUTHY = ("1", "yes", "on", "up", "true")


def truthy(value) -> bool:
    """The Makefile's `_truthy`: 1, yes, on, up (plus true/True for YAML)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTHY


# Every stack option `make up` accepts. `make_var` is the Makefile variable
# it maps to (and the CLI spelling), `key` is the environments/<name>.yaml
# `local:` spelling. Help text is the Makefile's own rationale, condensed.
OPTIONS = [
    {
        "key": "pdp", "make_var": "PDP", "type": "enum",
        "choices": ["allow", "whitelist", "deny", "mock", "helm"], "default": "allow",
        "label": "Trust policy provider",
        "help": ("Which PDP answers wallet-backend's trust questions. allow/whitelist/deny are "
                 "go-trust with hand-maintained flags; mock is the in-repo mock; helm renders "
                 "wallet-backend + PDP config from the siros-id-stack chart (needs ../siros-id-stack "
                 "and helm). helm is the only mode whose config cannot drift from production, and "
                 "the only one that gives wallet-backend a persistent Mongo store."),
    },
    {
        "key": "as_rules", "make_var": "AS_RULES", "type": "enum",
        "choices": ["allow-all", "baseline"], "default": "allow-all",
        "label": "Authorization Server ruleset",
        "help": ("SPOCP policy for wallet-backend's built-in AS (passkey login + token endpoint). "
                 "allow-all is unconditional so every other feature gets a working AS; baseline is "
                 "go-wallet-backend's real policy, the same one Fly gets - use it only when testing "
                 "AS rule behaviour, e.g. reproducing a Fly-only 403 locally."),
    },
    {
        "key": "vc", "make_var": "VC", "type": "bool", "default": False,
        "label": "VC services",
        "help": ("Production-like issuer/verifier/apigw/registry built from ../vc, plus mongodb and "
                 "mini-oidc. Their config is rendered from the chart regardless of PDP=."),
    },
    {
        "key": "transport", "make_var": "TRANSPORT", "type": "enum",
        "choices": ["websocket", "wmp", "http"], "default": "websocket",
        "label": "Wallet transport",
        "help": "websocket is the default; wmp is JSON-RPC over SSE; http is deprecated.",
    },
    {
        "key": "conformance", "make_var": "CONFORMANCE", "type": "bool", "default": False,
        "label": "OpenID conformance suite",
        "help": ("Adds the conformance suite, its Mongo, vc-proxy and conformance-runner, and the "
                 "VC<->go-trust wiring. Implies VC services. Needs 127.0.0.1 localhost.emobix.co.uk "
                 "in /etc/hosts (make up adds it, with sudo)."),
    },
    {
        "key": "r2ps", "make_var": "R2PS", "type": "bool", "default": False,
        "label": "R2PS remote signing",
        "help": "go-r2ps-service plus two SoftHSM2 tokens (remote WSCD/WSCA signing).",
    },
    {
        "key": "facetec", "make_var": "FACETEC", "type": "bool", "default": False,
        "label": "FaceTec bridge",
        "help": ("facetec-api between the FaceTec SDK and vc-issuer. Implies VC services and needs "
                 "FACETEC_SERVER_URL exported in the shell - a live credential, never committed."),
    },
    {
        "key": "domain", "make_var": "DOMAIN", "type": "string", "default": "",
        "label": "Custom hostname",
        "help": ("Replace localhost with a LAN hostname so a phone on the same network can reach "
                 "the stack. Mutually exclusive with tunnels."),
    },
    {
        "key": "tunnels", "make_var": "TUNNELS", "type": "bool", "default": False,
        "label": "Cloudflare quick tunnels",
        "help": ("Real-TLS *.trycloudflare.com URLs per service (host-side cloudflared processes, "
                 "not containers - make down leaves them running, make tunnel-stop ends them). "
                 "Needed for passkeys on a real device. Mutually exclusive with domain."),
    },
    {
        "key": "golden", "make_var": "GOLDEN", "type": "string", "default": "",
        "label": "Golden release",
        "help": ("Pre-built ghcr.io images from siros-conformance's golden-releases.yaml instead of "
                 "local builds: 'yes' for its default release, or a release name. VC services still "
                 "build from source even here - their config shape moves between releases."),
    },
    {
        "key": "dc_api", "make_var": "DC_API", "type": "bool", "default": False,
        "label": "W3C Digital Credentials API",
        "help": ("Lets the verifier UI attempt navigator.credentials.get(). Only useful with a "
                 "credential provider (wallet-companion) installed in the browser; a plain browser "
                 "hits a dead end before the QR path is offered."),
    },
    {
        "key": "rebuild", "make_var": "REBUILD", "type": "bool", "default": False, "transient": True,
        "label": "Force rebuild",
        "help": "docker compose build --no-cache before starting. A one-run flag, not saved.",
    },
]

OPTION_BY_KEY = {o["key"]: o for o in OPTIONS}
OPTION_BY_MAKE_VAR = {o["make_var"]: o for o in OPTIONS}

# The Makefile's compose file variables, by name, so the plan can name files
# the same way `make up` prints them.
COMPOSE = {
    "PRIMARY": "docker-compose.test.yml",
    "MONGODB": "docker-compose.mongodb.yml",
    "GO_TRUST": "docker-compose.go-trust.yml",
    "GO_TRUST_ALLOW": "docker-compose.go-trust-allow.yml",
    "GO_TRUST_WHITELIST": "docker-compose.go-trust-whitelist.yml",
    "GO_TRUST_DENY": "docker-compose.go-trust-deny.yml",
    "HELM_CONFIG": "docker-compose.helm-config.yml",
    "AS_RULES_BASELINE": "docker-compose.as-rules-baseline.yml",
    "VC_SERVICES": "docker-compose.vc-services.yml",
    "VC_GO_TRUST": "docker-compose.vc-go-trust.yml",
    "CONFORMANCE": "docker-compose.conformance.yml",
    "HTTP_TRANSPORT": "docker-compose.http-transport.yml",
    "WMP_TRANSPORT": "docker-compose.wmp-transport.yml",
    "R2PS": "docker-compose.r2ps.yml",
    "DOMAIN": "docker-compose.domain.yml",
    "TUNNEL": "docker-compose.tunnel.yml",
    "FACETEC": "docker-compose.facetec.yml",
    "GOLDEN": "docker-compose.golden.yml",
    "GOLDEN_GO_TRUST": "docker-compose.golden-go-trust.yml",
}


@dataclass
class Plan:
    options: dict
    compose_files: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checks: list = field(default_factory=list)   # pre-flight: {name, ok, detail}
    # Which stores this plan gives the stack, and whether they persist. The
    # storage panel (TUI + dashboard) and `make storage-status` read this
    # rather than re-deriving it from the compose file list.
    stores: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "options": self.options, "compose_files": self.compose_files, "labels": self.labels,
            "errors": self.errors, "warnings": self.warnings, "checks": self.checks, "stores": self.stores,
        }


def default_options() -> dict:
    return {o["key"]: o["default"] for o in OPTIONS}


def coerce(option: dict, value):
    if option["type"] == "bool":
        return truthy(value)
    if option["type"] == "enum":
        v = str(value).strip() or option["default"]
        if v not in option["choices"]:
            raise ValueError(f"{option['make_var']}={v!r}: must be one of {', '.join(option['choices'])}")
        return v
    return str(value).strip()


def options_from_env_file(env_name: str) -> dict:
    """The `local:` block of environments/<env_name>.yaml, validated."""
    cfg = load_environment_config(env_name)
    raw = cfg.get("local") or {}
    out = {}
    for key, value in raw.items():
        if key not in OPTION_BY_KEY:
            raise SystemExit(f"environments/{env_name}.yaml: local.{key} is not a stack option "
                             f"(known: {', '.join(OPTION_BY_KEY)})")
        out[key] = coerce(OPTION_BY_KEY[key], value)
    return out


def options_from_make_vars(pairs: dict) -> dict:
    """KEY=VALUE overrides in the Makefile's spelling (PDP=helm, VC=yes)."""
    out = {}
    for var, value in pairs.items():
        option = OPTION_BY_MAKE_VAR.get(var.upper()) or OPTION_BY_KEY.get(var.lower())
        if option is None:
            raise SystemExit(f"{var}: not a stack option (known: {', '.join(OPTION_BY_MAKE_VAR)})")
        # An empty make variable means "unset", not "the empty string wins".
        if value == "" and option["type"] != "string":
            continue
        out[option["key"]] = coerce(option, value)
    return out


def resolve_options(env_name: str = "", overrides: dict = None) -> dict:
    opts = default_options()
    if env_name:
        opts.update(options_from_env_file(env_name))
    opts.update(overrides or {})
    return opts


def compose_files(opts: dict) -> list:
    """The Makefile's COMPOSE_FILES, in the Makefile's order."""
    files = [COMPOSE["PRIMARY"]]

    pdp = opts["pdp"]
    if pdp == "allow":
        files += [COMPOSE["GO_TRUST"], COMPOSE["GO_TRUST_ALLOW"]]
    elif pdp == "whitelist":
        files += [COMPOSE["GO_TRUST"], COMPOSE["GO_TRUST_WHITELIST"]]
    elif pdp == "deny":
        files += [COMPOSE["GO_TRUST"], COMPOSE["GO_TRUST_DENY"]]
    elif pdp == "helm":
        files += [COMPOSE["HELM_CONFIG"]]

    if opts["as_rules"] == "baseline":
        files.append(COMPOSE["AS_RULES_BASELINE"])

    if opts["vc"]:
        files.append(COMPOSE["VC_SERVICES"])
    if opts["facetec"]:
        if COMPOSE["VC_SERVICES"] not in files:
            files.append(COMPOSE["VC_SERVICES"])
        files.append(COMPOSE["FACETEC"])

    if opts["transport"] == "wmp":
        files.append(COMPOSE["WMP_TRANSPORT"])
    elif opts["transport"] == "http":
        files.append(COMPOSE["HTTP_TRANSPORT"])

    if opts["conformance"]:
        if COMPOSE["VC_SERVICES"] not in files:
            files.append(COMPOSE["VC_SERVICES"])
        files.append(COMPOSE["VC_GO_TRUST"])
        files.append(COMPOSE["CONFORMANCE"])

    if opts["r2ps"]:
        files.append(COMPOSE["R2PS"])
    if opts["domain"]:
        files.append(COMPOSE["DOMAIN"])
    if opts["tunnels"]:
        files.append(COMPOSE["TUNNEL"])

    if opts["golden"]:
        files.append(COMPOSE["GOLDEN"])
        if pdp != "mock":
            files.append(COMPOSE["GOLDEN_GO_TRUST"])

    # mongodb rides along whenever something needs it: the vc services always,
    # and wallet-backend in helm mode (its rendered config points at
    # mongodb://mongodb). Appended last so it never reorders the blocks above,
    # which the Makefile prints in this order.
    if COMPOSE["VC_SERVICES"] in files or pdp == "helm":
        files.append(COMPOSE["MONGODB"])
    return files


def stores(opts: dict, files: list) -> list:
    """What holds state in this plan, and whether it survives `make down`."""
    out = []
    if opts["pdp"] == "helm":
        out.append({"name": "wallet-backend", "kind": "mongodb", "database": "wallet-backend",
                    "volume": "sirosid-mongodb-data", "persistent": True})
    else:
        out.append({"name": "wallet-backend", "kind": "memory", "database": None, "volume": None,
                    "persistent": False,
                    "note": "in-memory store: gone on container restart. PDP=helm gives it Mongo."})
    if COMPOSE["VC_SERVICES"] in files:
        out.append({"name": "vc services", "kind": "mongodb",
                    "database": "vc, vc_registry, verifier, *_cache",
                    "volume": "sirosid-mongodb-data", "persistent": True})
    if opts["conformance"]:
        out.append({"name": "conformance suite", "kind": "mongodb", "database": "(suite-owned)",
                    "volume": "sirosid-conformance-mongodb-data", "persistent": True})
    if opts["r2ps"]:
        out.append({"name": "SoftHSM tokens", "kind": "softhsm", "database": None,
                    "volume": "sirosid-r2ps-softhsm-tokens, sirosid-attest-softhsm-tokens",
                    "persistent": True})
    return out


def preflight(opts: dict) -> list:
    """The `make up` pre-flight checks, as data, so a UI can show them as a
    checklist before anything is started."""
    root = SIROSID_DEV_ROOT
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("docker", shutil.which("docker"), "docker CLI on PATH")
    for name, rel in (("wallet-frontend", "../wallet-frontend"), ("go-wallet-backend", "../go-wallet-backend")):
        check(f"{name} checkout", (root / rel).is_dir(), f"{rel} - run `make setup`")
    if opts["pdp"] in ("allow", "whitelist", "deny", "helm"):
        check("go-trust checkout", (root / "../go-trust").is_dir(), "../go-trust - run `make setup`")
    if opts["vc"] or opts["conformance"] or opts["facetec"]:
        check("vc checkout", (root / "../vc").is_dir(), "../vc - run `make setup`")
    if opts["pdp"] == "helm" or opts["vc"] or opts["conformance"] or opts["facetec"]:
        chart = root / "../siros-id-stack"
        check("siros-id-stack checkout", chart.is_dir(), "../siros-id-stack - run `make setup`")
        check("helm", shutil.which("helm"), "config is rendered with `helm template`")
        if chart.is_dir():
            branch = _git(chart, "branch", "--show-current")
            check("siros-id-stack on main", branch == "main",
                  f"on '{branch or 'detached'}' - a stale chart renders wrong config for everything")
    if opts["facetec"]:
        import os
        check("FACETEC_SERVER_URL", os.environ.get("FACETEC_SERVER_URL"), "export it in your shell")
    if opts["tunnels"]:
        check("cloudflared", shutil.which("cloudflared"), "needed for TUNNELS=yes")
    return checks


def _git(path: Path, *args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def build_plan(env_name: str = "", overrides: dict = None, with_checks: bool = True) -> Plan:
    opts = resolve_options(env_name, overrides)
    plan = Plan(options=opts)

    if opts["tunnels"] and opts["domain"]:
        plan.errors.append("TUNNELS=yes cannot be combined with DOMAIN=: pick quick tunnels or a "
                           "local hostname, not both.")
    if opts["golden"] and (opts["vc"] or opts["conformance"]):
        plan.warnings.append("GOLDEN= only covers wallet-frontend/backend/go-trust - the VC services "
                             "still build from ../vc (their config shape moves between releases).")
    if opts["pdp"] != "helm":
        plan.warnings.append("wallet-backend uses its in-memory store in this mode: users and "
                             "credentials vanish on restart. PDP=helm gives it the persistent Mongo volume.")

    plan.compose_files = compose_files(opts)
    plan.labels = {
        "PDP": {"allow": "go-trust allow-all", "whitelist": "go-trust whitelist", "deny": "go-trust deny-all",
                "mock": "mock-trust-pdp", "helm": "go-trust (helm-rendered config)"}[opts["pdp"]],
        "AS rules": "allow-all (fixtures/as-rules) - default" if opts["as_rules"] == "allow-all"
                    else "go-wallet-backend baseline (rules/default.rules) - AS ruleset testing",
        "VC services": "yes" if (opts["vc"] or opts["conformance"]) else ("yes (via facetec)" if opts["facetec"] else "no"),
        "Transport": {"websocket": "WebSocket (default)", "wmp": "WMP (JSON-RPC+SSE)",
                      "http": "HTTP proxy (deprecated)"}[opts["transport"]],
        "Conformance": "yes" if opts["conformance"] else "no",
        "R2PS": "yes" if opts["r2ps"] else "no",
        "Domain": opts["domain"] or "localhost (default)",
        "Tunnels": "yes" if opts["tunnels"] else "no",
        "facetec-api": "yes" if opts["facetec"] else "no",
        "DC API": "enabled" if opts["dc_api"] else "disabled",
        "Golden": opts["golden"] or "",
    }
    plan.stores = stores(opts, plan.compose_files)
    if with_checks:
        plan.checks = preflight(opts)
    return plan


def make_vars(opts: dict) -> list:
    """`make up` arguments reproducing these options - what the TUI runs."""
    args = []
    for o in OPTIONS:
        v = opts.get(o["key"], o["default"])
        if o["type"] == "bool":
            if v:
                args.append(f"{o['make_var']}=yes")
        elif v != o["default"] or o["make_var"] in ("PDP", "AS_RULES"):
            if v:
                args.append(f"{o['make_var']}={v}")
    return args


def _parse_sets(values) -> dict:
    pairs = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--set {item!r}: expected KEY=VALUE")
        k, v = item.split("=", 1)
        pairs[k.strip()] = v.strip()
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "compose-files", "make-args"):
        p = sub.add_parser(name)
        p.add_argument("--env", default="", help="environments/<name>.yaml whose local: block supplies defaults")
        p.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="override one option, Makefile spelling (PDP=helm, VC=yes)")
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-checks", action="store_true", help="skip the filesystem/PATH pre-flight checks")
        p.add_argument("--file-only", action="store_true",
                       help="make-args only: emit just the keys environments/<name>.yaml's local: block sets "
                            "(what the Makefile applies as defaults), not the full resolved option set")
    sub.add_parser("options")
    args = parser.parse_args(argv)

    if args.cmd == "options":
        print(json.dumps(OPTIONS, indent=2))
        return 0

    plan = build_plan(args.env, options_from_make_vars(_parse_sets(args.set)), with_checks=not args.no_checks)
    if args.cmd == "compose-files":
        print(" ".join(f"-f {f}" for f in plan.compose_files))
        return 1 if plan.errors else 0
    if args.cmd == "make-args":
        if args.file_only:
            file_opts = options_from_env_file(args.env) if args.env else {}
            print(" ".join(f"{OPTION_BY_KEY[k]['make_var']}={'yes' if v is True else ('' if v is False else v)}"
                           for k, v in file_opts.items()))
        else:
            print(" ".join(make_vars(plan.options)))
        return 0
    if args.json:
        print(json.dumps(plan.to_json(), indent=2))
        return 1 if plan.errors else 0

    print(f"Plan{' for environment ' + args.env if args.env else ''}:")
    for k, v in plan.labels.items():
        if v:
            print(f"  {k + ':':<13}{v}")
    print("\nCompose files (in order):")
    for f in plan.compose_files:
        print(f"  -f {f}")
    print("\nStorage:")
    for s in plan.stores:
        where = s["volume"] or s["kind"]
        print(f"  {s['name']:<20}{'persistent' if s['persistent'] else 'ephemeral ':<11} {where}"
              + (f"  ({s['note']})" if s.get("note") else ""))
    if plan.checks:
        print("\nPre-flight:")
        for c in plan.checks:
            print(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']}" + ("" if c["ok"] else f" - {c['detail']}"))
    for w in plan.warnings:
        print(f"\nwarning: {w}")
    for e in plan.errors:
        print(f"\nerror: {e}")
    print(f"\nEquivalent: make up {' '.join(make_vars(plan.options))}")
    return 1 if plan.errors else 0


if __name__ == "__main__":
    sys.exit(main())
