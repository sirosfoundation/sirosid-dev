"""The knowledge loader: front matter, ordering, search, the system-prompt digest."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core import knowledge  # noqa: E402


class LoaderTests(unittest.TestCase):
    def test_topics_load_with_front_matter_and_order(self):
        topics = knowledge.list_topics()
        self.assertTrue(topics)
        self.assertEqual([t.order for t in topics], sorted(t.order for t in topics))
        for t in topics:
            self.assertRegex(t.id, r"^[a-z0-9][a-z0-9-]*$")
            self.assertTrue(t.title and t.summary and t.body, t.id)

    def test_get_topic_refuses_anything_that_is_not_a_topic_id(self):
        first = knowledge.list_topics()[0]
        self.assertEqual(knowledge.get_topic(first.id).id, first.id)
        for bad in ("", "../x", "a/b", "A", "nope", None, "overview.md"):
            self.assertIsNone(knowledge.get_topic(bad), bad)

    def test_search_ranks_and_snippets(self):
        hits = knowledge.search("disposable environment")
        self.assertTrue(hits)
        topic, snippet = hits[0]
        self.assertTrue(snippet)
        self.assertEqual(knowledge.search("   "), [])
        self.assertEqual(knowledge.search("zzzzqqqq"), [])

    def test_overview_lists_digests_and_topics(self):
        text = knowledge.overview()
        self.assertIn("get_knowledge", text)
        for t in knowledge.list_topics():
            self.assertIn(t.id, text)

    def test_examples_are_well_formed(self):
        ex = knowledge.examples()
        self.assertTrue(ex)
        ids = [e["id"] for e in ex]
        self.assertEqual(len(ids), len(set(ids)))
        for e in ex:
            self.assertTrue(e["title"] and e["prompt"] and e["category"])


if __name__ == "__main__":
    unittest.main()
