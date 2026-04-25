# Dependency Supply-Chain Checks

## Status

backlog

## Outcome

Buzz should detect risky dependency and container changes before they reach
`main`. Python dependencies, Docker base images, Compose images, and CI images
should be scanned for high-impact vulnerabilities and updated through a
delayed merge-request workflow. Normal dependency update proposals should wait
at least seven days after upstream publication so compromised or mistakenly
published packages have time to be detected upstream before Buzz consumes
them.

The implementation should prefer reusable CI components over locally
maintained low-level scanner jobs. Local jobs are acceptable only when no
ready-made component covers the required behavior cleanly.

## Decision Changes

- **Use OSS scanner tooling unless a reusable CI component already wraps it.**
  The default plan is Trivy for both Python filesystem/lockfile scanning and
  container image scanning, but preparation must first check whether
  `gabriel.chamon/ci-components` or another approved component already
  provides dependency, container, or security scanning jobs that can be reused
  from `.gitlab/ci/security.gitlab-ci.yml`.
- **Stay compatible with GitLab Free.** Use native GitLab security templates
  only when the feature is available on the Free plan. If a required scanner is
  not Free-plan compatible, use a local OSS scanner job instead.
- **Block high-impact findings.** Security scan jobs should fail for `HIGH`
  and `CRITICAL` findings. Lower-severity findings can be reported in
  artifacts without blocking the pipeline.
- **Use Renovate for delayed updates.** Add repository-level Renovate
  configuration with a seven-day minimum release age for Python and Docker
  updates. Disable automerge and keep the dependency dashboard enabled.
- **Pin Docker references.** Floating Docker tags such as `latest` and
  `alpine` should be replaced with explicit tag-plus-digest references where
  feasible so updates become reviewable merge requests and the seven-day hold
  can apply meaningfully.
- **Do not scan the project's own published image as an update dependency.**
  Renovate should ignore `registry.gitlab.com/gabriel.chamon/buzz/buzz` to
  avoid self-update loops.

## Preparation

- Check the available CI component repository before writing jobs:
  - Inspect `https://gitlab.com/gabriel.chamon/ci-components`.
  - Look specifically for reusable components for dependency scanning,
    container scanning, Trivy, OSV, SBOM generation, Renovate validation, or
    image build-and-scan workflows.
  - If a component exists, consume it from
    `.gitlab/ci/security.gitlab-ci.yml` and pass inputs rather than
    reimplementing scanner commands.
  - If no suitable component exists, document that decision in the merge
    request and keep the local jobs small enough that they can later be
    extracted into `ci-components`.
- Confirm the exact scanner behavior against the current repo:
  - Python dependencies are declared in `pyproject.toml` and resolved in
    `uv.lock`.
  - The application image is built from `buzz/Dockerfile`.
  - Runtime Docker image references live in `docker-compose.yml`.
  - CI image references live in `.gitlab/ci/*.gitlab-ci.yml`.

## Main Quests

- **Create the security CI split file**:
  - Add a `security` stage to `.gitlab-ci.yml`.
  - Include `.gitlab/ci/security.gitlab-ci.yml`.
  - Prefer reusable component includes inside that file when preparation finds
    a suitable component.
  - If local jobs are required, add:
    - a Python dependency scan over `uv.lock` / repository filesystem.
    - a container image scan that builds an ephemeral Buzz image and scans it.
  - Publish machine-readable scan artifacts for both jobs.

- **Scope CI execution by changes**:
  - Python dependency scanning should run when `pyproject.toml`, `uv.lock`,
    or security CI config changes.
  - Container scanning should run when `buzz/Dockerfile`, Python dependency
    files, application code copied into the image, scripts, or security CI
    config changes.
  - Both scans should run on merge requests and `main` branch pipelines when
    relevant files changed.

- **Add Renovate configuration**:
  - Enable package managers for Python/uv, Dockerfile, Docker Compose, and
    GitLab CI image references.
  - Set a seven-day minimum release age for normal updates.
  - Require release timestamps when enforcing the age gate.
  - Disable automerge.
  - Enable the dependency dashboard.
  - Group related low-risk patch/minor updates where Renovate supports it,
    but keep major updates separate.

- **Pin Docker dependencies**:
  - Pin `buzz/Dockerfile` base image.
  - Pin Compose service images that currently float.
  - Pin CI job images where practical.
  - Keep the Buzz runtime image reference
    `registry.gitlab.com/gabriel.chamon/buzz/buzz:v1` as the operator-facing
    tracked release image; do not ask Renovate to update it.

- **Document the policy**:
  - Update `README.md` or `docs/architecture.md` with a short supply-chain
    section explaining scanners, blocking severity, Renovate's seven-day hold,
    and the reason Docker images are pinned.

## Acceptance Criteria

- `.gitlab-ci.yml` includes a `security` stage and a split
  `.gitlab/ci/security.gitlab-ci.yml`.
- The implementation either reuses ready-made CI components for scanner jobs
  or documents why local scanner jobs were necessary.
- Python dependency scans fail for `HIGH` or `CRITICAL` findings and publish
  artifacts.
- Container image scans fail for `HIGH` or `CRITICAL` findings and publish
  artifacts.
- Renovate is configured to wait seven days before proposing normal Python and
  Docker updates.
- Floating Docker image tags used by Buzz, Compose, and CI are pinned where
  feasible.
- The project's own published runtime image is excluded from Renovate update
  proposals.
- CI YAML validates, Compose still renders with `docker compose config`, and
  the Renovate config passes validation or a dry run.

## Metadata

### id

dependency-supply-chain-checks

### type

Chore
