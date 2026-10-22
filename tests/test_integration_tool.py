#!/usr/bin/env python3
"""scripts/integration.py: what an integration branch contains, and proof that nothing else slipped in.

Each case builds a real git repository, so `verify` is exercised against what git actually records: clean PR merges pass;
a plain commit on the branch, a change hidden inside a merge commit, and a conflict resolved by hand inside a merge all fail."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import integration  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.org", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


class Repo:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.git("init", "-q", "-b", "main")
        self.write("a.txt", "base\n")
        self.commit("base")

    def git(self, *args, check=True):
        out = subprocess.run(["git", "-C", str(self.dir), *args], capture_output=True, text=True, env=ENV)
        if check and out.returncode != 0:
            raise AssertionError(f"git {args}: {out.stderr}")
        return out.stdout.strip()

    def write(self, name, text):
        (self.dir / name).write_text(text)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def pr(self, name, filename, text, base="main"):
        """A PR branch off `base` adding one file; returns its head."""
        self.git("checkout", "-q", "-b", name, base)
        self.write(filename, text)
        head = self.commit(f"feat: {name}")
        self.git("checkout", "-q", "main")
        return head

    def integration(self):
        self.git("checkout", "-q", "-b", "integration/x", "main")

    def merge(self, branch, resolve=None):
        out = subprocess.run(["git", "-C", str(self.dir), "merge", "--no-ff", "-m", f"Merge {branch}", branch], capture_output=True,
                             text=True, env=ENV)
        if out.returncode != 0:
            assert resolve, out.stdout
            resolve()
            self.git("add", "-A")
            self.git("commit", "-q", "--no-edit")


def analysis(repo):
    return integration.analyze(repo.dir, "integration/x", "main")


class Verify(unittest.TestCase):
    def test_clean_pr_merges_have_no_drift_and_are_listed_in_order(self):
        r = Repo()
        h1 = r.pr("pr-one", "one.txt", "1\n")
        h2 = r.pr("pr-two", "two.txt", "2\n")
        r.integration()
        r.merge("pr-one")
        r.merge("pr-two")
        a = analysis(r)
        self.assertEqual([s.kind for s in a.steps], ["merge", "merge"])
        self.assertEqual(a.problems, [])
        self.assertEqual([m.parents[1] for m in a.merges], [h1, h2])
        self.assertEqual(integration.report(a)["extra_commits"], [])

    def test_a_plain_commit_on_the_branch_is_drift(self):
        r = Repo()
        r.pr("pr-one", "one.txt", "1\n")
        r.integration()
        r.merge("pr-one")
        r.write("patch.txt", "a hand edit\n")
        r.commit("patch: not in any PR")
        a = analysis(r)
        self.assertEqual([s.kind for s in a.steps], ["merge", "commit"])
        self.assertEqual(len(a.problems), 1)
        self.assertIn("not a merge", a.problems[0][1])
        self.assertEqual(len(integration.report(a)["extra_commits"]), 1)

    def test_a_change_hidden_inside_a_merge_commit_is_drift(self):
        r = Repo()
        r.pr("pr-one", "one.txt", "1\n")
        r.integration()
        subprocess.run(["git", "-C", str(r.dir), "merge", "--no-ff", "--no-commit", "pr-one"], capture_output=True, env=ENV)
        r.write("smuggled.txt", "not from the PR\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "Merge pr-one")
        a = analysis(r)
        self.assertEqual(len(a.problems), 1)
        self.assertIn("differs from the automatic merge", a.problems[0][1])

    def test_a_conflict_resolved_by_hand_inside_a_merge_is_drift(self):
        r = Repo()
        r.pr("pr-one", "a.txt", "one\n")
        r.pr("pr-two", "a.txt", "two\n")
        r.integration()
        r.merge("pr-one")
        r.merge("pr-two", resolve=lambda: r.write("a.txt", "both\n"))
        a = analysis(r)
        self.assertEqual(len(a.problems), 1)
        self.assertIn("conflicts when replayed", a.problems[0][1])

    def test_the_same_pr_merged_again_as_it_moves_is_one_entry_with_its_latest_head(self):
        r = Repo()
        h1 = r.pr("pr-one", "one.txt", "1\n")
        r.integration()
        r.merge("pr-one")
        r.git("checkout", "-q", "pr-one")
        r.write("one.txt", "1b\n")
        h2 = r.commit("feat: pr-one, again")
        r.git("checkout", "-q", "integration/x")
        r.merge("pr-one")

        def lookup(repos, sha):
            return [{"repo": "o/r", "number": 7, "title": "pr one", "state": "open", "draft": False, "head": h2, "labels": []}]
        rep = integration.report(analysis(r), ["o/r"], lookup)
        self.assertEqual(len(rep["prs"]), 1)
        self.assertEqual((rep["prs"][0]["merged_head"], rep["prs"][0]["head_moved"]), (h2, False))
        self.assertEqual(rep["merges"], 2)
        self.assertNotEqual(h1, h2)

    def test_a_merged_head_that_is_no_pr_is_reported_unmapped(self):
        r = Repo()
        r.pr("pr-one", "one.txt", "1\n")
        r.integration()
        r.merge("pr-one")
        rep = integration.report(analysis(r), ["o/r"], lambda repos, sha: [])
        self.assertEqual(len(rep["unmapped_merges"]), 1)
        self.assertEqual(rep["prs"], [])

    def test_the_cli_exits_non_zero_on_drift_and_zero_when_clean(self):
        clean = Repo()
        clean.pr("pr-one", "one.txt", "1\n")
        clean.integration()
        clean.merge("pr-one")
        self.assertEqual(integration.main(["verify", "--path", str(clean.dir), "--tip", "integration/x", "--base", "main"]), 0)
        clean.write("patch.txt", "x\n")
        clean.commit("patch")
        self.assertEqual(integration.main(["verify", "--path", str(clean.dir), "--tip", "integration/x", "--base", "main"]), 1)

    def test_report_json_is_machine_readable(self):
        r = Repo()
        r.pr("pr-one", "one.txt", "1\n")
        r.integration()
        r.merge("pr-one")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            integration.main(["report", "--path", str(r.dir), "--tip", "integration/x", "--base", "main", "--json"])
        data = json.loads(buf.getvalue())
        self.assertEqual(set(data), {"base", "tip", "prs", "unmapped_merges", "merges", "extra_commits", "drift"})


if __name__ == "__main__":
    unittest.main()
