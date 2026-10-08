"""The knowledge base's CONTENT: complete, within budget, citing real files, free of secrets.

tests/test_knowledge.py covers the loader; this file holds the content to its promises: every saved-config
key and every template is documented, every component is named, the digests that go into the system
prompt stay small, every cited source exists, and nothing private leaks into text an agent repeats.
No network.
"""
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core import knowledge  # noqa: E402
from sirosid_core.components import component_names  # noqa: E402
from sirosid_core.policy import CAPABILITIES, SAVED_CONFIG_KEYS, validate  # noqa: E402
from sirosid_core.templates import config_templates  # noqa: E402

KDIR = ROOT / "sirosid_core" / "knowledge"
REQUIRED_TOPICS = {
    "overview", "concepts", "lifecycle", "config-reference", "templates", "trust", "issuing", "presenting",
    "mobile-testing", "registries", "attestation", "custom-images", "troubleshooting", "limits-and-policy",
    "mcp-and-agents",
}
CATEGORIES = {"get-started", "configure", "test", "manage", "troubleshoot"}
MAX_DIGEST_WORDS = 700
MAX_TOPIC_WORDS = 1600

SECRET_PATTERNS = [
    (r"fm2_", "a Fly macaroon token"),
    (r"FlyV1", "a Fly token"),
    (r"BEGIN ", "PEM material"),
    (r"(?i)password\s*[:=]\s*\S", "a password literal"),
    (r"(?i)secret\s*[:=]\s*[\"']?[A-Za-z0-9]", "a secret literal"),
    (r"\b[a-z0-9-]+\.internal\b", "an internal hostname"),
    (r"/home/|/Users/|[A-Za-z]:\\\\Users", "an absolute home path"),
    (r"(?i)leifj", "a personal name"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "a GitHub token"),
    (r"\bsk-[A-Za-z0-9-]{16,}", "an API key"),
    (r"\beyJ[A-Za-z0-9_-]{10,}", "a JWT"),
    # 32+ characters of base64url/hex with digits AND upper case: a token, a hash pin, a key - not prose
    # like draft-ietf-oauth-attestation-based-client-auth.
    (r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])",
     "a long token-like string"),
    (r"(?<![A-Fa-f0-9])[a-f0-9]{40,}(?![A-Fa-f0-9])", "a long hex string"),
]


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def sources_of(topic) -> list:
    lines = [ln for ln in topic.body.splitlines() if ln.startswith("Sources:")]
    return re.findall(r"`([^`]+)`", lines[-1]) if lines else []


def body_of(topic_id: str) -> str:
    t = knowledge.get_topic(topic_id)
    assert t is not None, topic_id
    return t.body


class TopicShapeTests(unittest.TestCase):
    def test_every_required_topic_exists_and_parses(self):
        topics = {t.id: t for t in knowledge.list_topics()}
        self.assertTrue(REQUIRED_TOPICS <= set(topics), sorted(REQUIRED_TOPICS - set(topics)))
        for t in topics.values():
            self.assertRegex(t.id, r"^[a-z0-9][a-z0-9-]*$")
            self.assertTrue(t.title and t.summary and t.digest and t.body and t.tags, t.id)

    def test_orders_are_unique(self):
        orders = [t.order for t in knowledge.list_topics()]
        self.assertEqual(len(orders), len(set(orders)))

    def test_digests_fit_the_system_prompt_budget(self):
        total = sum(words(t.digest) for t in knowledge.list_topics())
        self.assertLess(total, MAX_DIGEST_WORDS, f"digests total {total} words")
        for t in knowledge.list_topics():
            self.assertLessEqual(len(t.digest.splitlines()), 3, t.id)

    def test_no_topic_is_too_long(self):
        for t in knowledge.list_topics():
            self.assertLessEqual(words(t.body), MAX_TOPIC_WORDS, f"{t.id}: {words(t.body)} words")

    def test_every_topic_ends_with_a_sources_line(self):
        for t in knowledge.list_topics():
            self.assertTrue(t.body.rstrip().splitlines()[-1].startswith("Sources:"), t.id)
            self.assertTrue(sources_of(t), t.id)


class CoverageTests(unittest.TestCase):
    def test_every_saved_config_key_has_its_own_section(self):
        # A heading per key, not a passing mention: deleting a key's section must fail this test.
        body = body_of("config-reference")
        headings = set(re.findall(r"^### `([a-z_]+)`\s*$", body, re.M))
        missing = sorted(set(SAVED_CONFIG_KEYS) - headings)
        self.assertEqual(missing, [], f"config-reference lacks a section for: {missing}")
        unknown = sorted(headings - set(SAVED_CONFIG_KEYS))
        self.assertEqual(unknown, [], f"config-reference documents keys the policy does not have: {unknown}")

    def test_every_key_appears_in_the_digest_too(self):
        digest = knowledge.get_topic("config-reference").digest
        for key in SAVED_CONFIG_KEYS:
            self.assertIn(key, digest)

    def test_every_capability_is_documented(self):
        body = body_of("limits-and-policy")
        for cap in CAPABILITIES:
            self.assertIn(f"`{cap}`", body)

    def test_every_template_has_its_own_section(self):
        body = body_of("templates")
        headings = set(re.findall(r"^### `([a-z0-9-]+)`", body, re.M))
        ids = {t.id for t in config_templates()}
        self.assertEqual(sorted(ids - headings), [], "templates topic lacks a section")
        self.assertEqual(sorted(headings - ids), [], "templates topic documents a template that does not exist")

    def test_every_component_is_named_in_overview_or_concepts(self):
        text = body_of("overview") + "\n" + body_of("concepts")
        for name in component_names():
            self.assertIn(f"`{name}`", text, name)

    def test_config_examples_are_valid_configs(self):
        # Every ```json block in config-reference is a config the policy accepts for a fully
        # capable user; illustrative non-configs are written as ```text.
        body = body_of("config-reference")
        blocks = re.findall(r"```json\n(.*?)```", body, re.S)
        self.assertGreater(len(blocks), len(SAVED_CONFIG_KEYS) - 2)
        for block in blocks:
            doc = json.loads(block)
            self.assertEqual(validate(doc, CAPABILITIES), [], block)

    def test_json_examples_elsewhere_parse_and_validate(self):
        for t in knowledge.list_topics():
            for block in re.findall(r"```json\n(.*?)```", t.body, re.S):
                doc = json.loads(block)
                self.assertEqual(validate(doc, CAPABILITIES), [], f"{t.id}: {block}")

    def test_every_mcp_tool_is_documented(self):
        # Read from the source, so this runs without the service's requirements installed.
        src = (ROOT / "sirosid_service" / "mcp.py").read_text(encoding="utf-8")
        tools = set(re.findall(r'\bTool\("([a-z_]+)"', src))
        self.assertGreater(len(tools), 10)
        body = body_of("mcp-and-agents")
        for name in tools:
            self.assertIn(f"`{name}`", body, f"tool {name} is not documented")


class SafetyTests(unittest.TestCase):
    def _texts(self):
        for p in sorted(KDIR.glob("*.md")) + [KDIR / "examples.yaml"]:
            yield p.name, p.read_text(encoding="utf-8")

    def test_no_secrets_or_private_values(self):
        for name, text in self._texts():
            for pattern, what in SECRET_PATTERNS:
                m = re.search(pattern, text)
                self.assertIsNone(m, f"{name} contains {what}: {m.group(0) if m else ''!r}")

    def test_links_are_https_only(self):
        for name, text in self._texts():
            self.assertNotRegex(text, r"(?i)\bhttp://", name)
            for url in re.findall(r"\]\(([^)]+)\)", text):
                self.assertTrue(url.startswith("https://"), f"{name}: {url}")

    def test_cited_sources_exist(self):
        for t in knowledge.list_topics():
            for path in sources_of(t):
                self.assertFalse(path.startswith("/"), f"{t.id}: absolute path {path}")
                target = (ROOT / path).resolve()
                if path.startswith("../"):
                    sibling = (ROOT / path.split("/")[0] / path.split("/")[1]).resolve()
                    if not sibling.exists():
                        continue          # sibling checkout absent here: cannot check
                self.assertTrue(target.exists(), f"{t.id} cites {path}, which does not exist")


class ExampleTests(unittest.TestCase):
    def test_examples_are_well_formed_and_unique(self):
        raw = yaml.safe_load((KDIR / "examples.yaml").read_text(encoding="utf-8"))
        self.assertTrue(12 <= len(raw) <= 16, len(raw))
        for e in raw:
            self.assertEqual(set(e) - {"id", "title", "prompt", "category"}, set(), e)
        ex = knowledge.examples()
        for key in ("id", "title", "prompt"):
            values = [e[key] for e in ex]
            self.assertEqual(len(values), len(set(values)), f"duplicate {key}")
        for e in ex:
            self.assertRegex(e["id"], r"^[a-z0-9][a-z0-9-]*$")
            self.assertIn(e["category"], CATEGORIES, e["id"])
            self.assertLessEqual(len(e["title"]), 60, e["id"])
            # Only obvious <slots> may be left for the user; no template syntax that means nothing.
            self.assertNotRegex(e["prompt"], r"\{\{|\}\}|\$\{|\bTODO\b|\bXXX\b|\.\.\.\.", e["id"])
        self.assertEqual({e["category"] for e in ex}, CATEGORIES)


if __name__ == "__main__":
    unittest.main()
