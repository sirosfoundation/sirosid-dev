# Persisted Fly.io environment configs

Every `make fly-up ENV=<name>` flag (`IMAGES=`, `TRUSTED_ISSUERS=`,
`TRUSTED_VERIFIERS=`, `TRUSTED_VERIFIER_ROOTS=`, `ZK_CIRCUITS_SOURCES=`,
`CONFORMANCE=`, `WALLET_ATTESTATION=`) is otherwise purely ephemeral -
nothing records what a given named environment is actually running, so a
redeploy that forgets to repeat a flag silently drops it, and overriding
only one component's image while a sibling shares the same rendered config
file can crash-loop the one left behind if the config's shape changed
underneath it.

`environments/<name>.yaml` closes that gap for a *named, durable* test
environment - one meant to be redeployed the same way every time, by
anyone, not a personal scratch environment (`alice`/`bob`, ...). If the
file exists, `make fly-up ENV=<name>` merges its contents in as defaults
automatically; CLI flags on that same invocation add to (or, for image
pins, override per-component) the file for that one run only - they never
edit the file. See `scripts/env_config.py`'s module doc for the exact
merge/precedence rules and the full schema.

- `make env-show ENV=<name>` - print what a name currently resolves to.
- Update the file itself (not just a one-off CLI flag) whenever an
  environment's *intended* state changes - e.g. a new image build meant to
  stick around, not just a single test run.

## Adding a new persisted environment

Copy the shape from `gdc.yaml` and fill in only the keys that differ from
the (all-empty/false) defaults. Nothing is required - an empty file
(or no file at all) behaves exactly like today's CLI-flags-only path.
