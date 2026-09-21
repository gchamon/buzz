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

- **Jellyfin's image is already digest-pinned, but not human-readable.**
  The current `docker-compose.yml:77` uses
  `jellyfin/jellyfin:latest@sha256:1694ff069f0c9dafb283c36765175606866769f5d72f2ed56b6a0f1be922fc37`.
  A digest pin is immutable (stronger than a tag pin), but `:latest` makes
  it impossible to tell at a glance which version is in use. This work item
  switches the tag to a named version (e.g. `10.11.8`) while keeping the
  digest, so the pinned version is greppable. Bumping Jellyfin requires
  re-verifying the bootstrap; that's an acceptable maintenance cost.
- **Bootstrap runs in a one-shot init container**, `buzz-jellyfin-init`,
  with `restart: "no"`. It depends on `jellyfin: service_healthy` and exits
  after configuration completes. Re-running `docker compose up -d` is safe
  because the container detects existing configuration and exits cleanly.
- **Admin password is generated on first run and persisted** in
  `data/jellyfin-admin.secret` (chmod 600). Operators can read it from there
  if they want to log into Jellyfin manually. The username is `buzz` by
  default, configurable via env var.
- **The minted API key is written to `buzz.overrides.yml`** under whichever
  path `setup-docs-truth` settles on (`media_server.api_key` if the schema
  is flattened, otherwise `media_server.jellyfin.api_key` — today
  `_OVERRIDE_SCHEMA` in `buzz/models.py:251-288` only accepts
  `media_server.library_map`, so the bootstrap cannot persist until the
  schema is extended). The config UI already manages this file; the init
  container uses the same `save_overrides` write path (atomic rename,
  schema validation). Curator/dav reload picks up the value on next
  config reload without a restart.
- **Library creation respects the same `media_server.library_map`** that
  selective-sync uses. The init container reads
  `library_map` from the merged buzz config and creates one Jellyfin
  VirtualFolder per entry, each pointed at
  `/mnt/buzz/curated/<category>`.
- **Jellyfin starts first; the init container does the entire setup over
  REST.** The original three-step recipe assumed the init container could
  pre-seed `config/jellyfin/system.xml` before Jellyfin's first start.
  External evidence ([jellyfin#12961](https://github.com/jellyfin/jellyfin/issues/12961),
  the official container docs) shows this doesn't work: Jellyfin generates
  its config tree on first boot, and pre-seeding individual files in an
  empty bind-mount causes mount conflicts. The viable ordering is a single
  REST sequence after Jellyfin is healthy:
  1. Poll `GET /health` until 200.
  2. `POST /Startup/Configuration` (locale defaults).
  3. `POST /Startup/User` with the generated admin credentials (no auth
     required; protected by `FirstTimeSetupOrElevated` while the wizard is
     incomplete).
  4. `POST /Startup/Complete` to mark the wizard done.
  5. `POST /Users/AuthenticateByName` to obtain a session token.
  6. `GET /Auth/Keys` → `POST /Auth/Keys?app=buzz` if no `App=buzz` key
     exists.
  7. `GET /Library/VirtualFolders` → `POST /Library/VirtualFolders` for
     each missing entry in `library_map`.
  8. `save_overrides` writes the API key into `buzz.overrides.yml`.
  9. `POST http://buzz-curator:8400/api/config/reload`.
  Each step is independently idempotent (detect existing → skip vs create)
  and logs a single line on success. Editing an already-generated
  `system.xml` remains a documented recovery path if `POST /Startup/User`
  ever breaks on a future major.

## Dependencies

- **`setup-docs-truth` is a hard prerequisite.** Today
  `_OVERRIDE_SCHEMA` (`buzz/models.py:251-288`) accepts only
  `media_server.library_map`; `save_overrides` will reject any write to
  `media_server.api_key` (or `media_server.jellyfin.api_key`) with
  `ValueError: Invalid override keys`. `setup-docs-truth` extends the
  schema and decides whether the path is flattened to `media_server.api_key`
  or kept nested under `media_server.jellyfin.*` (currently the latter; see
  `UI_MANAGED_CONFIG_FIELDS` in `models.py:38-40`). The bootstrap cannot
  ship until that decision lands.
- `gitlab-cutover` is **done** and does not block this — the README's
  bootstrap section can describe the fully-automated flow once available.

## Main Quests

- **Switch the Jellyfin image to a named-version tag plus digest** in
  `docker-compose.yml` (currently `:latest@sha256:...`). The digest pin
  already exists; this just makes the version greppable. Document the
  pinned version in the README and add the recovery procedure for major
  bumps.
- **Add a `healthcheck` to the `jellyfin` service** in `docker-compose.yml`
  (`curl -fsS http://127.0.0.1:8096/health`) so `buzz-jellyfin-init` can
  `depends_on: { jellyfin: { condition: service_healthy } }`.
- **Wizard completion (no pre-seeding)**: after Jellyfin is healthy, the
  init container completes the wizard via the REST sequence in Decision
  Changes (`POST /Startup/Configuration` → `POST /Startup/User` →
  `POST /Startup/Complete`). If `GET /Startup/Configuration` (or any
  startup endpoint) returns "wizard already complete", skip steps 2–4 and
  load the persisted password from `data/jellyfin-admin.secret`. Editing
  the already-generated `system.xml` is documented as a recovery path
  only.
- **Admin user creation**: covered by `POST /Startup/User` in the REST
  sequence above. The `FirstTimeSetupOrElevated` policy means no auth is
  required while the wizard is incomplete.
- **API key minting**: authenticate as the admin user
  (`POST /Users/AuthenticateByName`), then `GET /Auth/Keys` to detect an
  existing `App=buzz` key (idempotency); `POST /Auth/Keys?app=buzz` if
  none. Persist the key via `save_overrides` (`buzz/models.py:308-321`) —
  the same atomic-write helper used by `dav_app.py:788-807::persist_overrides`.
  Verify the override path matches what `setup-docs-truth` chose before
  implementation.
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

- **Which Jellyfin version do we pin to, and who owns re-verification on
  each major bump?** Latest stable per Docker Hub research is `10.11.8`.
  The bootstrap is verified against whatever version is named here; a
  Jellyfin major bump requires re-running the verification before bumping
  the tag in `docker-compose.yml`. Decide before promoting from `backlog`
  to `planned`.

### Resolved (kept here for future-reader context)

- ~~API contract stability~~ — `POST /Startup/User`, `POST /Auth/Keys`,
  `POST /Library/VirtualFolders` are all present and exercised by the
  Jellyfin web UI in 10.11.x stable OpenAPI. Not a formal contract
  surface, but stable enough to depend on with the documented `system.xml`
  / `users.json` recovery path as a fallback.
- ~~Permission model / two-phase start~~ — moot under the Jellyfin-first
  ordering adopted in Decision Changes. The init container only needs
  network access to Jellyfin and write access to `data/`; no
  `config/jellyfin/` write needed in the happy path.
- ~~Secret handling~~ — keep persisting the admin password to
  `data/jellyfin-admin.secret` (chmod 600). The admin-by-API-key-only
  alternative leaves operators without a recovery path if overrides are
  wiped.

## Metadata

### id

jellyfin-auto-bootstrap

### type

Issue
