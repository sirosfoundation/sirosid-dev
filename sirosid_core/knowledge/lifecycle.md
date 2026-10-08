---
title: "Lifecycle - create, stop, start, reset, keep, destroy"
summary: "What each lifecycle operation does to an instance's data and URLs, which statuses allow it, and what the status values mean."
digest: "create returns at once with status 'creating' - poll get_instance until 'running' or 'failed' (takes minutes). stop/start keep data and URLs; reset wipes data but keeps URLs and keys; destroy deletes everything. reset, destroy and delete_config need the user's approval."
order: 30
tags: [lifecycle, create, stop, start, reset, destroy, keep, status, reconfigure]
---
## Status values

| status | meaning | what you can do |
|---|---|---|
| `creating` | the deploy job is running | wait; poll `get_instance`; `destroy_instance` works |
| `running` | deploy (or start/reset) finished; `urls` is filled in | stop, reset, destroy, keep, read credentials |
| `stopped` | every container is stopped; data and URLs kept | start, reset, destroy, keep, read credentials |
| `resetting` | data is being wiped and the stack restarted | wait; poll `get_instance` |
| `reconfiguring` | a new config is being applied (components restart) | wait; poll `get_instance` / `get_instance_health` |
| `failed` | the last operation failed; `error` says why | start, stop, reset, destroy |
| `destroyed` | gone; no longer listed | nothing |

`stopping` and `starting` are reserved status names; the current control plane runs stop and start
inside the call and goes straight to `stopped` / `running` (or `failed`).

## create

`create_instance` with `config_name` (a saved config) or an inline `config`, an optional `name` label and
`keep`. It checks, in order: your session is unlocked, your account is enabled, your concurrent limit,
the global cap, your keep allowance (if `keep`), then validates the config against your **current**
capabilities (a capability withdrawn since you saved the config is enforced now). It returns at once
with status `creating`; a background job deploys. Poll `get_instance` - creating takes several minutes,
longer when `trusted_issuers` names an entity without a JWKS endpoint (see `trust`). It ends in
`running` (with `urls`) or `failed` (with `error`). Do not tell the user it is ready before `running`.

## stop and start

- `stop_instance` (from `running` or `failed`): stops every container. Apps, data, keys and URLs stay;
  only storage is billed. The URLs then answer with the shared edge's 503 page. The expiry clock keeps
  running and the instance still counts against your concurrent limit.
- `start_instance` (from `stopped` or `failed`): starts it and waits until every container reports
  healthy (up to 15 minutes), then makes it routable again. Status becomes `running`, or `failed` if
  something never turned healthy.
- An environment is stopped and started **as a whole**, never one component: an internal component
  that is stopped would never be woken by internal traffic.

## reset

`reset_instance` (from `running`, `stopped` or `failed`; destructive, needs approval) erases the
instance's data and brings it back up empty:
- lost: everything in the instance's MongoDB - wallet accounts and their passkeys, every stored
  credential, and any change made to the issuer's datastore;
- kept: the instance id, every URL, the signing keys and certificates, the admin token, the config;
- afterwards the issuer and verifier are registered with the wallet backend again and the issuer
  re-imports the synthetic documents from the fixtures, so issuance works again.

Status is `resetting` until it finishes, then `running` (or `failed`). Because passkeys are lost, users
must sign up in the wallet again; a native app's stale passkey for that RP ID will no longer work.
A reset also fixes "no documents" after the fixture set changed (the issuer imports documents only
into an empty datastore).

## keep

`set_keep` with `keep: true` removes the expiry (needs a keep allowance: `max_kept` > 0, not past
`kept_until`, and fewer kept live instances than `max_kept`). `keep: false` puts it back on the normal
clock: it then expires `ttl_days` from that moment. If your keep allowance lapses or your account is
disabled, kept instances go back on the clock with one day's grace, never deleted at once.

## destroy

`destroy_instance` (any status; destructive, needs approval) deletes every Fly resource of the instance,
its volume (all data) and its stored state. Status becomes `destroyed` and it disappears from
`list_instances`. If part of the teardown fails, the status is `failed` with the reason and the reaper
retries later. The reaper destroys expired, unkept instances the same way.

## Changing the configuration of an instance (reconfigure)

An instance is built from its config; editing a *saved* config afterwards changes nothing in running
instances. To change a running one use `reconfigure_instance` with `id` and either a new inline `config`
or a `config_name`:
- the new config is validated against your **current** capabilities first (all problems at once);
- allowed from `running` or `failed`; a `stopped` instance is refused with "start it first" (applying a
  config redeploys, which would silently start and bill it);
- data, URLs, keys and certificates are **kept**; the changed components restart (on the single-machine
  layout the whole machine restarts, so expect a short outage). Status is `reconfiguring`, then `running`;
- if the redeploy fails the instance ends `failed`, the error says the **previous config is still in
  place**, and re-applying `get_instance_config`'s output is the way back without losing data;
- the assistant asks the user to **approve** it in the console, because it restarts things.
Read the current config with `get_instance_config`; change only what was asked and keep the rest.
Trust lists, `credential_registries`, `wallet_attestation`, `dc_api_enable`, `android_apps` and `images`
are all applied this way. Only if reconfigure is refused or unavailable: `validate_config`, `save_config`,
`create_instance`, then `destroy_instance` the old one once the new one is `running` - that gives a **new
id and new URLs**, so passkeys and anything a partner trusted must be updated.

## Looking at an environment's health

`get_instance_health` returns each component's state and whether it is healthy (names and states only,
nothing secret); it needs no unlock and never fails on a Fly hiccup (it returns an `error` instead).
`get_instance_activity` lists what was done to the environment and by whom (you, system, admin).

## Data that survives what

| operation | data | URLs | keys/certificates |
|---|---|---|---|
| stop / start | kept | kept | kept |
| reset | erased | kept | kept |
| reconfigure | kept | kept | kept |
| destroy / expiry | erased | gone | gone |
| new instance from same config | separate, empty | new | new |

Sources: `sirosid_service/service.py`, `sirosid_service/mcp.py`, `sirosid_service/chat.py`, `sirosid_core/lifecycle.py`, `sirosid_core/deploy.py`, `sirosid_core/singlemachine.py`, `scripts/bootstrap.py`, `CLAUDE.md`
