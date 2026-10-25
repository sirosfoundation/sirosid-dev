# Integration branches: a documented "bag of PRs"

We test builds that combine several unmerged pull requests (see the PIN PROVENANCE block in `environments/gdc.yaml`). The rule
that keeps them honest:

> An `integration/<name>` branch is its base plus ordered `--no-ff` merges of **pull-request heads**, and nothing else.
> No hand edits, no patch commits, no change hidden in a merge commit. Glue that is needed to make PRs fit together is itself
> a (draft, never-for-upstream) PR in the list.

There is no manifest file: the branch history already records what was merged, and `scripts/integration.py` reads it back.

```sh
# what is in it: base, the ordered PRs (the latest merged head of each), and anything that is not a PR merge
python3 scripts/integration.py report --path <clone> --tip integration/<name> --base origin/main \
    --repos SUNET/vc,sirosfoundation/vc [--json]

# proof that nothing else slipped in (exit 1 and the offending commits on stderr otherwise)
python3 scripts/integration.py verify --path <clone> --tip integration/<name> --base origin/main [--repos ...]
```

`verify` fails on: a commit on the first-parent path that is not a merge; a merge whose result differs from the automatic
merge of its parents (a change inside the merge commit); a merge that conflicts when replayed (a conflict resolved by hand
inside the merge); and, with `--repos`, a merged head that is not the head of any pull request. A branch that fails is
regenerated from its PR list, never patched. `report` also shows when a PR's head has moved since it was merged (the branch is
stale) and whether the PR has since been merged upstream (re-pin to a release).

First run (2026-10-22): vc `integration/gdc-nymauth-v5` @ `c8c5b1ea` has 12 PRs but also 47 commits that are not merges of a
PR (the branch started on a feature line), and 51 problems in all; the engine branch `local/gdc-nymauth-integration-4`
@ `1d0708b` has a conflict resolved by hand in its #163 merge and two local-only commits (`a25df83`, `1d0708b`).
Neither can be built as a documented bundle until it is regenerated.
