# Reseed

## Status

planned

## Outcome

From the archive view, an operator can put an entry back into seeding. buzz
stages the entry's file bytes into a seed directory readable by an external
torrent client, adds the torrent to that client using the magnet and info-hash
already stored per entry, forces a recheck, and starts seeding. qBittorrent is
the first supported client, behind a small client interface so others can
follow.

Reseed is compatible with every configured provider and does not depend on any
other feature: staging downloads bytes through the same provider
stream-resolution machinery buzz already uses for WebDAV, from whichever
provider has a live link. If a local copy of the entry already exists on the
same machine, staging degrades gracefully into a cheap disk copy, but nothing
in reseed requires that.

Staging is bounded by a strict disk-usage limit. By default, buzz will never
fill the seed-path filesystem past 80% of its capacity.

## Decision Changes

- **External client, thin orchestration.** buzz does not embed a BitTorrent
  engine. A new `SeedClient` interface with a qBittorrent Web API
  implementation (e.g. `buzz/seeding/qbittorrent.py`) handles add-by-magnet,
  recheck, resume, status, and delete. buzz orchestrates; the client seeds.
  `buzz-dav` remains the only process that talks to providers.
- **Staging makes reseed provider-independent.** A background task downloads
  each of the entry's selected files from the highest-priority provider with a
  live link (restoring via magnet first when none is live), through the
  existing stream-resolution flow, into `seeding.save_path/{torrent-name}/…`.
  Once staged, buzz adds the magnet to the client with skip-download
  semantics, triggers a recheck, and resumes so the client seeds the verified
  pieces.
- **Strict disk-usage limit.** Before staging, buzz inspects the seed-path
  filesystem via `os.statvfs` and refuses to stage if projected usage (current
  usage plus the entry's bytes) would exceed
  `seeding.max_fs_usage_percent` (default `80`). The limit is re-checked
  during staging; on breach the task aborts cleanly, removes partial files,
  and records a warning event. The check is against actual filesystem usage,
  not per-feature bookkeeping, so it composes safely with any other feature
  writing to the same filesystem. The archive UI disables the reseed action,
  with an explanatory title, for entries that cannot fit.
- **Partial selections seed partially.** Entries where only a subset of the
  torrent's files was selected can only seed the pieces fully contained in the
  staged files; boundary pieces spanning missing files cannot be served. buzz
  sets file priorities accordingly and documents this limitation rather than
  solving it.
- **Seed lifecycle in the archive view.** Seeding status is surfaced per entry
  via client polling, with a stop/remove-seed action. Removing a seed can
  optionally delete the staged files.
- **New configuration.** Add `seeding.qbittorrent.url`,
  `seeding.qbittorrent.username`, `seeding.qbittorrent.password` (a secret,
  masked in read views and logs like provider tokens), `seeding.save_path` (a
  path both buzz and the client can access, mapped consistently when either
  runs in a container), `seeding.max_fs_usage_percent`, and an optional
  `seeding.category`. Whitelist the editable fields in
  `UI_MANAGED_CONFIG_PATHS`.

## Main Quests

- **Define the seed client interface and qBittorrent implementation.**
  - Add a `SeedClient` interface covering authentication, add-by-magnet with
    save path and category, recheck, resume, per-hash status, and delete with
    optional data removal.
  - Implement it against the qBittorrent Web API with httpx, including session
    cookie handling and clear provider-neutral errors.
- **Add configuration and secrets.**
  - Add the `seeding.*` config fields to `DavConfig` in `buzz/models.py` with
    validation, UI-managed paths, and password masking in effective-config
    read views and logs.
- **Build the staging pipeline.**
  - Add `BuzzState` submit/execute methods running on `BackgroundTaskPool`
    with progress on `/threads`, cooperative cancellation, and `record_event`
    telemetry.
  - Reuse the provider stream-resolution machinery for downloads, write to
    temporary files, and atomically rename into the seed directory.
  - Enforce the disk-usage limit up front and mid-stage with clean abort and
    partial-file cleanup.
  - After staging, add the torrent to the client, set file priorities for
    partial selections, force recheck, and resume.
- **Update the archive view.**
  - Add an `[S]` reseed action, a seeding status tag driven by client polling,
    and a stop-seed action, following the existing inline action style and
    Dracula color vocabulary from DESIGN.md.
- **Update documentation.**
  - Extend the architecture docs and config reference with the seeding
    subsystem, the qBittorrent requirements, the shared-filesystem semantics of
    the disk-usage limit, and the partial-selection limitation.

## Acceptance Criteria

- Reseed works from the archive view for an entry available on exactly one
  provider, for each configured provider kind, including the restore-first
  path for entries with no live link.
- After staging, qBittorrent reaches a seeding state for the entry's torrent
  once recheck completes.
- Staging that would push the seed-path filesystem past
  `seeding.max_fs_usage_percent` (default 80%) is refused before it starts,
  and aborts cleanly with partial-file cleanup if the limit is hit mid-stage;
  the UI disables the action for entries that cannot fit.
- Partial selections seed with correct file priorities and the limitation is
  documented.
- Seeding status appears in the archive view and stop-seed removes the torrent
  from the client, optionally deleting staged files.
- qBittorrent credentials are masked in read views and logs but editable from
  the config UI.
- New tests cover the qBittorrent client against fake API payloads, the
  staging pipeline including limit enforcement and cancellation, lifecycle
  actions, and config secret handling.
- `uvx pyright buzz tests` passes.
- The Docker-based pytest suite passes.

## Metadata

### id

reseed

### type

Issue
