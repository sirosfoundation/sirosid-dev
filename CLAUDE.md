# sirosid-dev — instructions for Claude Code

This repo is a **harness, not a service**: it orchestrates sibling repos
(go-wallet-backend, go-trust, vc, wallet-frontend, wallet-common)
via a large self-documenting `Makefile`, docker-compose files, and Python
scripts under `scripts/`. The only code of its own that runs inside an
environment is `env-admin/` (the storage-reset service behind the
dashboard's "Clear all data", ~one file) and the `mocks/`; everything else
is orchestration. Run `make help` for the authoritative, current list of
targets/flags — this file only covers what `make help` and `README.md`
don't: the non-obvious traps.

Three entry points share one core and must stay in step:
- `make` — the CLI, and what CI runs.
- the **boot manager** (installed by `make setup`, launched by `make boot`, `bootmgr/`) — a
  Textual TUI that only ever runs `make` commands it shows first.
- `scripts/stack.py` — the `make up` option matrix as data (`make plan`).
  The Makefile still builds `COMPOSE_FILES` itself; `tests/test_stack_parity.py`
  fails when the two disagree, so **adding a compose overlay or flag means
  editing both** (and its help string in `stack.py`'s `OPTIONS`, which is
  where the TUI's per-field help comes from).

## `sirosid_core/` — the part that is not a command line

`scripts/` is the CLI (argv, the developer's gitignored files, printing).
`sirosid_core/` is what a CLI **and** a hosted service can both call, and it
never reads argv, the environment or a developer's disk:

- `spec.py` — `InstanceSpec`, a content-only description of one instance
  (PEM text and key material, not file paths). Strict `from_dict`: an unknown
  key is an error. `fly-up.py` builds one in `_spec_from_args()`, which is where
  every file read and the file-vs-CLI merge now live.
- `naming.py` — `Naming`: Fly app names, `.internal` addresses, **public
  hostnames** and the network, derived in one place. Default is the historical
  `sirosid-<env>-<component>` / `*.fly.dev`; `--app-prefix` / `--host-pattern`
  (or `app_prefix:` / `host_pattern:` in `environments/<name>.yaml`) change it.
  Never write `f"sirosid-{env}-..."` again — take a `Naming`.

- `components.py` / `assets.py` / `android.py` — the component registry
  (`build_components(mini_oidc_image, env_admin_image)`: the two pins from
  `values-fly.yaml` are parameters, the library reads no repo file) and the pure
  generators (fly.toml, nginx configs, dashboard, assetlinks). `fly_common.py`
  re-exports them.
- `fly.py` — `FlyClient(org, token, runner, ...)`: the flyctl operations as an
  object (any org, any identity, injectable runner, raises `FlyError`).
  `fly_common.py` keeps the old function names as a facade over one default
  client and turns `FlyError` into `SystemExit`.
- `state.py` — which files of an instance's working directory are **state**
  (`mongoRootPassword`, `jwtSecret`, `adminToken`, `apiAuthKey.pem`, `vc-pki/`)
  and `StateStore`/`workdir()` to carry them through a database. **If you add a
  value that is generated once and handed to Fly (which cannot give it back),
  add it to `STATE_FILES`** — `tests/test_state.py` redeploys from exported
  state alone and fails when something is missing.
- `tests/test_core_layering.py` keeps the package a library: no imports from
  `scripts/`, no `__file__`/`SIROSID_DEV_ROOT`, no `open()`.

- `lifecycle.py` — `destroy_instance(fly, naming, keep_data)`: what `fly-down`
  does, as a call that reports (`DestroyReport`) and keeps going past a failed
  app, so a TTL reaper never leaves the rest billing. `fly-down` takes
  `--app-prefix` for instances deployed with one.
- `resources.py` — `Resources(root)`: where the chart, `values-*.yaml` and
  `fixtures/` are. The CLI points it at the checkout; a service at what it
  ships. The library never walks up from `__file__` to find them.
- `render.py`, `vc_render.py`, `api_auth.py`, `helm.py` — the renderer
  (`render()` needs `resources=`). `scripts/render-helm-config.py` is the CLI over
  it; `scripts/{vc_render,api_auth,helm_render_lib}.py` are aliases so old
  imports resolve to the package module. `scripts/bootstrap.py` is **not** moved —
  the env-admin image copies it as a standalone file — so the library takes
  registration as an injected `register(...)` callable.
- `deploy.py` — `deploy_instance(spec, fly, naming, resources, ...)`: `fly-up`
  as a library call. Prints nothing (progress goes to a callback), exits nothing
  (raises `DeployError` with `.component` / `.deployed`), acts as the
  `FlyClient`'s org and token. `scripts/fly-up.py` is now argument parsing,
  `_spec_from_args()` and the summary.

To call the deploy from a service:
`state.workdir(store, id, subdir=f"fly-{env}")` -> `deploy_instance(spec, fly, naming,
Resources(root), rendered_root=<yielded dir>, register=..., progress=...)`.
`tests/test_deploy_instance.py` is the worked example.

## `sirosid_service/` — the control plane (no HTTP/MCP/auth layer yet)

Sits on top of `sirosid_core` (never the other way: a layering test enforces it) and
holds every rule about who may do what: users and single-use **invites** (admin-issued,
only a hash stored), per-user **quotas** and a global cap, saved **configs** validated by
`sirosid_core.policy` against the caller's *current* capabilities (re-checked at create
time, so a withdrawn grant bites), instance ownership (someone else's id and a
nonexistent one are indistinguishable), **keep** allowances, an audit log, a TTL
**reaper**, `lapse_kept()` (a lapsed allowance gets a day's grace, never instant
deletion) and an orphan **sweeper** (only apps matching exactly
`<prefix>-<8 id chars>-<known component>`, destroyed after a grace period). Instance
state lives in SQLite (`DbStateStore`, with `seal`/`unseal` hooks where encryption at
state lives in SQLite. **A user's data is sealed under a key only their passkey can
produce** (`vault.py`; the wallet's privatedata-spec model: one main AES-256-GCM key,
wrapped per passkey in the browser from the WebAuthn PRF output, stored by the server as
an opaque container). The browser unlocks it and hands the server the main key for the
session only (`ControlPlane.begin_session`; held in memory, never persisted, capped at
24 h). Configs, specs and instance secrets are sealed under it with AAD binding owner and
object; ids, owners, status, expiry and `naming` stay plaintext, so stop, start, destroy,
the reaper and the sweeper work with nobody logged in, while deploy, reset, reading a
config or an instance's credentials raise `Locked` without a session. Admins cannot read
users' data and a database dump or backup holds none. Without `cryptography` (see
`sirosid_service/requirements.txt`) the service tests skip. with only the service's own org credential
(stop, destroy the Mongo machine and volume, redeploy) so service instances need no
in-instance Fly credential: the platform sets `PlatformPolicy.env_admin=False`, which
`InstanceSpec.env_admin` (also `fly-up --no-env-admin`) turns into no env-admin app, no
nginx `/_admin/` + health proxy, no dashboard Storage card and no per-consumer token
minting. (Beware when editing `wallet_frontend_conf`: it once had a local named
`env_admin` holding the *hostname*, which silently shadowed the boolean parameter.)
Front ends authenticate a caller into a `Principal` and
call `ControlPlane`; they must not reimplement any check. `tests/test_service.py` runs
all of it against the fake flyctl.

### Front end of the control plane: `auth.py`, `api.py`, `__main__.py`

- `auth.py` — passkey enrolment (invite + ceremony; the invite is only spent if the
  ceremony succeeds), discoverable-credential login, web sessions (tokens stored hashed),
  and `unlock` (the browser derives the main key from the passkey's PRF output
  client-side and hands the server the key; the PRF output never reaches the server).
  One constant PRF salt is advertised for every credential. Every verification failure
  returns the same message. The server cannot verify PRF support (the client reports it
  unsigned): the browser must refuse to enrol an authenticator without it.
- `api.py` — a thin Starlette skin over `ControlPlane`; **it decides nothing**, so a rule
  added there would be one MCP and the CLI lack. It owns only the web's own risks:
  `__Host-sid` cookie (HttpOnly, SameSite=Strict, no Domain), an Origin allow-list on
  every state-changing request (a cookie alone never authorises a write), JSON-only
  size-capped bodies, per-client rate limits on the endpoints that take guesses, strict
  security headers and no caching. Status mapping: 401 not signed in, 423 locked, 422
  policy problems (all at once), 409 quota/invalid state, 404 for another user's or a
  nonexistent id and for admin routes a member calls.
- `python -m sirosid_service serve | admin-invite`; configuration from the environment
  (`config.py`: `FLY_API_TOKEN` is required, origins must be https and under the RP ID, the
  host pattern must vary per instance). The first admin gets a **bootstrap invite** from
  `admin-invite` (refused once an admin exists) and enrols a passkey like anyone else.
- RP ID is the apex **`sirosid.dev`** and is permanent. **Hard rule: nothing untrusted, and
  in particular no instance content, may ever be served from any subdomain of
  `sirosid.dev`** - any subdomain page may request passkeys scoped to the apex.
- `console/` — the web UI, served by the service itself (`ApiConfig.console_dir`, env
  `SIROSID_CONSOLE_DIR`; only `index.html`, `js/`, `css/` from a table built at startup, so
  `console/test/` is never reachable). Plain ES modules, **no build step and no dependencies**.
  Pages get their own CSP (`script-src 'self'`, no inline script or style, no handlers);
  `tests/test_console_static.py` fails on inline code, `innerHTML`/`eval`, or web storage.
  `js/container.js` is the key container (the privatedata-spec key layer, WebCrypto only;
  our own HKDF info string so a PRF output is never confusable with the wallet's);
  `js/webauthn.js` converts options and **strips the PRF output from everything sent to the
  server**; `js/app.js` builds the DOM with `createElement`/`textContent` only. Enrolment refuses
  an authenticator without `prf.enabled` before finish, then takes a second touch for the PRF
  output. Adding a passkey stores the new container *before* registering the credential, so a
  failure never leaves a passkey that cannot open it. The main key lives in page memory only: a
  reload keeps the server session unlocked but needs a new sign-in to add/remove passkeys.
  `tests/test_console_js.py` runs the Node tests (`node --test console/test/*.test.mjs`) and a
  Python cross-implementation check of the container format; run the service tests with `.venv`.
- `oauth.py`, `mcp.py`, `mcp_web.py` — the MCP server (`POST /mcp`, JSON-RPC, JSON responses only,
  no sessions, no batching) and the minimal OAuth 2.1 AS it needs (dynamic registration of
  PUBLIC clients, code + mandatory S256 PKCE, no refresh tokens, no secrets). The only login is
  the console's passkey login: `/oauth/authorize` parks the request and redirects to
  `/#authorize=<id>`, the console shows consent, and **approving clones the user's key session**
  (`SessionKeys.clone`): the application gets a bearer token for its own handle on the
  server-held key - never the key - which dies on its 8 h clock, when revoked in the console's
  "Connected apps" tab, when the user is disabled, or on restart (codes, tokens and keys are
  memory-only; only registered clients are stored). So **token validity == key-session
  validity**, and console sign-out does not kill an agent. Tools are one `ControlPlane` call
  each as the token's Principal - decide nothing there; administrative actions, passkeys and the
  key container are deliberately not tools. `/mcp` refuses a browser `Origin` that is not ours
  (DNS rebinding). `tests/test_mcp.py` covers the flow and is mutation-checked (PKCE, redirect,
  origin, replay and key-session revocation each fail a test when removed).
- `llm.py`, `chat.py`, `chat_web.py` — the **assistant** (console "Assistant" tab), enabled only when
  `SIROSID_CHAT_MODELS` (comma list, first is default) and `OPENROUTER_API_KEY` are set; per-user and
  global daily token budgets (`SIROSID_CHAT_USER_DAILY_TOKENS`/`..._GLOBAL_...`, table `chat_usage`).
  It is the MCP tool table driven by a model, acting only as the unlocked signed-in user, so it can
  do nothing the console cannot. What is special: a model provider sees the user's messages and tool
  results (requests ask OpenRouter for `data_collection: deny`; the UI says so); the credentials
  tool is **not offered** and admin actions are not tools; destructive tools (`destructiveHint`:
  destroy, reset, delete config) **pause for an Approve click** bound to the exact tool call id
  (`confirm` is single-use, per user); tool output is data (system prompt says so) and the console
  renders everything as text. One running turn per user, step limit, bounded history/tool output.
  Conversations are server memory only. The turn streams SSE (`status/tool/tool_result/confirm/
  message/error/done`). `tests/test_chat.py` uses a scripted fake model against the real tools and
  HTTP layer; the approval gate, withheld tool, call binding, budget and unlock checks are
  mutation-checked.
- `tests/softauthn.py` is a software authenticator (real authenticator data, COSE keys,
  signed assertions) so passkey verification is exercised, not mocked.

## Sibling repo layout

`make setup` clones these into `../`:

| repo | branch | notes |
|---|---|---|
| `wallet-frontend` | `release/sirosid` | |
| `wallet-common` | `release/sirosid` | shared TS types |
| `go-wallet-backend` | `main` | |
| `go-trust` | `main` | trust PDP |
| `vc` | `main` | SUNET/vc issuer/verifier/apigw/registry — only needed for `VC=yes` |
| `facetec-api` | `main` | only needed for `FACETEC=yes` |

`make update` force-hard-resets every repo above to its
default branch — destructive, only run it when you actually want to discard
local changes in the sibling checkouts.

## Two deployment targets — when to use which

- **Local docker-compose (`make up ...`)** — fast iteration, builds from local
  source in the sibling repos (or `REBUILD=yes` to force a no-cache rebuild).
  Use this for day-to-day development and for anything `sirosid-tests` runs
  against `localhost`.
- **Named Fly.io environments (`make fly-up ENV=<name>` / `fly-down` /
  `fly-status`)** — a full, independently-addressable, shareable stack at
  `sirosid-<env>-*.fly.dev` URLs under the `sirosfoundation` Fly org, region
  `arn`. Images are pulled straight from `chart/values.yaml` (layered with `values-fly.yaml`) — **no local Docker build** by
  default. Use this for: handing a URL to someone else, native
  Android/iOS app testing (real assetlinks/AASA over real TLS), OIDC-backed
  issuance (mini-oidc needs to be reachable from a real browser redirect, not
  just a container network), or running several isolated environments at once
  (`ENV=alice`, `ENV=bob`, ... — fully isolated, verified with concurrent
  deploys, each env gets its own Fly `--network` segment so apps in one
  environment can't resolve another's `.internal` addresses).

Both paths share one underlying mechanism: `scripts/render-helm-config.py`
renders **every** service's config — wallet-backend, the PDP, and the four vc
services (issuer-apigw, issuer-core, issuer-registry, verifier) — from the
same in-repo chart (`chart/`), just with different hostname targets (`--target
fly` uses `.internal`/`.fly.dev`, compose uses `*.localhost` service
aliases). Nothing is hand-maintained in parallel any more:
`fixtures/vc-config.yaml` and the four mechanically-patched copies of it are
gone, along with `generate-tunnel-config.py`, `generate-android-config.py`
and `patch-vc-config-fly.py`.

Values layering, later wins (`values-base.yaml` → `values-dev/fly.yaml` →
per-run generated overlay → `environments/<name>.yaml`'s `values:` block).
Every mode that used to need its own pre-patched config file is now just a
different hostname set: TUNNELS, DOMAIN, Android/Waydroid, CONFORMANCE.

**`chart/` is ours.** It began as the SIROS ID Stack Helm chart
(sirosfoundation/siros-id-stack, at our PR #3 + `mdoc_iacas_uri` + issuer BBS)
and is now maintained here, deliberately more generic than the production
chart: it exists to expose every knob sirosid-dev needs, where the production
chart exposes only what a deployment wants. Helm is used purely as the template
engine (`helm template`; only the ConfigMaps are consumed), so the k8s-only
templates in it are dead weight to prune, not something to keep in sync. The
upstream chart reverted the `extraConfig` design this repo relies on
(2026-09-29) and silently ignores unknown values keys, so tracking it would
have produced stacks that boot with the wrong config. Compare against
upstream by hand when a feature there is worth porting — don't merge it.

**`make vc-config-parity`** renders the chart for both targets and
semantically diffs the result against checked-in goldens
(`fixtures/vc-config-golden/`), with every accepted difference justified in
`accepted-diffs.yaml`. Run it after touching the chart, `values-base.yaml`,
or the renderer — it is the only thing that catches a template edit quietly
dropping a field this repo depends on.

### `make up` — key flags (see `make help` for the full, current list)

- `PDP=allow|whitelist|deny|mock|helm` — trust policy provider. `helm` renders
  wallet-backend + PDP config from the chart (see above); the other modes are
  hand-maintained env vars/CLI flags, kept independently and known to drift
  from the chart (that's the whole reason `PDP=helm` exists — see
  "Helm alignment" below). Note `VC=yes` renders the vc services' config from
  the chart regardless of `PDP=`, so `helm` is now only about wallet-backend
  and the PDP themselves.
- `ENV=<name>` — layers `environments/<name>.yaml` onto a local `make up` too,
  not just `fly-up`. Its `values:` block is free-form chart values merged last,
  so a one-off override needs no code change anywhere. Its `local:` block
  supplies defaults for every `make up` option (`pdp`, `vc`, `transport`, ...
  — `scripts/stack.py`'s `OPTIONS`), applied by the Makefile as plain
  assignments so a flag on the command line still wins for that run.
- **Storage:** Mongo lives in its own overlay (`docker-compose.mongodb.yml`),
  loaded whenever the VC services are on **or** `PDP=helm` — the chart-rendered
  wallet-backend config points at `mongodb://mongodb`, and before the overlay
  `make up PDP=helm` without `VC=yes` pointed at a host that never started.
  Its volume `sirosid-mongodb-data` survives `make down` (`make clean` and
  `make storage-clear` remove it). wallet-backend in every non-helm mode is
  an **in-memory** store — a container restart empties it; `make plan` and the
  dashboard's Storage card both say so. Clearing while up goes through
  env-admin (stop consumers → drop databases → start → wait healthy →
  `scripts/bootstrap.py` re-registers issuer/verifier). The restart is not
  optional: wallet-backend creates its default tenant and vc-apigw imports
  its bootstrap documents **only at startup**, so a wipe without a restart
  leaves a stack that looks healthy and cannot issue.
- `AS_RULES=allow-all|baseline` — SPOCP policy for wallet-backend's built-in
  Authorization Server (passkey login + token endpoint), separate from the
  `PDP=` trust policy above. Default `allow-all` (`fixtures/as-rules/`) is an
  unconditional allow — local dev's job is a working AS for every client
  (web + native SDK) out of the box, not exercising the AS ruleset itself.
  `baseline` swaps in go-wallet-backend's own real policy (`rules/` in that
  repo) — the same one `make fly-up` gets by default, since it never sets
  `WALLET_AS_RULES_DIR` and go-wallet-backend's `EnableForRole()` falls back
  to its image-baked rules when unset. Use `baseline` only when directly
  testing AS rule behavior, e.g. reproducing a Fly-only 403 locally.
- `REGISTRY=vendored|external` — where credential type metadata comes from.
  `vendored` (default) renders each type's VCTM/MDDL from `fixtures/vc-metadata`
  into the chart's `vctms` ConfigMap. `external` drops the documents entirely
  and resolves each scope by identifier — `vct` for `dc+sd-jwt`, `doctype` for
  `mso_mdoc` — from `CREDENTIAL_REGISTRIES` (ordered, later overrides earlier).
  Honored identically by `make up` and `make fly-up`; persistable per
  environment as `credential_registries:` in `environments/<name>.yaml`.
- `VC=yes` — adds production-like issuer/verifier/apigw/registry + mongodb,
  built from `../vc`. Requires `helm`, since their
  config is rendered from `chart/`.
- `TRANSPORT=websocket|wmp|http` — wallet transport; `http` is deprecated.
- `CONFORMANCE=yes` — layers in VC services + VC↔go-trust wiring
  (`docker-compose.vc-go-trust.yml`) and the OpenID conformance suite overlay,
  on top of whatever `PDP=` you set (default stays `allow` since that's the
  Makefile default, not something CONFORMANCE forces).
- `R2PS=yes` — adds go-r2ps-service + SoftHSM2 (remote WSCD/WSCA signing).
- `DOMAIN=<host>` / `TUNNELS=yes` — mutually exclusive; both exist for mobile
  device testing (custom LAN hostname vs. real-TLS Cloudflare quick tunnels).
- `GOLDEN=yes|<release>` — pre-built images from `siros-conformance`'s
  `golden-releases.yaml` instead of local builds (see below). VC services
  still build from source even with `GOLDEN=yes VC=yes` — `fixtures/vc-config.yaml`
  evolves between releases, so golden VC images aren't config-compatible yet
  (`docker-compose.golden-vc.yml` exists but is deliberately unused, per an
  explicit comment in the Makefile — don't "fix" this without checking
  config/image version alignment first).
- `ANDROID_APPS=pkg=fingerprint,...` — extra Android package/signing-key
  pairs to trust, on top of the gitignored `.android-apps` file (copy from
  `.android-apps.example`). Honored identically by `make up` (any PDP mode)
  **and** `make fly-up` — same underlying `scripts/android_apps.py`, one
  source of truth, no drift between local and Fly.
- `FACETEC=yes` — implies `VC=yes`; requires `FACETEC_SERVER_URL` exported in
  your shell (a live credential, never committed).

### `make fly-up ENV=<name>` — key flags

- `IMAGES=component=ref,...` — ad-hoc, per-environment image override (e.g.
  your own branch build). A bare unqualified tag (no `/`) that's already in
  your local Docker daemon (what `make up REBUILD=yes` produces) gets
  auto-pushed into that Fly app's own `registry.fly.io` namespace for you —
  no manual `docker tag`/`push`/`flyctl auth docker` needed. A fully-qualified
  ref (has a `/`) is always passed through untouched even if it's also
  cached locally.
- `REGION=<code>` — where this environment's machines go. **You usually don't
  need it**: with nothing pinned, `fly-up` asks Fly where to run and takes its
  answer — the anycast edge nearest you, the same thing `fly launch` picks.
  Contributors are in different places, so that's the right default. Note it's
  Fly's *routing* view, not raw geography: from Stockholm it answers `fra`,
  not `arn`.

  Pin it when you want to, most specific first: `REGION=` →
  `environments/<name>.yaml`'s `region:` → `$FLY_REGION` → a gitignored
  `.fly-region` (copy `.fly-region.example`). Only if Fly is unreachable does
  it fall back to `arn`. Every run prints which it used and why.

  A **named, shared** environment should pin `region:` in its own file, so it
  doesn't land somewhere different depending on who redeployed it; the
  personal default is for scratch environments (`ENV=alice`) that have no file.

  `primary_region` is a preference, not a constraint — it's where Fly places
  the *first* machine. Changing it does **not** move machines that already
  exist. Since Mongo got a volume, a volume also **pins** its app to its
  region: `fly_common.ensure_volume` refuses a run targeting another region
  while a volume exists, so relocating means clearing the data first
  (`make fly-storage-clear ENV=<name>`, or `fly-down` without `KEEP_DATA`).
- **Storage on Fly:** `mongodb` and `conformance-mongodb` mount a Fly volume
  (`fly_common.STORAGE_APPS`, created by `fly-up` if missing). Data survives
  redeploys, image bumps and host maintenance. `fly-down` destroys apps and
  therefore volumes — a teardown deletes the data — unless `KEEP_DATA=yes`,
  which leaves the two Mongo apps with machines stopped and keeps
  `fixtures/rendered/fly-<env>/` (it caches the root password, see the gotcha
  below). `env-admin` (11th app, deployed right before wallet-frontend) is the
  reset actor; it holds one app-scoped deploy token per Mongo consumer
  (minted every `fly-up`, never an org token) to stop/start them through the
  Machines API. Its image is `values-fly.yaml`'s `images.envAdmin`; if that
  pin isn't pullable (first release, or you're testing an env-admin change)
  `fly-up` builds `env-admin/Dockerfile` locally and pushes it into the app's
  registry.fly.io namespace — the one place `fly-up` builds anything.
- `ANDROID_APPS=` — same as local (see above).
- `CONFORMANCE=yes` — deploys 3 extra apps (`conformance-mongodb`,
  `conformance-server`, `conformance` nginx front) after the core 10.
- `TRUSTED_ISSUERS=url,...` — ad-hoc external issuers for interop testing
  (e.g. a third-party mdoc issuer). **Only ever added to `pid_issuers`, never
  to `verifiers`** — use `TRUSTED_VERIFIERS=identity,...` for that instead.
  See the PDP gotcha below before assuming either covers the other.
- `TRUSTED_VERIFIER_ROOTS=path,...` — PEM CA cert file path(s) to merge into
  PDP's system CA pool (go-trust#123+), for a verifier whose request-signing
  cert is issued by a self-signed "reader CA" root meant to be trusted
  out-of-band rather than a public CA. See the PDP gotcha below.

## Credential metadata: two components, one list

Two components independently need each type's metadata document, and when they
disagree **nothing errors** — the wallet just holds a credential it can never
present:

- **vc** — the issuer builds the credential from it; the verifier derives its
  DCQL presentation queries from it (`common.credential_registry`)
- **go-wallet-backend** — its registry serves it to the wallet, which renders
  and DCQL-matches against it (`registry.yaml`'s `sources`)

`REGISTRY=external` therefore sets both from one `--credential-registries`
value, in one render (`scripts/render-helm-config.py`). There is no second
place to keep in step — which there used to be, four of them, before the vc
services' config was rendered from the chart.

The identifiers each scope switches to are read out of
`values-base.yaml`'s `features.credentialTypes`, the same values the chart
already renders `supported_credentials` and the VCTM mounts from, so the
vendored and registry-resolved paths cannot drift apart while both exist.

Both paths use each registry's legacy `.well-known/vctm-registry.json` index,
**not** its TS11 `/api/v1/schemas.json` endpoint. TS11 lists only fully
TS11-compliant credentials — 26 entries on registry.siros.org today against 37
in the legacy index — so switching would silently drop every type not migrated
yet. That is the obvious "modernisation" to reach for; don't, until the two
agree.

### Synthetic documents: what `fixtures/vc-bootstrapping` guarantees

Every datastore-sourced type is issued from a document in
`fixtures/vc-bootstrapping/<scope>.json`, keyed by mini-oidc user id. Three
invariants hold, and `tests/test_fixture_integrity.py` is what holds them —
run it after touching anything under `fixtures/vc-bootstrapping` or
`fixtures/vc-metadata`. Each one guards a failure that is **silent**: the
stack issues the wrong credential, or refuses with a message that points at
the wrong thing.

- **The document's identity is the login identity.** PID-authenticated
  issuance resolves the holder by matching the presented PID's
  `given_name`/`family_name`/`birth_date` against `identity_mappings.json`.
  A document whose person drifted from mini-oidc's `users.yaml` fails with
  "no documents", which reads like a missing fixture rather than a
  mismatched one — it hid three broken natural persons until 2026-09-18.
  A mapping with no `birth_date` fails the same way for any type whose
  `authOptions.scopes` matches on `birthdate`.
- **Every claim is declared** by that type's VCTM/MDDL. Nothing errors on an
  undeclared claim; the issuer drops or rejects it depending on version.
- **Minimal + exactly one `-full`.** Each scope has documents covering only
  the mandatory claims, and exactly one whose `meta.document_id` ends in
  `-full`, covering mandatory *and* every optional claim, so both shapes of
  a credential are testable without editing fixtures. One holder owns the
  `-full` document per scope; two documents of one scope for one holder make
  issuance ambiguous. README.md has the per-scope table.

Derived age claims are recomputed from the birth date at test time, so they
fail the suite rather than rot. Adding or changing a document needs a
storage clear or a `make datastore-upload` on an existing environment — see
the datastore-import gotcha below.

## Why `values-fly.yaml` overrides exist (don't remove without checking)

`chart/values.yaml`'s image pins are a snapshot of what siros-id-stack shipped
and lag behind what this repo needs; `values-fly.yaml`'s `images:` block patches specific components:

- **`images.pdp`** — pinned to a specific `go-trust` tag ahead of the chart's
  own (pre-release, commit-sha) default, because the chart's default predates
  the `AllowHTTP` fix ([go-trust#112](https://github.com/sirosfoundation/go-trust/pull/112))
  the PDP's config-file whitelist needs to become healthy *at all* on Fly —
  without it, JWKS fetch fails for every whitelisted entity. Explicitly
  marked in the file as a stopgap to delete once the chart's own pin catches
  up — don't add more permanent special-casing here if you hit a similar
  lag elsewhere; fix it the same documented, delete-when-fixed way.
- **`images.walletBackend`** — pinned ahead of the chart's default, for
  Key Attestation / wallet-provider work this repo exercises ahead of the
  chart's own release cadence.
- **`images.issuerRegistry/issuerApigw/issuerCore/verifier`** (the `vc.*`
  services) — pinned ahead of the chart's default for the same reason
  (mdoc/BLE proximity, DC API, MDDLSchema fixes). Check the current pinned
  tag in `values-fly.yaml` before assuming a `fly-up` will pick up recent
  `vc` work — it deploys whatever tag is pinned, not `../vc`'s local HEAD,
  unless you pass `IMAGES=vc-apigw=...,...` explicitly.

None of these overrides are meant to be permanent — each one's comment names
the exact condition under which it should be deleted. If you're bumping one
of these, check whether the chart itself has since caught up first.

## Known-good end-to-end smoke test (validating an environment/image bump)

The following round trip is a useful reference recipe for confirming a fresh
named Fly environment — or a bump to the standard image pins above — actually
works end-to-end, beyond individual components' own health checks:

1. Point a real client at the environment's `wallet-proxy`/`wallet-frontend`
   public URL and sign up via passkey — e.g. siros-sdk-kotlin's `sample-app`
   (package `org.siros.sdk.sample`, a separate SDK test app, not
   `wallet-frontend` itself) has a runtime-configurable `backend_url` in its
   Settings, no rebuild needed to point it at a new environment.
2. Request and receive a real mDL credential via OpenID4VCI using
   OAuth-Client-Attestation — confirm in wallet-backend's own logs
   (`flyctl logs -a sirosid-<env>-wallet-backend`) for lines like `"using
   OAuth-Client-Attestation authentication (client-signed PoP)"` and
   `"Server-side issuer trust evaluation" ... "trusted":true`.
3. Present that mDL via Google's public `digital-credentials.dev` DC API
   conformance test site — a stricter, independent isomdoc-based verifier
   that catches COSE/mdoc conformance issues this stack's own verifier
   doesn't.
4. Present the same mDL via real BLE proximity (`siros-verifier-cli`'s
   `siros-verify read --mode peripheral`), confirming `deviceSignature
   VALID`.

All four succeeding together confirms issuance, DC API presentation, and BLE
proximity presentation all interoperate correctly against the currently
pinned images — worth re-running whenever bumping `images.walletBackend` or
the `images.issuer*`/`verifier` pins above, not just checking that each
component's own health check goes green.

## Golden release mechanism

`GOLDEN=<name>` fetches `golden-releases.yaml` from
`sirosfoundation/siros-conformance` (`fetch-golden-env` target), parses it
with the `GOLDEN_AWK` script embedded in the Makefile, and writes
`.env.golden` with resolved `ghcr.io/sirosfoundation/*` image refs consumed by
`docker-compose.golden*.yml`. `GOLDEN=yes` resolves to whatever
`golden-releases.yaml` names as its `default:`; anything else is treated as a
named release.

## Gotchas — symptom → likely cause → how to check

**A `values-fly.yaml` image pin looks unpublished/inaccessible (404, no
access) even though the release actually shipped:** GHCR image tags never
carry the `v` prefix that the corresponding git tag does — git tag
`v0.7.0-sirosid.0` publishes as image tag `0.7.0-sirosid.0`. Checking the
`v`-prefixed form first (the natural thing to try) will look like the image
doesn't exist. `docker manifest inspect
ghcr.io/sirosfoundation/<image>:<tag>` is the reliable way to confirm what's
actually published before pinning it; `gh api .../packages/.../versions`
needs a `read:packages` token scope a default `gh auth login` session may not
have.

**wallet-backend crash-loops with `Failed to load backend configuration`
after bumping `images.walletBackend` past go-wallet-backend v0.10.0, if
the chart's wallet-backend template sets `wallet_provider.wia.enabled:
true`:** v0.10.0 added a hard startup-time validation
(`pkg/config/config.go`'s `WIAConfig.WalletVersion` check) requiring
`wallet_provider.wia.wallet_version` whenever `wallet_provider.wia.mode`
defaults to `"etsi"` (EC TS03 v1.5.2 §2.3.1 made it a mandatory WIA claim,
with no sensible built-in default per that field's own comment) — `wia.
enabled: true` with no `wallet_version` alongside it fails config validation
and the backend never comes up. As things stand, `chart/`
doesn't render a `wallet_provider.wia` block in
`templates/04-wallet-backend.yaml` at all, so this isn't a live bug today —
but if a future chart update adds one, always pair `wia.enabled: true` with a
`wallet_version` (this repo uses go-wallet-backend's own version as the
value) in the same change.

**`scripts/render-helm-config.py --target fly --env <name> ...` run by
itself (not via `make fly-up`) without `--mongo-password <value>` silently
renders a Mongo connection URI with no credentials at all:** `mongo_password`
defaults to `None`, and `patch_wallet_backend_fly()` does `mongo_auth =
f"root:{mongo_password}@" if mongo_password else ""` — empty string, not an
error. The password is the one in `fixtures/rendered/fly-<env>/mongoRootPassword`
(`fly-up.py`'s `resolve_mongo_password()`): it is **no longer rotated per
run** — the Mongo volume's data was initialised with it and
`MONGO_INITDB_ROOT_*` never re-applies to a non-empty `/data/db`. If that
cache is missing but the environment has a volume (someone else deployed it
last), `fly-up` reads the password back from the running mongodb machine over
`fly ssh console` (a `--file-secret` is readable from inside; the API cannot
read secrets back) and refuses to continue if that fails, since deploying a
guessed password would lock every consumer out of data nobody can then
reach. For any single-field config tweak, just re-run `make fly-up ENV=<name>`
— idempotent, redeploys every component with mutually-consistent config and
secrets.

**A new datastore-sourced credential type (or new bootstrapping document)
issues fine on a fresh `make up` but fails with "no documents" on an
existing environment after a redeploy:** vc-apigw's importer only loads
`datastoreImport.documents` and the identity mappings into an **empty**
datastore - its log says `Datastore already contains data, skipping import`.
Redeploying does not add the new documents. Clear the environment's data
(dashboard Storage card, `make storage-clear`, `make fly-storage-clear
ENV=<name>`), which restarts apigw into an empty datastore so the import
runs with every document - or, when the environment holds data worth
keeping, add just the new documents without a wipe:

```bash
make datastore-sync DRY_RUN=yes [ENV=<name>]     # what would change
make datastore-sync [ENV=<name>]                 # make it match the fixtures
make datastore-search SCOPE=<scope> [ENV=<name>]
```

`datastore-sync` reconciles the whole of `fixtures/vc-bootstrapping` -
documents added, replaced and removed, plus `identity_mappings.json`, which
the same importer skips and whose drift fails issuance with "no documents",
pointing at the documents rather than at the mapping. `SCOPE=` narrows it to
one type (and then leaves the shared mappings alone). `make datastore-upload
FILE=...` still adds a single file's documents, and is the right thing when
the environment holds documents you deliberately don't want reconciled away.

One trap if calling the API directly: the bulk endpoint's body is a map keyed
by holder, so one call can only carry one document per holder - fine for a
bootstrapping file, one scope per file, and the reason `datastore-sync` sends
one call per scope.

The import gap bit the EU Business Wallet types (`ebw_oid`/`eucc`/`eu_poa`,
all PID-authenticated datastore types) on gdc, 2026-09-10; `iban_ov` was
added to gdc this way on 2026-09-12, and on 2026-09-18 a `datastore-sync`
brought gdc up to the reworked fixtures (12 documents added, 10 replaced, 7
renamed away, 3 identity mappings given the birth dates the PID match needs).

**Issuing to a wallet on another device (no browser on the phone, no OIDC
login there):** vc-apigw mints a pre-authorized credential offer for any
datastore document, and the offer carries its own code, so the wallet that
scans it needs no session of its own:

```bash
make datastore-offer SCOPE=pid_1_8 QR=yes [DOCUMENT_ID=<id>] [ENV=<name>]
```

It prints an `openid-credential-offer://` URL and, with `QR=yes`, the QR the
SDK sample app's scanner reads (it registers that scheme). With no
`DOCUMENT_ID` it takes the scope's only document, or its `-full` one. This
works for the PID-authenticated types too: the document is named here rather
than resolved from a presented PID, which is the whole point of the
pre-authorized grant. The code expires 5 minutes after minting, so generate
it when the phone is in your hand.

**The issuer and the verifier show the SUNET logo:** they are showing vc's
built-in assets, which means `common.branding` never reached them. The chart
points `logo_path`/`favicon_path` at `/branding-assets/*.png` and expects an
initContainer to decode them there from the `branding` ConfigMap; there is no
initContainer here, and vc validates a branding path as a real PNG at startup
even when it is empty (`panic: validation:image_png field:logo_path`), so the
whole block used to be stripped. `vc_render.write_branding_assets` now does
the initContainer's job at render time, writing
`fixtures/rendered/branding-assets/{logo,favicon}.png`, which compose mounts
and `fly-up` ships with `--file-local`. Check that directory exists before
looking anywhere else.

To use something other than the chart's default (the SIROS ball), name a PNG
in this repo - `features.branding.logoDataUrl: {file: fixtures/...}`, and the
same for `faviconDataUrl` - in `values-base.yaml` or in an environment's
`values:` block. The chart's own literal `data:image/png;base64,...` form
still works; the `{file: ...}` reference is resolved to exactly that before
the chart sees the values, the same way credential-type documents are.

**vc-apigw's `/api/v1/*` (datastore, identity mappings) answers 401 to a
plain request - or, before 2026-09-15, answered anything to anyone:** the
admin API takes a Bearer JWT. The chart renders `api_server.api_auth` (JWKS
at `/main-config/api_auth_jwks.json` plus the SPOCP rule granting
`/api/v1/*` to `admin@<tenant.id>`); `scripts/render-helm-config.py`
generates the EC key behind it per target (`fixtures/rendered-secrets/
apiAuthKey.pem` for compose, `fixtures/rendered/fly-<env>/apiAuthKey.pem`
for Fly, reused across renders like every other generated secret) and
`scripts/api_auth.py` mints 5-minute tokens from it - `make datastore-token`
for a raw one. Until 2026-09-15 `vc_render.strip_unrenderable` deleted the
block instead, on the grounds that nothing could mint the token, which left
the datastore API open on every environment's public URL (found on gdc
2026-09-12; nothing indicates it was used). Two consequences worth knowing:
a Fly environment deployed by someone else has its key in *their*
`fly-<env>/` directory, so your `make datastore-*` against it fails with 401
until you have that file (same story as `mongoRootPassword`); and the JWKS
is baked into the running config, so a key you regenerate only takes effect
after the next render + deploy.

**PDP boot appears stuck / "Issuer not trusted" right after `fly-up` with
`TRUSTED_ISSUERS=` set (or any PDP redeploy with it already set):**
`go-trust`'s `WhitelistRegistry.StartRefreshLoop` does a *synchronous*,
unbounded initial JWKS refresh for every whitelisted entity before the HTTP
listener even starts. An mdoc-only issuer with no JWKS endpoint burns the
full discovery-timeout budget — several minutes. Check
`flyctl logs -a sirosid-<env>-pdp` for `"Configuring whitelist registry from
config file"` sitting unfinished before assuming a new trust-config
regression — this is deliberate, tolerated behavior (`extra_trusted_issuers`
is deliberately still added to `pid_issuers` despite the slow-boot cost,
because a resolution-only trust check doesn't need the JWKS to have actually
resolved). Separately: if testing wallet-initiated *presentation* against an
external verifier, `TRUSTED_ISSUERS`/`extra_trusted_issuers` is **only** added
to `pid_issuers`, never `verifiers` — use `TRUSTED_VERIFIERS=` for that
instead (`make fly-up ENV=<name> TRUSTED_VERIFIERS=identity,...`); check
`fixtures/rendered/fly-<env>/pdp.yaml`'s `whitelist.lists.verifiers` if a
presentation-trust failure looks like it should've been covered but wasn't.
`TRUSTED_VERIFIERS` entries must be the exact string go-trust's
`WhitelistRegistry` compares against *after* its own `Subject.ID`
normalization, confirmed via a live PDP rejection: an `x509_hash:...`
`client_id` is left un-normalized (safe to paste verbatim from the wallet's
"not trusted" error log), but `x509_san_dns:<host>`/`x509_san_uri:<uri>`
values get normalized to `https://<host>`/`<uri>` before any whitelist match
runs — an entry written in the original `x509_san_dns:`/`x509_san_uri:` form
(the shape go-trust's own docs example at `docs/docs/sirosid/trust/go-trust.md`
uses) silently never matches. Separately, if a whitelisted `x509_san_dns:`/
`x509_san_uri:` verifier's request-signing certificate is issued by a
long-lived, self-signed "reader CA" root rather than a public CA (a real
ISO 18013-5 convention, not a misconfiguration — confirmed live for
`verifier.multipaz.org`, whose signing cert is distinct from its ordinary
publicly-CA-issued HTTPS cert), whitelist membership alone won't help:
`TrustX509ViaSystemCA`'s chain-validation step still runs for these two
schemes and can never succeed against a self-signed root. Use
`TRUSTED_VERIFIER_ROOTS=<path,...>` (go-trust#123+, PEM cert file paths,
repeatable/comma-separated) to merge that root into PDP's system CA pool
instead — see `fixtures/trusted-roots/README.md` for a worked example.

**vc-apigw/vc-issuer crash-looping on Fly with an `mdl`/mdoc-schema config
error (`unexpected end of JSON input` / `vctm_file_path ... required_without`):**
the deployed image is older than the `MDDLSchema` support
`fixtures/vc-config.yaml`'s `mdl` scope needs (`mddl_file_path`). Check
`values-fly.yaml`'s current `images.issuer*`/`verifier` pins — they should be
a published tag that includes `sirosfoundation/vc#23`
(`feature/support-mdoc-schema-driven`, merged upstream into `SUNET/vc`'s
`main`). If a `fly-up` was run with an explicit `IMAGES=` override for these
components, a *subsequent* plain `fly-up` (no `IMAGES=`) silently reverts them
back to whatever's pinned in `values-fly.yaml` — check
`flyctl status -a sirosid-<env>-vc-apigw`/`-vc-issuer` after any redeploy of
an environment you didn't build from scratch yourself.

**Every credential type fails at `POST /credential` while `/token` succeeds,
locally but NOT on `make fly-up`:** the browser shows only "Credential
issuance failed" - a sanitized constant from go-wallet-backend's
`ErrorCode.UserFacingMessage()`, never the reason. Never try to diagnose one
of these from the console. The reason is in **vc-apigw's** log, and (with the
issuer's raw response body) in wallet-backend's *debug* log, which
`docker-compose.test.yml` already enables via `WALLET_LOG_LEVEL=debug`:

```bash
docker logs vc-apigw-e2e 2>&1 | grep -iE "VCICredential|proofs verification|/credential"
docker logs wallet-backend-e2e-test 2>&1 | grep -i "credential endpoint error"
```

The local-only part is the real clue, and it generalizes well beyond this one
bug: **`make up` builds vc, go-wallet-backend and wallet-frontend from your
sibling checkouts, while `make fly-up` deploys the pinned images in
`values-fly.yaml`.** Local therefore runs *current source on both sides of
every protocol boundary*, and Fly runs a matched, older, known-good set. Any
spec-conformance tightening that landed in one repo but not the other shows
up locally and nowhere else - and reads as "Emil's machine is broken" rather
than as a real interop bug. When a failure is local-only, diff the two sides,
don't hunt for environment state.

Worked example (fixed 2026-08-24, go-wallet-backend
`fix/key-attestation-typ-spelling`): vc's apigw validates the Key Attestation
JWT header with `eq=key-attestation+jwt`
(`pkg/openid4vci/proof_attestation.go`) - the hyphenated media type
OpenID4VCI 1.0 registers in Appendix G.6.2 - while go-wallet-backend emitted
the pre-1.0 draft spelling `keyattestation+jwt`. Every credential type using
the `attestation` proof type failed at once, because the proof type is chosen
per issuer, not per credential. `siros-sdk-kotlin` and multipaz already used
the hyphenated form; go-wallet-backend was the outlier. Two other repos still
carry the old spelling (`go-r2ps-service`, `wallet-backend-server`) - check
them before assuming a KA interop failure is new.

A *different* local-only divergence with the same shape: mini-oidc is the one
local-stack service pulled from a registry rather than built from a sibling
checkout, so it's the only one that can differ between two machines that both
changed nothing. It used to default to an unpinned `:main`, which docker never
re-pulls once cached; it's now pinned from `values-fly.yaml`'s
`images.miniOidc` via the Makefile's `MINI_OIDC_VERSION`, so local and Fly
move together. Check with `docker image inspect
ghcr.io/sirosfoundation/mini-oidc:$MINI_OIDC_VERSION --format '{{.Created}}'`.

**Fly networking, in general:**
- 6PN (Fly's internal network) is **IPv6-only** — a component binding only
  IPv4 (`mongod`'s default) is unreachable from sibling apps over
  `.internal` unless it's explicitly told to bind `--ipv6` too. Bit
  env-admin 0.1.0 the same way (Python's `HTTPServer(("", port))` is
  IPv4-only): wallet-frontend's `/_admin/` proxy 502'd while the machine's
  own health check passed, since that check is local. Any new component
  must listen on `::` (dual-stack) — see `DualStackHTTPServer` in
  `env-admin/server.py`.
- **Scale to zero is per ENVIRONMENT, never per app.** `make fly-stop ENV=x` stops
  every machine (apps and the Mongo volume are kept; only the volume is billed) and
  `make fly-start ENV=x` brings them back in deploy order, waiting for health.
  Deploying with `--scale-to-zero` also sets `auto_start_machines = false`, so a stray
  request cannot wake one machine in front of a stopped backend (verified on real Fly:
  without it, a single request to a stopped environment woke only the frontend).
  `--org <org>` on `fly-up`/`fly-down`/`fly-power.py` targets a dedicated org.
- **Autostart only fires on the public edge**, never for internal 6PN calls
  between sibling apps — an internal-only component left on
  `auto_stop_machines='stop'` goes idle and *stays* stopped forever once a
  caller only ever reaches it over 6PN. Every component here uses
  `auto_stop_machines='off'`, `min_machines_running=1` — the environment's
  own `fly-up`/`fly-down` lifecycle is the actual on-demand mechanism, not
  per-machine autostop.
- A machine that crash-looped and got fixed by a config-only redeploy does
  **not** restart itself — `scripts/fly_common.py`'s `ensure_running()`
  explicitly checks and `fly machine start`s anything not `started` after
  every deploy. This runs inside `fly-up.py`'s own per-component deploy loop
  (right after each `flyctl deploy`), so it covers every component during a
  normal `make fly-up` — the gap is specifically a single component deployed
  by hand (bypassing `fly-up.py`), where nothing calls `ensure_running()` for
  you. The Mongo-auth footgun above is a common way to end up needing this:
  a manual partial redeploy crash-loops the component, `flyctl status` shows
  it `stopped`, and the fix is either `flyctl machine start -a
  sirosid-<env>-<component>` or (safer, and the recommended path per the
  Mongo-auth gotcha) just re-running the full `make fly-up ENV=<name>`.
- `flyctl secrets list --json` returns lowercase `"name"`, not `"Name"` — an
  idempotency check keyed on the wrong case silently always re-sets secrets.
- The PDP whitelist must list the identity that actually appears as the
  credential's `iss` claim (vc-apigw's/vc-verifier's *public* URL) — not an
  internal `.internal` hostname that's merely reachable. Whitelisting the
  wrong one produces a 404 on JWKS fetch that looks like a trust bug but is
  really a URL-choice bug.

## Where to look next

- `make help` — authoritative, current flag/target reference (this file
  intentionally doesn't duplicate it).
- `README.md` — setup, service ports, Android passkey troubleshooting table,
  directory structure.
- `ANDROID-TESTING.md` / `R2PS.md` — deep dives on Android SDK testing and
  the R2PS key-provisioning flow, respectively. (`ENVIRONMENT.md` and
  `CONFORMANCE-CANDIDATE-BUGS.md` were removed as stale/resolved - don't
  recreate them from an old cached read.)
- `scripts/fly_common.py`'s module/function docstrings — extremely thorough
  inline rationale for every Fly-specific design decision (component ordering,
  health-check strategy, nginx configs); read these before changing anything
  Fly-related rather than re-deriving it from scratch.
- `scripts/render-helm-config.py`'s `build_fly_values_overlay()` — the
  whitelist/mdociaca construction referenced above.
- `scripts/stack.py` (`make plan`) — the `make up` option matrix as data, with
  each option's help; `scripts/storage.py` (`make storage-*`) — storage from
  outside an environment; `env-admin/server.py` — the reset from inside;
  `bootmgr/sirosid_bootmgr/harness.py` — everything the TUI knows, no UI.

## Working conventions

- This repo pushes directly to `main` with **no PR workflow** — there is no
  review gate catching a bad direct push. Be conservative about committing
  changes here unprompted, and never run `make fly-up`/`fly-down`/anything
  Fly-touching without confirming no one else has a deployment for that
  `ENV=` name in progress (`flyctl apps list`/`fly-status ENV=<name>` first).
  `fly-down` now also **deletes the environment's data** (volumes go with the
  apps) unless `KEEP_DATA=yes` — ask before a plain `fly-down` of a shared
  environment.
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py'` (stack
  parity needs `make`; the fly-up tests need `helm` and `openssl`; nothing needs
  Docker or Fly — `tests/fakefly.py` is a stateful fake `flyctl`).
  **`tests/test_fly_up_characterization.py` is the safety net under any change to
  the deploy path**: it runs the real `fly-up.py` against the fake and compares
  the exact command sequence and every rendered file with `tests/golden/`. A
  deliberate change refreshes it with `UPDATE_GOLDEN=1`; an accidental one fails. Run it after touching
  the Makefile's compose logic, `scripts/stack.py`, `env-admin/`, or the Fly
  scripts' pure parts.
- `env-admin/` changes ship as an image: bump `VERSION` in `server.py`, push a
  `env-admin-v<version>` tag after merge (`.github/workflows/env-admin-image.yml`
  publishes `ghcr.io/sirosfoundation/sirosid-env-admin:<version>`), and bump
  `values-fly.yaml`'s `images.envAdmin`. Local `make up` builds it from the
  checkout regardless.
- `values-fly.yaml` and `.golden-releases.yaml` are checked-in, shared
  defaults — changes to them affect every developer's next `fly-up`/`GOLDEN=`
  run, not just yours. `.android-apps`, `.env*` files are gitignored,
  per-developer state.
