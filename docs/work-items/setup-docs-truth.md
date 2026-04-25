# Setup Docs Truth

## Status

backlog

## Outcome

The `README.md` should be the single, accurate operational source of truth
for setting up buzz from a clean machine to a running stack. Today the README
drifts in several places: it documents the OpenSubtitles credentials and the
Jellyfin API key as living in `.env` while the codebase has moved most
configuration into `buzz.yml` (and the new config UI writes
`buzz.overrides.yml`); newer YAML keys (`media_server.library_map`,
`server.upstream_concurrency`) are not listed at all; the actual first-time
bootstrap involves a Jellyfin-then-buzz dance that the README never spells
out; and the `.env.dist` carries dead variables (`LIBRARY_MOUNT`) that the
compose file silently overrides. An operator following the README today will
either over-configure the stack, hit unexplained ordering surprises, or
configure values that have no effect.

The fix is small but precise: walk the actual setup as a fresh user would,
write down what they actually need to do, retire what they don't, and pick a
single canonical home for each setting (with env vars defined as overrides
so portainer-style deployments still work). Keep the README short — split into
`docs/` only if a section grows beyond a screen.

## Decision Changes

- **YAML is canonical, env vars override.** All operator-facing settings live
  in `buzz.yml` (and `buzz.overrides.yml` for UI-driven changes). Env vars are
  retained as overrides so that a portainer-style "deploy a docker-compose"
  workflow can inject secrets without writing a YAML file. This matches the
  precedence we'll need once HTTPS + login lands and the UI becomes the
  primary configuration surface. Where YAML and env disagree, env wins.
- **`.env.dist` is trimmed to deployment-shape variables only**: `PUID`,
  `PGID`, `MEDIA_SERVER`, `COMPOSE_PROFILES`, `JELLYFIN_URL`,
  `PLEX_URL`/`PLEX_TOKEN`/`PLEX_VERSION`/`PLEX_CLAIM`. `LIBRARY_MOUNT` is
  removed because `docker-compose.yml:13` already hard-codes it and the
  `.env` value is dead. `OPENSUBTITLES_*`, `SUBTITLE_*`, and
  `JELLYFIN_API_KEY` move out of the documented happy path — they remain
  read by the code as overrides for portainer-style deploys, but the README
  steers operators toward the YAML.
- **The first-time bootstrap is documented as an explicit ordered list with
  the Jellyfin handoff called out**: clone → host prep → `buzz.yml` with RD
  token → `.env` with profile + URLs → `docker compose up -d` → open Jellyfin
  → wizard + admin + libraries → mint API key → paste into `buzz.yml`'s
  `media_server.api_key` (or via the config UI) → restart. The handoff is
  unavoidable until `jellyfin-auto-bootstrap` lands; this work item makes it
  legible, not automated.
- **Two new YAML keys are added to the configuration reference** with their
  defaults and "why you'd touch this" commentary:
  `media_server.library_map` (debrid category → Jellyfin library name) and
  `server.upstream_concurrency` (cap on simultaneous Real-Debrid CDN
  connections; lever for surviving Jellyfin scan storms).
- **Jellyfin API credentials migrate from env-var-only to YAML-first.** A new
  `media_server` section accepts `api_key`, `url`, and the existing
  `library_map`. The env vars (`JELLYFIN_URL`, `JELLYFIN_API_KEY`,
  `JELLYFIN_SCAN_TASK_ID`) remain as overrides. This is the prerequisite
  that lets the config UI eventually edit Jellyfin credentials directly.

## Main Quests

- Reconcile the README's `buzz.yml` and `.env` tables with the current code
  (`buzz/models.py::CuratorConfig`, `DavConfig`):
  - Add rows for `media_server.library_map`, `media_server.url`,
    `media_server.api_key`, `server.upstream_concurrency`.
  - Update the complete YAML example to include `media_server` and
    `server.upstream_concurrency`.
  - Mark `OPENSUBTITLES_*` and `SUBTITLE_*` env vars as "override only — see
    `subtitles.*` in `buzz.yml`".
  - Remove `LIBRARY_MOUNT` from `.env.dist` and the README's `.env` table.
- Move Jellyfin credentials into the YAML schema:
  - Add `media_server.url` and `media_server.api_key` to `CuratorConfig`,
    sourced from merged YAML; keep env vars as overrides.
  - Update `_OVERRIDE_SCHEMA` and `to_nested_dict` so the config UI can
    surface these fields when we choose to. This work item only ships the
    schema move, not the UI exposure (deferred to a follow-up so the config-UI
    surface stays small until HTTPS + login lands).
- Rewrite the **Quick Start** as an ordered, numbered procedure that names the
  Jellyfin step explicitly:
  1. Host prep.
  2. `cp buzz.dist.yml buzz.yml`; fill `provider.token`.
  3. `cp .env.dist .env`; pick `MEDIA_SERVER`.
  4. `docker compose up -d`.
  5. Open Jellyfin web UI; finish wizard; set admin password; add libraries
     pointing at `/mnt/buzz/curated/{movies,shows,anime}`; mint API key.
  6. Paste API key into `buzz.yml`'s `media_server.api_key`.
  7. `docker compose restart buzz-dav buzz-curator`.
- Add a **Configuration Precedence** subsection that explains the
  YAML / overrides / env layering in two sentences:
  `buzz.yml` is the base; `buzz.overrides.yml` (written by the config UI) wins
  over the base; environment variables win over both. Operators who manage
  config from the UI should leave env vars unset.
- Add a one-line note that the published image (once
  `gitlab-cutover` lands) lets the user skip the build step. Until then, the
  build-from-source flow stays.

## Acceptance Criteria

- A new operator can follow the README top-to-bottom and reach a working
  stack without consulting code or other docs.
- Every variable named in `.env.dist` and every key in the README's `buzz.yml`
  table is read by the code; nothing is dead.
- `media_server.library_map` and `server.upstream_concurrency` are documented
  with defaults and rationale.
- `media_server.api_key` and `media_server.url` are accepted by `CuratorConfig`
  via YAML, with the existing env vars retained as overrides; setting either
  via YAML is sufficient (no env var required).
- The Jellyfin-bootstrap step is named in the Quick Start with explicit
  ordering.
- `LIBRARY_MOUNT` no longer appears in `.env.dist` or the README.
- All existing tests still pass; new tests cover the YAML loader picking up
  `media_server.url` / `media_server.api_key` and env-var override precedence.

## Metadata

### id

setup-docs-truth

### type

Issue
