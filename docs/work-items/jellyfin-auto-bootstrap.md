# Jellyfin Auto-Bootstrap

Eliminate the manual Jellyfin handoff from the buzz first-time setup.

## Status

backlog

## Outcome

An operator who runs `docker compose up -d` on a clean host should reach a
fully-configured stack — Jellyfin admin user provisioned, libraries created,
API key minted and threaded back into buzz — without ever opening Jellyfin's
web UI. The bootstrap is idempotent: re-running it on an already-configured
stack is a no-op. Credentials are generated on the host (for the admin
password) and minted by Jellyfin itself (for the API key); nothing is
hardcoded into the repository.

The work is exploratory because Jellyfin's startup endpoints have changed
across major versions and not all of them are stable contract surfaces.
This work item ships a recipe verified against the Jellyfin version pinned
in `docker-compose.yml`, with version-pinning as part of the contract.

## Decision Changes

- **Jellyfin's image tag is pinned**, not floating. The current
  `docker-compose.yml` uses `jellyfin/jellyfin` (unpinned latest). This
  bootstrap recipe assumes a known version, so the compose file pins to a
  specific tag and the bootstrap is verified against that tag. Bumping
  Jellyfin in the future requires re-verifying the bootstrap; this is an
  acceptable maintenance cost.
- **Bootstrap runs in a one-shot init container**, `buzz-jellyfin-init`,
  with `restart: "no"`. It depends on `jellyfin: service_healthy` and exits
  after configuration completes. Re-running `docker compose up -d` is safe
  because the container detects existing configuration and exits cleanly.
- **Admin password is generated on first run and persisted** in
  `data/jellyfin-admin.secret` (chmod 600). Operators can read it from there
  if they want to log into Jellyfin manually. The username is `buzz` by
  default, configurable via env var.
- **The minted API key is written to `buzz.overrides.yml`** at
  `media_server.api_key`. The config UI already manages this file; the
  init container uses the same write path (atomic rename, schema
  validation). Curator/dav reload picks up the value on next config reload
  without a restart.
- **Library creation respects the same `media_server.library_map`** that
  selective-sync uses. The init container reads
  `library_map` from the merged buzz config and creates one Jellyfin
  VirtualFolder per entry, each pointed at
  `/mnt/buzz/curated/<category>`.
- **Three-step recipe, with each step independently failure-recoverable**:
  1. Wizard bypass (file-seeded before Jellyfin's first start).
  2. Admin user creation (REST, before any auth is required).
  3. API key + libraries (REST, requires admin auth).
  Each step is idempotent and logs a single line on success. If step 2 fails
  the container exits non-zero and the operator can rerun the bootstrap;
  the prior step's effect is preserved.

## Dependencies

- `setup-docs-truth` should land first so that `media_server.api_key` and
  `media_server.url` are first-class YAML fields (the bootstrap writes
  there).
- `gitlab-cutover` does not block this but sequencing the docs work
  afterward is cleaner — the README's bootstrap section can describe the
  fully-automated flow once available.

## Main Quests

- **Pin the Jellyfin image** in `docker-compose.yml` to a specific tag and
  document the pinning in the README.
- **Wizard bypass**: the init container seeds
  `config/jellyfin/system.xml` before Jellyfin starts the first time. The
  bypass marker (current Jellyfin: `<IsStartupWizardCompleted>true</...>`)
  must be confirmed against the pinned version. If the file already exists
  with the marker set, skip seeding.
- **Admin user creation**: poll Jellyfin's healthcheck endpoint until
  ready, then `POST /Startup/User` with the generated credentials. If
  Jellyfin reports the wizard is already complete, skip and read the
  password from `data/jellyfin-admin.secret`.
- **API key minting**: authenticate as the admin user, then
  `POST /Auth/Keys?App=buzz` (or the version-appropriate equivalent).
  Persist the key by writing `media_server.api_key` into
  `buzz.overrides.yml` via the same atomic-write helper used by
  `dav_app.py::persist_overrides`. If a key with `App=buzz` already exists,
  reuse it.
- **Library creation**: read `media_server.library_map` from the merged
  buzz config, then for each `(category, jellyfin_name)` pair, call
  `POST /Library/VirtualFolders?name=<jellyfin_name>&collectionType=<inferred>&paths=/mnt/buzz/curated/<category>`.
  The `collectionType` mapping is `movies → movies`, `shows → tvshows`,
  `anime → tvshows` (anime is a Jellyfin tag, not a collection type). If a
  VirtualFolder with the target name already exists, skip creation but
  verify the path.
- **Init container image**: build the bootstrap as a small Python script
  that reuses `buzz/models.py` for YAML I/O and `httpx` for REST. Ship it
  in the same image as buzz-dav under a different entrypoint (e.g.
  `entrypoint: ["python", "-m", "buzz.scripts.jellyfin_init"]`) to avoid
  a second image build.
- **Idempotency tests**: integration test that runs the bootstrap twice
  back-to-back against a fresh Jellyfin container; the second run logs
  "already configured" and exits 0 within a few seconds.
- **Failure-mode tests**: integration tests that simulate (a) Jellyfin
  unreachable, (b) wrong API contract (HTTP 404 from `/Startup/User`),
  (c) write failure on `buzz.overrides.yml`. Each surfaces a distinct
  non-zero exit code so an operator can diagnose without log spelunking.
- **Trigger config reload**: after writing the API key, POST
  `http://buzz-curator:8400/api/config/reload` so curator picks up the new
  value without a service restart. (`buzz-dav` already hot-reloads
  `media_server.api_key` once it's a UI-managed field — depends on
  `setup-docs-truth`.)

## Side Quests

- Surface the bootstrap result in the buzz config UI as a status banner
  ("Jellyfin auto-configured ✓" with a "rotate API key" button). Defer
  unless the bootstrap stabilizes.
- Add the same recipe for Plex (`POST /myplex/claim` with a Plex Claim
  token, then library creation via Plex API). This is a bigger project
  because Plex requires an external claim flow tied to a logged-in
  Plex account; track it separately.

## Acceptance Criteria

- A fresh `docker compose up -d` on a clean host produces a stack with
  Jellyfin admin, API key, and three libraries (movies, shows, anime)
  configured, with no manual steps and no Jellyfin web UI access required.
- The minted API key appears in `data/buzz.overrides.yml` under
  `media_server.api_key`.
- Curator config reload runs automatically after the key is written.
- Re-running `docker compose up -d` on an already-configured stack causes
  the init container to log "already configured" and exit 0 within ~5
  seconds. No state is mutated.
- Wiping `data/jellyfin-admin.secret` and re-running the bootstrap does
  *not* succeed (we never overwrite an existing admin) — it logs a clear
  error directing the operator to delete `config/jellyfin` for a full
  reset.
- The Jellyfin image tag is pinned in `docker-compose.yml`.
- README documents the pinned version and the recovery procedure if a
  Jellyfin major bump breaks the bootstrap.
- Integration tests cover happy path, idempotency, and the three named
  failure modes.

## Open Questions

- **API contract stability**: `POST /Startup/User` and `POST /Auth/Keys`
  are the two surfaces most likely to change between Jellyfin majors.
  Verification against the pinned tag is part of the work, but the work
  item should also identify a fallback strategy (e.g. seeding
  `config/jellyfin/users.json` directly if the REST surface breaks).
  This needs investigation before promoting the work item from `backlog`
  to `planned`.
- **Permission model**: does the buzz-jellyfin-init container need write
  access to `config/jellyfin/` to seed `system.xml`? If so, it must run
  before Jellyfin starts, which means depending on the volume being
  populated rather than on Jellyfin being healthy. Two-phase start
  (`buzz-jellyfin-init-precheck` → `jellyfin` → `buzz-jellyfin-init`).
- **Secret handling**: persisting the admin password to disk in
  `data/jellyfin-admin.secret` is a tradeoff. An alternative is to never
  expose it (admin-by-API-key only), but then operators have no manual
  fallback if the API key is lost.

## Metadata

### id

jellyfin-auto-bootstrap

### type

Issue
