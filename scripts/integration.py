#!/usr/bin/env python3
"""What is in an integration branch, derived from its own history - and proof that nothing else is.

An `integration/<name>` branch is a "bag of PRs": its base plus ordered `--no-ff` merges of pull-request heads. This tool
needs no manifest file in the branch, because the history already holds the list:

    integration.py report --path <clone> --tip <ref> [--base origin/main] [--repos SUNET/vc,sirosfoundation/vc] [--json]
    integration.py verify --path <clone> --tip <ref> [--base origin/main]

`report` prints the base, the ordered PRs (the latest merged head of each) and anything on the branch that is not a PR
merge. With `--repos` each merged head is mapped to its pull request through the GitHub API (number, title, state, draft,
labels, and whether the PR's head has moved since it was merged).

`verify` exits non-zero when the branch contains anything a replay of its PR merges would not produce:
  - a commit on the first-parent path that is not a merge (a hand edit, a "patch");
  - a merge whose result differs from the automatic merge of its parents (a conflict resolved, or a change slipped in,
    inside the merge commit - the drift a list of PRs cannot show);
  - a merge of a second parent that is not a PR head (checked with the API when `--repos` is given).
A branch that fails is regenerated from its PR list, never patched.

    integration.py sync --path <clone> --branch integration/<name> --base origin/main --pr owner/repo#N [--pr ...] [--push]
    integration.py sync --path <clone> --branch integration/<name> --from integration/<old> --repos ...   # same PRs, latest heads

`sync` (re)creates the branch from a PR list: base, then one `--no-ff` merge per PR head in the order given, each with the
message `Merge PR owner/repo#N @<head7>: <title>` so `report` can read the list back offline. A conflict stops it: resolve it in
a (draft, never-for-upstream) glue PR that goes in the list, never by hand in the merge.

The first-parent path starts at the merge base of `--tip` and `--base`. A merge's second parent is the PR head that was merged.
"""
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def git(path, *args, check=True):
    out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out


@dataclass
class Step:
    sha: str
    parents: list
    subject: str
    tree: str
    kind: str = ""              # "merge" | "commit"
    problems: list = field(default_factory=list)


@dataclass
class Analysis:
    base: str
    tip: str
    steps: list

    @property
    def merges(self):
        return [s for s in self.steps if s.kind == "merge"]

    @property
    def extra_commits(self):
        return [s for s in self.steps if s.kind == "commit"]

    @property
    def problems(self):
        return [(s, p) for s in self.steps for p in s.problems]


def analyze(path, tip, base_ref="origin/main") -> Analysis:
    """Walk the first-parent path from the merge base to the tip and check every step against a replay."""
    tip_sha = git(path, "rev-parse", "--verify", f"{tip}^{{commit}}").stdout.strip()
    base = git(path, "merge-base", tip_sha, base_ref).stdout.strip()
    if not base:
        raise SystemExit(f"{tip} shares no history with {base_ref}")
    lines = git(path, "log", "--first-parent", "--reverse", "--format=%H|%P|%T|%s", f"{base}..{tip_sha}").stdout.splitlines()
    steps = []
    for line in lines:
        sha, parents, tree, subject = line.split("|", 3)
        step = Step(sha, parents.split(), subject, tree)
        if len(step.parents) == 1:
            step.kind = "commit"
            step.problems.append("a commit that is not a merge of a pull request (hand edit / patch)")
        else:
            step.kind = "merge"
            if len(step.parents) > 2:
                step.problems.append("an octopus merge")
            else:
                replay = git(path, "merge-tree", "--write-tree", "--no-messages", *step.parents, check=False)
                if replay.returncode != 0:
                    step.problems.append("the merge conflicts when replayed: it was resolved by hand inside the merge commit")
                elif replay.stdout.split()[0] != tree:
                    step.problems.append("the merge result differs from the automatic merge of its parents (a change inside "
                                         "the merge commit)")
        steps.append(step)
    return Analysis(base, tip_sha, steps)


def lookup_prs(repos, sha, run=subprocess.run):
    """The pull requests that contain `sha`, from the first of `repos` that knows it (`gh api`)."""
    for repo in repos:
        out = run(["gh", "api", f"repos/{repo}/commits/{sha}/pulls", "-H", "Accept: application/vnd.github+json"],
                  capture_output=True, text=True)
        if out.returncode == 0:
            try:
                found = json.loads(out.stdout)
            except json.JSONDecodeError:
                continue
            if found:
                return [{"repo": repo, "number": p["number"], "title": p["title"], "state": p["state"] if not p.get("merged_at")
                         else "merged", "draft": p.get("draft", False), "head": p["head"]["sha"],
                         "labels": [l["name"] for l in p.get("labels", [])]} for p in found]
    return []


MESSAGE = re.compile(r"^Merge PR (?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+) @(?P<sha>[0-9a-f]{7,40})(?:: (?P<title>.*))?$")


def from_message(subject):
    """The PR a `sync` merge message names (offline, no API), or None for any other message."""
    m = MESSAGE.match(subject)
    return m.groupdict() if m else None


def report(analysis: Analysis, repos=(), lookup=lookup_prs) -> dict:
    """Ordered PRs (the latest merged head of each), plus everything that is not a PR merge."""
    heads = []                              # [(second parent, merge commit)] in merge order
    for m in analysis.merges:
        if len(m.parents) == 2:
            heads.append((m.parents[1], m))
    prs, unmapped, seen = [], [], {}
    for head, merge in heads:
        named = from_message(merge.subject)
        found = lookup(repos, head) if repos else []
        if not found and named:
            found = [{"repo": named["repo"], "number": int(named["number"]), "title": named.get("title") or "", "state": "unknown",
                      "draft": False, "head": head, "labels": []}]
        if not found:
            unmapped.append({"merged_head": head, "merge": merge.sha, "subject": merge.subject})
            continue
        for pr in found:
            key = (pr["repo"], pr["number"])
            entry = {"repo": pr["repo"], "number": pr["number"], "title": pr["title"], "state": pr["state"],
                     "draft": pr["draft"], "labels": pr["labels"], "merged_head": head, "current_head": pr["head"],
                     "head_moved": head != pr["head"], "first_merged_at": seen[key]["first_merged_at"] if key in seen else merge.sha}
            if key in seen:                 # re-merged as the PR moved: keep the latest head, in the position of the first merge
                prs[prs.index(seen[key])] = entry
            else:
                prs.append(entry)
            seen[key] = entry
    return {"base": analysis.base, "tip": analysis.tip, "prs": prs, "unmapped_merges": unmapped,
            "merges": len(analysis.merges), "extra_commits": [{"sha": c.sha, "subject": c.subject} for c in analysis.extra_commits],
            "drift": [{"commit": s.sha, "subject": s.subject, "problem": p} for s, p in analysis.problems]}


def parse_pr(spec):
    """`owner/repo#N` or `owner/repo#N@<sha>` -> (repo, number, expected head or None)."""
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)(?:@([0-9a-f]{7,40}))?", spec)
    if not m:
        raise SystemExit(f"not a PR reference (owner/repo#N[@sha]): {spec}")
    return m.group(1), int(m.group(2)), m.group(3)


def ssh_url(repo):
    return f"git@github.com:{repo}.git"


def sync(path, branch, base_ref, prs, url_for=ssh_url, titles=None, push_to=None):
    """(Re)create `branch` as base_ref plus one `--no-ff` merge per PR head, in order. Returns [(repo, number, head)]."""
    path = Path(path)
    if git(path, "status", "--porcelain").stdout.strip():
        raise SystemExit("the working tree is not clean")
    merged = []
    git(path, "checkout", "-q", "-B", branch, base_ref)
    for repo, number, expected in prs:
        ref = f"refs/integration/{repo.replace('/', '_')}_pr{number}"
        fetched = git(path, "fetch", "-q", url_for(repo), f"+pull/{number}/head:{ref}", check=False)
        if fetched.returncode != 0:
            raise SystemExit(f"cannot fetch {repo}#{number}: {fetched.stderr.strip()}")
        head = git(path, "rev-parse", ref).stdout.strip()
        if expected and not head.startswith(expected):
            raise SystemExit(f"{repo}#{number} head is {head[:9]}, not the requested {expected}: the PR has moved")
        title = (titles or {}).get((repo, number), "")
        message = f"Merge PR {repo}#{number} @{head[:9]}" + (f": {title}" if title else "")
        merge = git(path, "merge", "--no-ff", "-m", message, head, check=False)
        if merge.returncode != 0:
            git(path, "merge", "--abort", check=False)
            raise SystemExit(f"{repo}#{number} @{head[:9]} conflicts with what is already on {branch}. Resolve it in a glue PR "
                             f"added to the list (before this one), not by hand: {merge.stdout.strip()[:300]}")
        merged.append((repo, number, head))
    if push_to:
        git(path, "push", "--force-with-lease", push_to, f"{branch}:{branch}")
    return merged


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("report", "verify", "sync"))
    ap.add_argument("--path", required=True, help="a clone of the repository holding the integration branch")
    ap.add_argument("--tip", help="the integration branch (or commit)")
    ap.add_argument("--branch", help="sync: the branch to (re)create")
    ap.add_argument("--pr", action="append", default=[], help="sync: owner/repo#N[@sha], in merge order (repeatable)")
    ap.add_argument("--from", dest="source", help="sync: take the PR list (latest heads) from this existing branch")
    ap.add_argument("--push", metavar="REMOTE", help="sync: force-with-lease push the branch to this remote")
    ap.add_argument("--base", default="origin/main", help="what the branch was built on (default origin/main)")
    ap.add_argument("--repos", default="", help="comma-separated owner/name list searched for each merged head's PR")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--summary", action="store_true", help="report: one line, owner/repo#N@head9,... (an image label)")
    args = ap.parse_args(argv)
    repos = [r for r in args.repos.split(",") if r]
    if args.command == "sync":
        if not args.branch:
            raise SystemExit("sync needs --branch")
        prs = [parse_pr(p) for p in args.pr]
        titles = {}
        if args.source:
            listed = report(analyze(Path(args.path), args.source, args.base), repos)
            if listed["unmapped_merges"]:
                raise SystemExit("some merges of the source branch name no PR; pass --repos or --pr explicitly")
            prs += [(p["repo"], p["number"], None) for p in listed["prs"]]
            titles = {(p["repo"], p["number"]): p["title"] for p in listed["prs"]}
        if not prs:
            raise SystemExit("sync needs at least one --pr (or --from)")
        for repo, number, head in sync(args.path, args.branch, args.base, prs, titles=titles, push_to=args.push):
            print(f"merged {repo}#{number} @{head[:9]}")
        return 0
    if not args.tip:
        raise SystemExit(f"{args.command} needs --tip")
    analysis = analyze(Path(args.path), args.tip, args.base)
    data = report(analysis, repos)
    if args.command == "verify":
        if repos and data["unmapped_merges"]:
            for u in data["unmapped_merges"]:
                data["drift"].append({"commit": u["merge"], "subject": u["subject"],
                                      "problem": f"the merged head {u['merged_head'][:9]} is not a head of any pull request in {', '.join(repos)}"})
        for d in data["drift"]:
            print(f"DRIFT {d['commit'][:9]} {d['subject'][:70]}: {d['problem']}", file=sys.stderr)
        print(f"{'FAILED' if data['drift'] else 'OK'}: {data['merges']} merges, {len(data['drift'])} problems", file=sys.stderr)
        return 1 if data["drift"] else 0
    if args.summary:
        print(",".join(f"{p['repo']}#{p['number']}@{p['merged_head'][:9]}" for p in data["prs"]))
    elif args.json:
        print(json.dumps(data, indent=2))
    else:
        import yaml
        print(yaml.safe_dump(data, sort_keys=False, width=140))
    return 0


if __name__ == "__main__":
    sys.exit(main())
