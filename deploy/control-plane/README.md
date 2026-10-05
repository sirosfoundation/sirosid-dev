# Control plane deployment (Fly)

The sirosid-dev control plane (`sirosid_service`: console, API, MCP, and the
assistant where its code is present and `OPENROUTER_API_KEY` is set) runs as ONE Fly
app with ONE always-on machine. In production that app is **`sirosid-console` in org
`sirosfoundation`**, serving `console.sirosid.dev`, with the Tigris bucket
`sirosid-console-backup`. The instances it creates live in a different org,
**`sirosdev`** (`SIROSID_FLY_ORG`), and it acts there with a sirosdev org token. SQLite lives on a Fly volume. Litestream replicates it
continuously to a Tigris bucket.

| file | what it is |
|---|---|
| `Dockerfile` | python slim (pinned by digest), non-root `app` user, hash-locked requirements (`requirements.lock`), and `helm`, `flyctl` and `litestream` pinned by version and sha256 |
| `entrypoint.sh` | as root it only chowns `/data`, then drops to `app`. It writes the Litestream config, restores the DB if it is missing (fail-closed, see below), then runs `litestream replicate -exec "python3 -m sirosid_service serve"` |
| `litestream_config.py` | the Litestream config, generated from the environment. It reads exactly the secrets `fly storage create -a <app>` sets (`BUCKET_NAME`, `AWS_*`), plus `SIROSID_BACKUP_PATH` (key prefix, default `sirosid.db`). The keys are referenced as `${AWS_ACCESS_KEY_ID}` and `${AWS_SECRET_ACCESS_KEY}` and never written to the file |
| `sirosid-admin` | `python -m sirosid_service <cmd>` as the `app` user, for `fly ssh console` (which logs in as root) |
| `fly.toml` | the app: region `arn`, 1 GB volume at `/data`, `auto_stop_machines = "off"`, `min_machines_running = 1`, a `GET /healthz` check, port 8080, `force_https`, non-secret `[env]` |
| `../../.dockerignore` | an **allow-list** for the build context (the repo root). Generated secrets under `fixtures/` are excluded explicitly |
| `../../.github/workflows/deploy-control-plane.yml` | deploys by manual dispatch or on a `control-plane-v*` tag, using `flyctl deploy --remote-only` and an app-scoped deploy token |
| `../../.github/workflows/rotate-fly-org-token.yml` | rotates the service's org token monthly, using `scripts/rotate_fly_token.py` |

Tests: `tests/test_control_plane_deploy.py` and `tests/test_rotate_fly_token.py`. They
check that every COPY source exists and survives the dockerignore, and they run a real
`deploy_instance` (against the fake flyctl) from a tree holding only what the image
holds. They also cover the pins, fly.toml, Litestream config generation, every entrypoint
branch, the workflow SHA pins and every rotation failure mode.

**Why one machine, always on.** SQLite has one writer and Litestream has one
replicator. The TTL reaper, `lapse_kept()` and the orphan sweeper tick inside the
service process every `SIROSID_TICK_SECONDS`. If the machine scaled to zero they would
stop, and expired instances would keep billing. Never `fly scale count 2`.

## What the backup holds (keep it this way)

The replica is the database file, nothing else. It holds **plaintext metadata**: user
ids, names, emails, roles, capabilities, invite *hashes*, instance ids, owners, status,
expiry, public URLs, `naming`, the audit log and session-token *hashes*. It also holds
**sealed blobs**: saved configs, instance specs and config snapshots, and instance
secrets (admin token, PKI, Mongo password). Each blob is AES-256-GCM under its owner's
main key, which only that owner's passkey (WebAuthn PRF, unwrapped in the browser) can
produce. See `sirosid_service/db.py` and `vault.py`. The server never stores that key.

So a stolen bucket or a restored copy tells you who has which instances and when they
expire, and nothing the users put in them. **Never add a plaintext secret column.** A
value generated once and handed to Fly belongs in the sealed `state` table
(`sirosid_core.state.STATE_FILES`), never in a plaintext one. After a restore, every
user is locked: the API answers 423 until they sign in with their passkey again.

## Secrets

| secret | set by | notes |
|---|---|---|
| `FLY_API_TOKEN` | you, then the rotation workflow | an **org token for `sirosdev`** (`fly tokens create org -o sirosdev -n sirosid-console -x 1440h`). The service acts as it for every instance |
| `BUCKET_NAME`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `fly storage create -a <app>` | Tigris. Another S3-compatible store works when you set the same five names. The key prefix is `SIROSID_BACKUP_PATH` in `[env]` (default `sirosid.db`) |
| `OPENROUTER_API_KEY` | you, optional | enables the assistant (only on builds that contain it) |

GitHub (environment secrets, not repo secrets):

| secret | environment | what |
|---|---|---|
| `FLY_DEPLOY_TOKEN_CONTROL_PLANE` | `control-plane` and `control-plane-rotation` | `fly tokens create deploy -a sirosid-console -x 4320h`: this app only. It deploys, and the rotation uses it to set the secret, since the app is in sirosfoundation |
| `FLY_ROTATOR_TOKEN` | `control-plane-rotation` | the bot user's token (see "Token rotation") |

Set secrets by stdin, never on a command line, so they stay out of shell history and `ps`:

```bash
read -rs TOKEN && printf 'FLY_API_TOKEN=%s\n' "$TOKEN" | fly secrets import -a sirosid-console --stage; unset TOKEN
```

## Bootstrap runbook (once)

```bash
APP=sirosid-console APP_ORG=sirosfoundation INSTANCE_ORG=sirosdev
fly apps create $APP -o $APP_ORG
fly volumes create sirosid_data -a $APP -r arn -s 1 -y
fly storage create -n $APP-backup -a $APP -o $APP_ORG -y   # Tigris; stages BUCKET_NAME + AWS_* secrets
#   (production: already done - bucket sirosid-console-backup, secrets staged on the app)
# the INSTANCE org's token, from stdin (see "Secrets")
fly tokens create org -o $INSTANCE_ORG -n sirosid-console -x 1440h   # copy it, then:
read -rs TOKEN && printf 'FLY_API_TOKEN=%s\n' "$TOKEN" | fly secrets import -a $APP --stage; unset TOKEN

# FIRST deploy only: SIROSID_FIRST_BOOT=1 allows starting with an empty database when the
# bucket has no replica yet. Later deploys (CI) do not pass it.
fly deploy . -a $APP -c deploy/control-plane/fly.toml \
    --dockerfile deploy/control-plane/Dockerfile --remote-only --ha=false -e SIROSID_FIRST_BOOT=1
curl -fsS https://$APP.fly.dev/healthz                      # {"ok":true}

# the FIRST admin: a single-use invite, valid 1 day, refused once an admin exists
fly ssh console -a $APP -C "sirosid-admin admin-invite"
# open the console, "Have an invite?", paste it, enrol a passkey that supports PRF
```

Deploy tokens for CI: `fly tokens create deploy -a $APP -x 4320h` goes into the
`control-plane` environment as `FLY_DEPLOY_TOKEN_CONTROL_PLANE`. Configure the
environment's protection: required reviewers, and deployment branches and tags limited
to `main` and `control-plane-v*`. Then deploy with a tag:
`git tag control-plane-v0.1.0 && git push origin control-plane-v0.1.0`.

### Custom domain (documented, not yet done)

The console is meant to be `https://console.sirosid.dev` with RP ID `sirosid.dev`. That is
`fly.toml`'s `[env]`. Until DNS exists, deploy with
`-e SIROSID_ORIGINS=https://$APP.fly.dev -e SIROSID_RP_ID=$APP.fly.dev`. A `fly.dev`
host is its own registrable domain, so passkeys work there. Note that passkeys are bound
to the RP ID: accounts enrolled under one RP ID cannot sign in under another.

```bash
fly certs add console.sirosid.dev -a $APP
fly ips list -a $APP          # the dedicated/shared v4 and the v6
# DNS at the sirosid.dev zone:
#   console  CNAME  sirosid-console.fly.dev.      (or A/AAAA to the IPs above)
#   _acme-challenge.console  CNAME  <value shown by `fly certs show console.sirosid.dev -a $APP`>
fly certs show console.sirosid.dev -a $APP              # wait for "Issued"
```

Rules that come with the RP ID: no instance content may ever be served under any
subdomain of `sirosid.dev`. Instances use `{app}.fly.dev` (`SIROSID_HOST_PATTERN`). And
neither the apex, `www`, nor `console` may serve `/.well-known/webauthn`. Chrome's
Related Origin Requests let any origin listed there use that host as its RP ID. The
console serves no such path (a test in `tests/test_service_entry.py` pins this). The apex
and edge configuration must return 404 for it too.

## Restore drill (do it after setup, then every quarter)

The entrypoint restores only when `/data/sirosid.db` is missing, and it is fail-closed.
If a replica is configured but cannot be restored, the machine refuses to boot. It never
starts empty, because an empty database would issue a fresh bootstrap invite and orphan
every instance (the sweeper would then destroy them after its grace period).
`SIROSID_FIRST_BOOT=1` lets it start empty only when the bucket holds no replica at all.

```bash
# 0. what the replica holds
fly ssh console -a $APP -C "litestream ltx -config /tmp/litestream.yml /data/sirosid.db"
# 1. lose the machine and its volume
fly machines list -a $APP                                  # note the id
fly machine destroy <id> -a $APP --force
fly volumes list -a $APP && fly volumes destroy <vol> -a $APP -y
# 2. bring it back WITHOUT SIROSID_FIRST_BOOT: it restores before serving
fly volumes create sirosid_data -a $APP -r arn -s 1 -y
fly deploy . -a $APP -c deploy/control-plane/fly.toml --dockerfile deploy/control-plane/Dockerfile --remote-only --ha=false
fly logs -a $APP --no-tail | grep entrypoint               # "restoring it from the replica" / "restored"
# 3. check: users and instances are back; users must sign in with their passkey again (423 until then)
```

If the boot fails with `litestream restore failed`, fix the bucket settings. Do NOT
reach for `SIROSID_FIRST_BOOT=1` unless you have decided to start from nothing.

## Token rotation

**Facts that shape it (verified on Fly):** an org token cannot mint org tokens or app
deploy tokens. So the service cannot rotate its own credential, and the rotator must be a
**user** token. `flyctl tokens list` has no `--json` and keeps listing revoked tokens
(with a `REVOKED AT` column), so the script parses the table by its header.

Setup (once):

1. Create a bot Fly user (a dedicated mailbox, e.g. `fly-rotator@siros.org`). Make it a
   member of the `sirosdev` org **only**: the token can reach every org its user belongs
   to. Do not reuse a person's account.
2. Log in as the bot (`fly auth login`), then `fly auth token`. Store the token as
   `FLY_ROTATOR_TOKEN` in the GitHub environment `control-plane-rotation`, next to
   `FLY_DEPLOY_TOKEN_CONTROL_PLANE`. The rotation reads that one as `FLY_APP_TOKEN` and
   sets the secret with it. The bot cannot do that itself, because it is not in
   sirosfoundation. Restrict that
   environment's deployment branches to `main`. Add no required reviewers, since a
   scheduled run cannot wait for one.
3. Name the service's org token `sirosid-console` (the bootstrap command above
   does). Only tokens with that name are ever revoked.

Each run (`scripts/rotate_fly_token.py`, monthly, 60-day expiry so one missed run is not
an outage):

1. List the unrevoked org tokens named `sirosid-console`.
2. Mint a new one. If that fails, nothing has changed.
3. Stage it with `fly secrets import` (stdin) and run `fly secrets deploy`. If that
   fails, the new token is revoked again and the old ones are kept.
4. Wait for 3 consecutive `{"ok":true}` from `/healthz`. If they do not come, nothing is
   revoked and the job fails (exit 3): investigate, both tokens are valid.
5. Revoke exactly the ids listed in step 1, never the new one.

No token is ever printed: all output, including flyctl's own error text, is redacted.
`workflow_dispatch` defaults to `--dry-run`.

When to rotate by hand:

- **Suspected leak of `FLY_API_TOKEN`:** run the workflow with `dry_run: false` now. If
  GitHub is unavailable, run the same script locally:
  `FLY_ROTATOR_TOKEN=... FLY_APP_TOKEN=... python3 scripts/rotate_fly_token.py --org sirosdev --app sirosid-console --health-url https://console.sirosid.dev/healthz`.
  Then check `fly tokens list -o sirosdev -s org`.
- **Leak of `FLY_ROTATOR_TOKEN`:** as the bot, revoke that token in the Fly dashboard
  (Account, Access Tokens). Then log in again, run `fly auth token` and update the
  environment secret. (Whether `fly auth logout` alone revokes it server-side has not
  been verified, so do not rely on it.) Then rotate `FLY_API_TOKEN` as above, because a
  rotator token can mint org tokens.
- **Leak of `FLY_DEPLOY_TOKEN_CONTROL_PLANE`:** `fly tokens list -a $APP`, then
  `fly tokens revoke <id>`, mint a new one and update the secret. It can deploy only this app.
- **Tigris keys:** issue new access keys for the bucket in the Tigris console
  (`fly storage dashboard <bucket>`), `fly secrets import` the new `AWS_*` values
  (stdin), run `fly secrets deploy`, then delete the old key in Tigris. This path is not
  scripted and has not been exercised.

## Verified on real Fly (2026-10-05)

The run used a throwaway app `sbx-cp-*` in `sirosdev`, with `SIROSID_ORIGINS`/`RP_ID` set to
its `fly.dev` host, a throwaway Tigris bucket, `SIROSID_TICK_SECONDS=30` and
`SIROSID_SWEEP_GRACE_SECONDS=300`. Headless Chrome drove the real console with a virtual
PRF authenticator. Everything below passed:

- remote build and first deploy in about 1m40s; build context 1.3 MB
- `admin-invite` over `fly ssh console`, then enrolment and unlock
- a sealed config: 31 opaque bytes in the database
- a REAL apps-layout instance: 10 apps, running in about 160 s; its wallet-frontend
  answered 200
- stop (16 s), start (111 s), reset (199 s; the Mongo volume was replaced) and destroy
  (37 s; no apps left)
- reaper: an instance whose `expires_at` was edited in SQLite was destroyed with its
  volumes about 60 s later
- sweeper: an orphan `sbx-i-*-<8>-wallet-backend` app was still present at 330 s and gone
  at 350 s (grace 300 s)
- Litestream: LTX files appeared in the bucket. After the machine AND the volume were
  destroyed, a redeploy restored the database in about 70 s (users, configs and instance
  rows identical). The old session then got 423 for the config until a passkey sign-in,
  which returned it
- the rotation script against real flyctl: its dry run parsed the real token table.
  With the org token as the "rotator" it failed safe at minting (`Not authorized to access
  createlimitedaccesstoken`) and changed nothing

Not exercised: the GitHub workflows themselves, a rotation with a real bot user, the
custom domain, the single-machine layout on Fly from the deployed service, and the
assistant.
