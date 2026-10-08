---
title: "Limits, quotas, capabilities and policy"
summary: "Per-account and platform limits, the TTL and keep allowance, the two capabilities, what an admin can change, and the assistant's own limits."
digest: "Defaults: 2 live instances per account (stopped and failed count), 10 platform-wide, 3-day TTL, keep only with an allowance. custom_images and raw_values are granted by an admin, never by a config. The assistant has a daily token budget and needs Approve for reset, destroy, delete_config."
order: 140
tags: [limits, quotas, ttl, keep, capabilities, admin, policy, assistant, budget]
---
## Account limits

`get_account` returns your `role`, `capabilities`, whether your key session is `unlocked`, and `limits`:
`max_concurrent`, `max_kept`, `kept_until` and the platform `ttl_days`.

| limit | default | notes |
|---|---|---|
| concurrent instances per account (`max_concurrent`) | 2 | counts every live status: creating, running, stopped, resetting, failed. Stopping does not free a slot; destroying does |
| instances platform-wide | 10 | "the service is at capacity; try again later" |
| TTL (`ttl_days`) | 3 days | from creation, or from releasing a keep |
| kept instances (`max_kept`) | set per account by the admin (0 unless granted) | a kept instance has no expiry |
| keep allowance end (`kept_until`) | optional, set by the admin | after it, kept instances go back on the clock with 1 day's grace |
| saved configs | 50 per account | config names at most 64 characters |
| list entries in a config | 50 (`trusted_*`, `credential_registries`), 20 `android_apps`, 10 `trusted_verifier_roots` | |

An admin may set different values for an account (at invite time or later). The platform operator sets
the global cap, the TTL and the layout.

## Lifetime enforcement

A periodic pass (every few minutes; 300 s on the hosted service) runs three jobs:
1. kept instances whose owner's allowance ended, or whose owner was disabled, lose `kept` and expire one
   day later (never deleted immediately);
2. the reaper destroys every live instance whose `expires_at` has passed, with all its data;
3. a sweeper destroys Fly apps that look like instances but belong to no live instance, after a grace
   period (an hour on the hosted service).

Disabling a user puts all their instances on a one-day clock.

## Capabilities

| capability | unlocks |
|---|---|
| `custom_images` | the `images` key (and the `custom-wallet-backend` template) |
| `raw_values` | the `values` key (raw chart values, reaching every setting) |

They are granted per account by an admin; nothing in a config can grant them. They are checked when a
config is saved and again when an instance is created, so a withdrawn grant takes effect immediately.

## Platform policy (not settable by users)

Region, Fly org, app and host naming, the layout (single machine behind the shared edge), scale-to-zero,
whether env-admin is deployed (it is not, on the hosted service), which channels exist, an optional
image-registry allow-list, and whether conformance is allowed (it is not). URLs in configs must be
public https; see `config-reference`.

## What an admin can do (not through the assistant or MCP)

Create and revoke invites (role, capabilities, `max_concurrent`, `max_kept`, keep period, an optional
email binding; valid up to 30 days), grant or change capabilities and limits, disable a user, list all
instances and destroy any instance. Admins cannot read users' configs, specs or instance secrets: those
are sealed under each user's own passkey-derived key. None of these are MCP tools.

## Assistant limits

- Daily token budget per user (300 000 by default) and for the whole service (3 000 000); resets at
  00:00 UTC.
- At most 8 model steps per turn, one running turn per user, bounded history and tool output (long tool
  results are truncated), messages up to 8000 characters.
- Destructive tools (`reset_instance`, `destroy_instance`, `delete_config`) pause until the user clicks
  Approve; an approval request older than 10 minutes is dropped. A declined action must not be retried.
- `get_instance_credentials` (the admin token) is never offered to the assistant.
- Conversations live in server memory only and end after an hour of silence or a restart.
- The user's messages and tool results are sent to a model provider through OpenRouter (requested with
  no data retention); never put secrets into the chat.

Sources: `sirosid_service/service.py`, `sirosid_service/config.py`, `sirosid_service/chat.py`, `sirosid_service/mcp.py`, `sirosid_service/__main__.py`, `sirosid_core/policy.py`, `deploy/control-plane/fly.toml`, `CLAUDE.md`
