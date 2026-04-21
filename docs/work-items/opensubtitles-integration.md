# OpenSubtitles Integration

## Status

Planned

## Outcome

Operators using buzz should be able to get subtitles for their curated library
without standing up an external *arr service or manually managing `.srt` files.
The curator already knows every video file it manages — it should use that
knowledge to fetch matching subtitles automatically from OpenSubtitles REST API
v2 and wire them into the curated library on each rebuild.

Subtitles are written to a persistent overlay directory (`/mnt/buzz/subs`) so
they survive the curator's full wipe-and-replace cycle. On every rebuild, the
curator re-creates symlinks from `/mnt/buzz/curated` into the overlay, so
Jellyfin and other consumers always see the right files without re-downloading
what already exists. The system fetches one subtitle per configured language per
video file, respects rate limits, and applies user-configured filters for
hearing-impaired, AI-translated, and machine-translated tracks.

## Decision Changes

- Subtitle fetching runs **inside the curator service** as a post-build step,
  not as a standalone daemon. After `build_library()` creates the symlink tree,
  a subtitle phase walks the mapping, checks the overlay for already-downloaded
  files, queries OpenSubtitles for anything missing, and writes `.srt` files to
  `/mnt/buzz/subs/{category}/{relative_path}`. Symlinks from the curated
  directory into the overlay are created every rebuild regardless of whether a
  new download occurred.
- The overlay directory (`/mnt/buzz/subs`) mirrors the curated layout exactly,
  which lets `apply_subtitle_overlay()` walk it with a simple path join —
  no path translation logic needed.
- Per-language search: for a config with `languages: [en, pt]`, each video
  file triggers two independent API searches. The overlay check is per-language,
  so a file that already has `Movie.en.srt` but not `Movie.pt.srt` only
  triggers a Portuguese search.
- Auth flow: API key (header `Api-Key`) is used for all requests. A JWT from
  `/login` is acquired (requiring username and password) once per curator
  process run and is required for downloads.
  Credentials live in `PresentationConfig` via env vars
  (`OPENSUBTITLES_API_KEY`, `OPENSUBTITLES_USERNAME`,
  `OPENSUBTITLES_PASSWORD`).
- Five ranking strategies are supported — `best-match`, `most-downloaded`,
  `best-rated`, `trusted`, `latest` — with an automatic fallback chain:
  chosen strategy → `most-downloaded` → `best-match`. Fallbacks are logged
  clearly so operators know which strategy actually selected the file.
- Release name similarity for `best-match` uses Jaccard token similarity:
  tokenize both the source filename and the subtitle `release` field on
  `.`, `-`, `_`, space, then score `|intersection| / |union|`. This naturally
  matches resolution, codec, and release group tokens without a regex library.
- `SubtitleConfig` is a new Pydantic model nested under `PresentationConfig`.
  It is disabled by default (`enabled: false`) so no API calls are made unless
  the operator explicitly opts in and provides credentials.
- Subtitle fetch is spawned as a background thread in `rebuild_and_trigger()`
  so it does not block the HTTP response for the rebuild trigger caller.
- Two new curator API endpoints expose subtitle state:
  `GET /api/subtitles/status` (fetch progress and errors) and
  `POST /api/subtitles/fetch` (manual trigger for all missing subs).

## Main Quests

- **Config + Models** (`buzz/models.py`, `buzz.dist.yml`, `.env.dist`):
  - Add `SubtitleConfig(BaseModel)` with fields: `enabled`, `api_key`,
    `username`, `password`, `languages`, `strategy`, `filters`
    (`hearing_impaired`, `exclude_ai`, `exclude_machine`),
    `search_delay_secs`, `download_delay_secs`.
  - Add `subtitles: SubtitleConfig` and `subtitle_root: Path` (default
    `/mnt/buzz/subs`) to `PresentationConfig`, sourced from env vars.
  - Add the `subtitles:` block to `buzz.dist.yml` with all defaults shown and
    `enabled: false`.
  - Add env var stubs to `.env.dist`:
    `OPENSUBTITLES_API_KEY`, `OPENSUBTITLES_USERNAME`,
    `OPENSUBTITLES_PASSWORD`, `SUBTITLE_LANGUAGES`, `SUBTITLE_STRATEGY`,
    `SUBTITLE_ROOT`.

- **OpenSubtitles client + strategies** (`buzz/core/subtitles.py`):
  - `OpenSubtitlesClient`: `login()`, `search(title, year, language)`,
    `download(file_id)`, rate-limit handling via `X-RateLimit-Remaining`.
  - `rank_subtitles(results, strategy, filters, source_filename)`: applies HI /
    AI / machine filters then ranks by the chosen strategy; returns the top
    candidate or `None`.
  - `release_similarity(source_name, release_name)`: Jaccard token similarity.
  - `fetch_subtitles_for_library(mapping, config)`: walks the curator mapping,
    checks overlay for existing files per language, orchestrates search →
    rank → download → write to overlay for each missing (file, language) pair.

- **Curator integration** (`buzz/core/curator.py`, `buzz/curator_app.py`,
  `docker-compose.yml`):
  - Add `apply_subtitle_overlay(tmp_root, subtitle_root)` in `curator.py` that
    walks `/mnt/buzz/subs` and creates symlinks into the temp build directory;
    call it inside `build_library()` after movies/shows/anime are built.
  - In `rebuild_and_trigger()`, after the build completes, spawn a background
    thread that calls `fetch_subtitles_for_library` for entries missing an
    overlay file.
  - Add `GET /api/subtitles/status` and `POST /api/subtitles/fetch` to
    `buzz/curator_app.py`.
  - Mount `/mnt/buzz/subs` as a named volume in `docker-compose.yml` for the
    `buzz-curator` service.

- **Tests** (`tests/test_subtitles.py`):
  - Strategy ranking: each strategy with mock API response data, assert correct
    ordering.
  - Release name similarity: known (source, release) pairs with expected Jaccard
    scores.
  - Filter application: HI / AI / machine combinations, assert filtered items
    are excluded.
  - Fallback chain: when a strategy yields no results, the next fallback is used
    and logged.
  - Overlay structure: given a curator mapping fixture, assert that
    `apply_subtitle_overlay` creates symlinks at the correct paths.

- **Logging + UI** (`buzz/templates/torrents.html`):
  - Wire subtitle events through `record_event()` with consistent messages:
    - `"Fetching subtitle for: {title} [strategy={s}, lang={l}]"`
    - `"Subtitle found: {release} (downloads: {n}, rating: {r})"`
    - `"No subtitle found for: {title} [fallback: {from} → {to}]"`
    - `"Subtitle download skipped: rate limit reached (remaining: 0)"`
  - Surface per-file subtitle status in `torrents.html`: a small indicator (has
    subs / missing subs / not attempted) alongside each entry.

## Acceptance Criteria

- With `subtitles.enabled: true` and valid credentials, a curator rebuild
  downloads subtitles for video files that lack an overlay file, and symlinks
  them into `/mnt/buzz/curated`. A subsequent rebuild does not re-download
  existing overlay files.
- With `subtitles.enabled: false` (default), no API calls are made and the
  curator rebuild behaves identically to today.
- Each configured language produces a separate `.srt` file
  (`{stem}.{lang}.srt`) in the overlay.
- The chosen strategy correctly ranks candidates; if it yields nothing, the
  fallback chain is exercised and logged.
- Filters (`hearing_impaired`, `exclude_ai`, `exclude_machine`) are applied
  before ranking; candidates violating a filter never appear in the ranked list.
- The subtitle fetch runs in a background thread and does not delay the rebuild
  HTTP response.
- `/api/subtitles/status` returns current fetch state (idle / running / error
  count / last run timestamp).
- `/api/subtitles/fetch` triggers a fetch for all video files currently missing
  an overlay file.
- All new unit tests pass under `uv run python -m pytest tests/test_subtitles.py -v`.
- Existing tests continue to pass under
  `uv run python -m unittest discover -s tests`.
- `uv run pyright` passes with no new errors.

## Metadata

### id

opensubtitles-integration

### type

Issue
