# GitLab Canonical Upstream Cutover

Move buzz from GitHub-only hosting to a GitLab-first workflow, keep GitHub as
a read-only mirror, and use the GitLab Container Registry to publish the
buzz image so operators can pull instead of build.

## Status

backlog

## Outcome

GitLab is the canonical upstream for buzz with `main` as the default branch.
GitHub mirrors `main` and tags from GitLab as a read-only replica.
Tagged releases automatically build and push the buzz container image to the
GitLab Container Registry, with `:latest` tracking the most recent release
tag and `:vX.Y.Z` pinned to the corresponding tag. The README and
`docker-compose.yml` reference the published image so an operator deploying
to portainer (or any plain Docker host) can `docker compose pull` instead of
building from source. Maintainer workflow documentation describes the hosting
model and the registry publish pipeline clearly.

## Decision Changes

- **GitLab is the canonical writable upstream for buzz.** GitHub remains a
  read-only mirror of `main` and tags so existing watchers/forks aren't
  abandoned mid-migration.
- **The GitLab Container Registry is the official image registry.** No
  Docker Hub publish — staying on a single host keeps secrets and access
  scoped to one place, and the registry is free for public projects.
- **Image tags follow git tags.** A push of `vX.Y.Z` triggers a CI job that
  builds `buzz/Dockerfile` and pushes both `:vX.Y.Z` and `:latest`. Pushes to
  `main` produce `:edge` for early adopters; `main` is not auto-deployed by
  the registry's `:latest` tag.
- **The published image is referenced from `docker-compose.yml`.** The
  current `build:` directive becomes a fallback under
  `docker-compose.dev.yml` while the production compose pulls from the
  registry. New operators who want to hack still get the build path; new
  operators who just want to deploy get a one-shot pull.
- **Cutover happens before the `setup-docs-truth` work item ships its
  README rewrite**, so the README can reference the published image URL
  directly without needing a follow-up doc patch.
- **No docker-compose.yml changes outside the image reference.** Compose
  internals (volumes, env, healthchecks) stay identical so existing operators
  can `docker compose pull && docker compose up -d` to migrate.

## Dependencies

- A GitLab namespace and project must exist.
- A GitHub credential or deploy token for mirror pushes if GitLab-managed
  push mirroring requires one.
- Repository settings access on both providers.
- A `CI_REGISTRY_USER` / `CI_REGISTRY_PASSWORD` (or project deploy token)
  scoped to write packages, available to GitLab CI as protected variables.

## Main Quests

- Create the GitLab project and confirm `main` is the default branch.
- Push full history to GitLab and verify `main` and tags are present.
- Configure GitLab as the canonical writable remote for local clones; update
  the local working copy to point its `origin` at GitLab.
- Configure one-way mirroring from GitLab to GitHub for `main` and tags.
- Lock down GitHub collaboration surfaces (issues, PRs) so contributors are
  redirected to GitLab. Keep GitHub's repository description pointing at the
  GitLab URL.
- Add a `.gitlab-ci.yml` with at least three stages:
  - `test`: run `uv run pytest` and `npx htmlhint "buzz/pyview_templates/*.html"`
    on every push and merge request.
  - `build`: build `buzz/Dockerfile` against the working copy on every tag
    matching `v*` and on every push to `main`.
  - `publish`: push to `$CI_REGISTRY_IMAGE:vX.Y.Z` and `$CI_REGISTRY_IMAGE:latest`
    on tag pushes; push `$CI_REGISTRY_IMAGE:edge` on `main` pushes. Use
    GitLab's predefined `CI_REGISTRY_*` variables; no externally-managed
    secrets.
- Update `docker-compose.yml` to reference
  `image: registry.gitlab.com/<namespace>/buzz:latest` for both `buzz-dav`
  and `buzz-curator`. Move the existing `build:` block into
  `docker-compose.dev.yml` as a development override.
- Verify a change merged to GitLab `main` appears in GitHub `main`.
- Verify a new annotated tag pushed to GitLab appears in GitHub and triggers
  the registry publish.
- Verify `docker compose pull` against the public image works on a clean
  host with no source checkout.
- Update the README to point operators at the published image URL and
  describe the build-from-source path as the development flow.
- Check for hardcoded GitHub clone URLs before declaring the migration
  complete (CI badges in `README.md`, any `git clone` examples in `docs/`).
- Record mirror owner, credential type, and failure-recovery steps in the
  maintainer workflow notes (in `docs/`).

## Acceptance Criteria

- GitLab is the canonical upstream with `main` as the default branch and
  `docs/planning-framework`-style work happens via merge requests there.
- GitHub mirrors `main` and tags from GitLab as a read-only replica.
- Tagging `vX.Y.Z` on GitLab produces both `:vX.Y.Z` and `:latest` images
  in the registry within one CI run.
- Pushing to `main` produces `:edge` in the registry and does not move
  `:latest`.
- `docker compose pull && docker compose up -d` works against a clean host
  with no source checkout, using only `docker-compose.yml`, `.env`, and
  `buzz.yml`.
- README references the published image URL and the build-from-source path
  is described under Development.
- The maintainer workflow doc explains who owns the registry credentials,
  how mirror sync is monitored, and the recovery steps if either pipeline
  breaks.

## Metadata

### id

gitlab-cutover

### type

Issue
