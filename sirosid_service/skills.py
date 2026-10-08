"""The platform's SKILLS: runbooks for the common tasks, offered as MCP prompts.

Each prompt renders into one user message telling an agent exactly which tools to call, in which
order, what to check between them and which knowledge to read first. They name only tools that
exist in the MCP tool table (tests/test_mcp.py parses every backticked name in every rendered
prompt and checks it against the table and the config schema), and only knowledge topics that
exist right now: a topic is named only if the knowledge base has it, and every step that reads
knowledge also says what to search for, so a runbook stays correct while the knowledge base
changes underneath it.

Arguments are strings (the MCP prompt contract), bounded and validated here; a bad one is an
invalid-params error, never a runbook built on a typo.
"""
import re
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

from sirosid_core import knowledge
from sirosid_core.android import identities_from_entries

MAX_ARG = 500
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class PromptArgumentError(ValueError):
    pass


@dataclass(frozen=True)
class Arg:
    name: str
    description: str
    required: bool = False
    check: Callable[[str], str] = None        # returns an error message, or ""


@dataclass(frozen=True)
class Prompt:
    name: str
    title: str
    description: str
    args: Tuple[Arg, ...]
    body: Callable[[dict], str]
    topics: Tuple[str, ...] = ()              # topic ids to read first, when the knowledge base has them
    query: str = ""                           # what to search for otherwise (or as well)

    def listing(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "arguments": [{"name": a.name, "description": a.description, "required": a.required} for a in self.args]}

    def render(self, given: dict) -> dict:
        known = {a.name: a for a in self.args}
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise PromptArgumentError(f"unknown arguments for {self.name}: {unknown}")
        args = {}
        for a in self.args:
            v = given.get(a.name)
            if v is None or v == "":
                if a.required:
                    raise PromptArgumentError(f"{self.name} needs the argument {a.name!r}")
                continue
            if not isinstance(v, str):
                raise PromptArgumentError(f"argument {a.name!r} must be a string")
            v = _CONTROL.sub(" ", v).strip()
            if len(v) > MAX_ARG:
                raise PromptArgumentError(f"argument {a.name!r} is longer than {MAX_ARG} characters")
            problem = a.check(v) if a.check else ""
            if problem:
                raise PromptArgumentError(f"argument {a.name!r}: {problem}")
            args[a.name] = v
        text = read_first(self.topics, self.query) + "\n\n" + self.body(args).strip()
        return {"description": self.description, "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def read_first(topic_ids, query: str) -> str:
    """Step 0 of every runbook: which knowledge to read. Names only topics that exist now."""
    have = {t.id for t in knowledge.list_topics()}
    named = [t for t in topic_ids if t in have]
    lines = ["Before you act, read what the platform knows:"]
    for t in named:
        lines.append(f'- `get_knowledge`(topic="{t}")')
    if query:
        lines.append(f'- `get_knowledge`(query="{query}") and read the best match in full'
                     + (" if the topics above do not cover it" if named else ""))
    lines.append("Follow what the knowledge says where it is more specific than this runbook.")
    return "\n".join(lines)


# ---- argument checks -------------------------------------------------------------------------

_INSTANCE_ID = re.compile(r"^[a-z2-7]{8}$")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def _instance(v):
    return "" if _INSTANCE_ID.match(v) else "must be an environment id (8 characters, as list_instances shows)"


def _kind(v):
    return "" if v in ("issuer", "verifier") else "must be 'issuer' or 'verifier'"


def _package(v):
    return "" if _PACKAGE.match(v) else "must be an Android package name such as org.example.app"


def _fingerprint(v):
    try:
        identities_from_entries([f"org.example.check={v}"])
    except Exception:                     # noqa: BLE001 - a malformed value, whichever way it fails
        return "must be the signing certificate's SHA-256 fingerprint (32 colon-separated hex bytes, as keytool prints it)"
    return ""


INSTANCE = Arg("instance", "the environment's id", True, _instance)

# ---- the runbooks --------------------------------------------------------------------------------

POLL = ("Poll `get_instance_health` (it is cheap) every 20 to 30 seconds and `get_instance` for the status, until the "
        "status is \"running\" - or \"failed\": then read `get_instance_activity` and the instance's error, explain it "
        "plainly, and stop.")


def _spin_up(a):
    label = a.get("label", "")
    if a.get("template"):
        pick = (f'Use the template with id "{a["template"]}". If it is not listed, say so, list the ids that are, and ask '
                "which to use.")
    else:
        pick = 'Pick the template that fits what the user asked for; "standard" when they did not say.'
    return f"""
Goal: create a new SIROS ID Dev environment{f' labelled "{label}"' if label else ''} and give the user its wallet URL.

1. Call `get_account` (limits) and `list_instances`. If the user is already at max_concurrent, stop: say which
   environments they could stop or destroy and let them choose. Never destroy anything to make room yourself.
2. Call `list_config_templates`. {pick}
3. If the user asked for changes (trusted issuers or verifiers, Android apps, credential registries, ...), copy the
   template's config, apply exactly those changes and call `validate_config`. Fix every problem it reports before
   going on; `get_config_schema` lists every key.
4. Optionally `save_config` under a short name so the config can be re-used, then `create_instance` with that
   config_name (or with the config inline){f', name="{label}"' if label else ''}. It returns at once with status "creating".
5. Tell the user this takes a few minutes. {POLL}
6. When it is running, give the wallet URL (urls["wallet-frontend"] from `get_instance`) and the other public URLs,
   say when it expires (expires_at) and that `set_keep` keeps it longer if their allowance permits.
"""


def _reconfigure(a):
    return f"""
Goal: change environment {a['instance']} as follows: {a['change']}

1. `get_instance` id="{a['instance']}". Its status must be "running" or "failed" (reconfigurable is true). If it is
   "stopped", ask the user whether to start it (`start_instance`): a stopped environment cannot be reconfigured. If it is
   "creating", "resetting" or "reconfiguring", wait for it with `get_instance_health`.
2. `get_instance_config` id="{a['instance']}": the config it runs now. Keep it - it is the way back.
3. Build the new config: the current one with ONLY the requested change applied. Keep every other key as it is.
   `get_config_schema` names every key; never invent one.
4. `validate_config` with the new config and fix every problem it reports.
5. Tell the user which keys change, and that reconfiguring redeploys the environment: its components restart (a few
   minutes; data, accounts and secrets are kept). Then call `reconfigure_instance` id="{a['instance']}" with the
   complete new config. The user may have to approve it.
6. {POLL} A failed reconfigure leaves the previous config in place: offer `reconfigure_instance` with the config from
   step 2 to go back.
"""


def _trusted(a):
    if a["kind"] == "issuer":
        rule = (f'Add "{a["url_or_identity"]}" to `trusted_issuers`. It must be the issuer\'s https URL as it appears in '
                "its credentials (their iss claim: the credential issuer's public URL) - not an internal address.")
    else:
        rule = (f'Add "{a["url_or_identity"]}" to `trusted_verifiers`. Accepted forms: an https URL, or x509_hash:...,'
                " x509_san_dns:<host>, x509_san_uri:<uri>. The trust service compares x509_san_dns:<host> as"
                " https://<host>, so prefer the https form for those. If the verifier's request-signing certificate is"
                " issued by a private reader CA, its CA certificate (PEM) must also go in `trusted_verifier_roots`: ask"
                " the user for it.")
    return f"""
Goal: make environment {a['instance']} trust the external {a['kind']} {a['url_or_identity']}.

1. `get_instance` id="{a['instance']}": it must be "running" or "failed" (start a stopped one first, with the user's agreement).
2. `get_instance_config` id="{a['instance']}".
3. {rule} Do not add a duplicate; keep every other key as it is.
4. `validate_config` with the new config; fix every problem.
5. `reconfigure_instance` id="{a['instance']}" with the new config (it restarts the environment's components; the user
   may have to approve it). {POLL}
6. Tell the user it is in place. Trust applies to new issuance and presentations, and the trust service can take
   several minutes to become ready when an issuer publishes no JWKS.
"""


def _android(a):
    entry = f"{a['package']}={a['fingerprint']}"
    return f"""
Goal: let the Android app {a['package']} use passkeys against environment {a['instance']}.

1. `get_instance` id="{a['instance']}": it must be "running" or "failed" (start a stopped one first, with the user's agreement).
2. `get_instance_config` id="{a['instance']}".
3. Add "{entry}" to `android_apps` (replace an existing entry for the same package; keep every other key). The
   fingerprint is the SHA-256 of the APK's signing certificate as keytool prints it - for a debug build, the debug
   keystore's, which differs from the release key.
4. `validate_config`; fix every problem. Then `reconfigure_instance` id="{a['instance']}" with the new config (the
   user may have to approve it). {POLL}
5. When it is running, tell the user: the environment's wallet URL (urls["wallet-frontend"]) now serves
   /.well-known/assetlinks.json listing the app; point the app at that environment; Android caches assetlinks, so
   reinstall the app (or clear its data) if it still refuses the passkey.
"""


def _diagnose(a):
    return f"""
Goal: find out why environment {a['instance']} is not working, and say what to do about it.

1. `get_instance` id="{a['instance']}": status, error, urls, expires_at.
2. `get_instance_health` id="{a['instance']}": which components are not healthy and their detail. An error there
   means Fly did not answer; try once more before drawing conclusions.
3. `get_instance_activity` id="{a['instance']}": what happened last (failed deploys, resets, reconfigures) and who did it.
4. `get_instance_config` id="{a['instance']}": what it runs; compare with what the user expects.
5. Search the knowledge base (`get_knowledge` with a query) for the symptom and the failing component.
6. Explain plainly what is wrong, the likely cause and the safest next step. In order of preference: wait (still
   creating), `start_instance` (stopped), `reconfigure_instance` with a fixed or the previous config (failed
   reconfigure), `reset_instance` (erases the environment's data), `destroy_instance` and create a new one (last
   resort). Never run reset or destroy unless the user explicitly agrees.
"""


def _clean_up(a):
    return """
Goal: help the user tidy up their environments.

1. `get_account` (limits, keep allowance) and `list_instances`.
2. Show a short table: id, label, status, expires_at, kept. Flag failed ones, stopped ones, ones expiring within a
   day and kept ones using the keep allowance.
3. Suggest per environment, and wait for the user to confirm each by id: `stop_instance` (data kept, only storage is
   billed), `set_keep` with keep=false (back on the normal expiry clock), or `destroy_instance` (irreversible).
   Never destroy without an explicit yes for that id.
4. Saved configs are separate from environments: `list_configs`; `delete_config` only when the user asks.
"""


PROMPTS: Dict[str, Prompt] = {p.name: p for p in (
    Prompt("spin-up-environment", "Spin up an environment",
           "Create a new environment from a template, wait until it is running and hand over its URLs.",
           (Arg("template", "a template id from list_config_templates (default: the one that fits, else 'standard')"),
            Arg("label", "a short label for the environment")),
           _spin_up, topics=("overview", "templates", "lifecycle"), query="create an environment from a template"),
    Prompt("reconfigure-environment", "Reconfigure an environment",
           "Change one environment's config and redeploy it, keeping its data.",
           (INSTANCE, Arg("change", "what to change, in plain words", True)),
           _reconfigure, topics=("config-reference", "reconfigure", "lifecycle"), query="reconfigure an environment config"),
    Prompt("add-trusted-party", "Trust an external issuer or verifier",
           "Make an environment's trust service accept a partner's issuer or verifier.",
           (INSTANCE, Arg("kind", "'issuer' or 'verifier'", True, _kind),
            Arg("url_or_identity", "the issuer's https URL, or the verifier's identity", True)),
           _trusted, topics=("trust", "config-reference"), query="trusted issuers verifiers trust"),
    Prompt("test-android-passkeys", "Test an Android app's passkeys",
           "Let an Android app (package + signing certificate fingerprint) use passkeys against an environment.",
           (INSTANCE, Arg("package", "the Android package name", True, _package),
            Arg("fingerprint", "SHA-256 fingerprint of the app's signing certificate", True, _fingerprint)),
           _android, topics=("android", "android-passkeys", "config-reference"), query="android passkeys assetlinks"),
    Prompt("diagnose-environment", "Diagnose an environment",
           "Find out why an environment is failing or misbehaving and what to do about it.",
           (INSTANCE,), _diagnose, topics=("troubleshooting", "lifecycle"), query="troubleshooting a failed environment"),
    Prompt("clean-up-environments", "Clean up environments",
           "Review the user's environments and stop, release or destroy the ones they no longer need.",
           (), _clean_up, topics=("lifecycle",), query="expiry keep stop destroy"),
)}
