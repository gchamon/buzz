# Config UI

## Status

done

## Outcome

An operator using the buzz seedling should be able to inspect the effective
configuration and adjust non-secret settings directly from the buzz-dav UI,
without SSH access to the host or manual YAML editing. The system writes
changes to a separate `buzz.overrides.yml` file — `buzz.yml` is never modified
at runtime — and deep-merges overrides onto the base config at startup. After
saving, the UI shows a restart-required banner and the operator can trigger
a restart from the same surface.

The config page has two sections: a read-only YAML view of the effective
(merged) configuration with secrets masked, and a form with editable fields
grouped by category. Fields classified as secrets (`provider.token`,
`subtitles.opensubtitles.api_key`, `subtitles.opensubtitles.username`,
`subtitles.opensubtitles.password`) are replaced with `***` in the YAML view,
excluded from the form, and stripped from any POST body — secrets remain
file-only.

## Decision Changes

- **Override file location**: `{state_dir}/buzz.overrides.yml` (inside
  `./data`, already mounted rw in `docker-compose.yml`). Configurable via
  `BUZZ_OVERRIDES` env var; when empty, defaults to
  `{state_dir}/buzz.overrides.yml`.
- **Override file format**: same nested YAML structure as `buzz.yml`,
  containing only the fields the operator has changed. Deep-merged onto the
  base config at load time (overrides win).
- **All changes require restart**: no hot-reload. Saving writes the file and
  shows a banner with the existing `triggerRestart()` mechanism. Docker's
  `restart: unless-stopped` policy brings the service back up.
- **Secret fields are display-only**: masked in the YAML view, absent from the
  form, and silently stripped from any API POST body. Operators manage secrets
  by editing `buzz.yml` on the host.

## Main Quests

- **Backend: override loading and deep merge** (`buzz/models.py`):
  - Add `deep_merge(base: dict, overrides: dict) -> dict` — recursive merge
    where override values win; nested dicts are merged, not replaced wholesale.
  - Refactor `DavConfig.load()` to support a second layer: load `buzz.yml` as
    the base dict, load `buzz.overrides.yml` (if it exists) as the overrides
    dict, deep-merge, then construct `DavConfig` from the merged dict.
    Existing field-extraction logic stays unchanged.
  - Store `_overrides_path` (resolved `Path`) and `_raw_merged` (the nested
    dict before field extraction) on the instance so the API and template can
    use them.
  - Add `to_nested_dict(config: DavConfig) -> dict` — reconstructs the nested
    YAML structure from flat model fields (reverse of `load`).
  - Add `mask_secrets(d: dict) -> dict` — replaces secret values with `"***"`.
  - Add `save_overrides(overrides: dict, path: Path)` — validates override
    keys against the known schema, writes YAML atomically.

- **API endpoints** (`buzz/dav_app.py`):
  - `GET /api/config` — returns
    `{"effective": <nested dict, secrets masked>, "overrides": <current overrides dict>}`.
  - `POST /api/config` — receives `{"overrides": <dict>}`, strips secret keys,
    validates, writes to `buzz.overrides.yml`, returns
    `{"status": "saved", "restart_required": true}`.
  - `GET /config` — renders `config.html`.
  - Add `_config_page()` renderer following the existing pattern
    (`_torrents_page`, `_logs_page`, etc.).

- **Config page template** (`buzz/templates/config.html`):
  - Standalone HTML following the existing template pattern (duplicated nav,
    same head/meta/scripts structure).
  - Nav bar with `config` link marked `active`.
  - Same meta bar (torrents count, last sync, state, ready indicator) as other
    pages.
  - Effective config section: `<pre>` block showing the merged config as YAML
    with secrets replaced by `***`, server-rendered.
  - Edit overrides section: form with `<fieldset>` groups:
    - **Polling**: `poll_interval_secs` (number).
    - **Server**: `bind` (text), `port` (number).
    - **Hooks**: `on_library_change` (text), `curator_url` (text),
      `rd_update_delay_secs` (number).
    - **Compatibility**: `enable_all_dir` (checkbox),
      `enable_unplayable_dir` (checkbox).
    - **Directories / Anime**: `patterns` (textarea, one pattern per line).
    - **Request**: `request_timeout_secs` (number), `user_agent` (text),
      `version_label` (text).
    - **UI**: `ui_poll_interval_secs` (number).
    - **Logging**: `verbose` (checkbox).
    - **Subtitles**: `enabled` (checkbox), `fetch_on_resync` (checkbox),
      `languages` (text, comma-separated), `strategy` (select), `filters`
      sub-fields (`hearing_impaired` select, `exclude_ai` checkbox,
      `exclude_machine` checkbox), `search_delay_secs` (number),
      `download_delay_secs` (number).
  - Save button and restart banner (hidden until save succeeds).

- **Nav link update** (all templates):
  - Add config nav link after `logs` in `torrents.html`, `logs.html`, and
    `trashcan.html`.

- **CSS additions** (`buzz/static/buzz.css`):
  - `.config-yaml` — pre block with border, background, scrollable, consistent
    with the Dracula theme.
  - `.config-section` — fieldset styling consistent with existing section
    patterns.
  - `.config-form` — label/input pair layout.
  - `.restart-banner` — orange/yellow warning banner.
  - Config-specific input styling (number, select, textarea).

- **JS additions** (`buzz/static/buzz.js`):
  - `saveConfig()` — collects form field values, builds a nested overrides
    dict, POSTs to `/api/config`, on success shows the restart banner.
  - Restart uses the existing `triggerRestart()`.

- **Tests** (`tests/test_buzz.py`):
  - `test_deep_merge` — nested overrides, empty overrides, additive keys.
  - `test_config_load_with_overrides` — `DavConfig.load()` picks up overrides.
  - `test_config_load_without_overrides` — `DavConfig.load()` works unchanged
    when no overrides file exists.
  - `test_mask_secrets` — secret paths are replaced with `***`.
  - `test_get_api_config` — returns masked secrets in effective config.
  - `test_post_api_config` — strips secrets, writes file, returns correctly.
  - `test_post_api_config_strips_secrets` — secret keys in POST body are
    silently ignored.

## Acceptance Criteria

- Navigating to `/config` shows the effective merged config as read-only YAML
  with secrets masked as `***`.
- Form fields are pre-populated with current effective values.
- Saving writes only the changed fields to `buzz.overrides.yml`; `buzz.yml` is
  never modified.
- Secret fields cannot be set via the form or the API.
- After save, a restart banner appears; clicking restart triggers
  `POST /api/restart`.
- On next startup, overrides are deep-merged onto the base config and take
  effect.
- All existing tests pass; new tests cover override loading, merge, secret
  masking, and config API endpoints.
- `npx htmlhint "buzz/templates/*.html"` passes with no errors.
- The config nav link appears on all pages.

## Metadata

### id

config-ui

### type

Issue
