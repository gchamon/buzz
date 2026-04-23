# Code Quality Improvement — Round 1

## Status

Done

## Outcome

The buzz codebase should be consistently documented, type-annotated, and
lint-clean so that contributors can navigate and modify it confidently.
Opaque `dict[str, Any]` return types that convey no domain meaning should be
replaced with named aliases. All public APIs should carry docstrings. The
project should adopt a standard linter (ruff) and type checker (pyright) with
zero violations as a hard baseline.

## Decision Changes

- **Ruff adopted as the project linter** with `[tool.ruff.lint]` configuration
  in `pyproject.toml`. Rules enabled: pycodestyle (E/W), Pyflakes (F), isort
  (I), pep8-naming (N), pydocstyle (D, Google convention), pyupgrade (UP),
  flake8-bugbear (B), flake8-comprehensions (C4), flake8-simplify (SIM).
  `D100`/`D104` (missing module docstrings in non-public modules) suppressed.
- **Line length set to 79** to match PEP 8, applied uniformly across all source
  files.
- **Named type aliases** instead of bare `dict[str, Any]` for domain objects in
  `state.py`. The Python 3.12 `type` statement is used (project requires 3.14):
  `SnapshotNode`, `Snapshot`, `TorrentInfo`, `TorrentSummary`, `SyncReport`,
  `StatusReport`, `ChangeClassification`, `OperationResult`.
- **`PresentationConfig.__init__` refactored to `PresentationConfig.load()`**
  class method so config loading is explicit and testable without side effects
  in the constructor.
- **`SubtitleConfig.from_raw()` and `SubtitleConfig.from_env()`** class methods
  extracted from `PresentationConfig.__init__` to encapsulate parsing logic
  close to the model it belongs to.
- **`_env_flag()` helper** extracted to `models.py` to de-duplicate the
  `os.environ.get(...).lower() in {"1", "true", "yes"}` pattern.

## Main Quests

- **ruff + pyright setup** (`pyproject.toml`):
  - Add `ruff` and `pytest` to `[dependency-groups] dev`.
  - Add `[tool.pytest.ini_options]` section.
  - Add `[tool.ruff.lint]` with selected rules and Google docstring convention.

- **Module docstrings** — add one-line module docstrings to all source files:
  `__main__.py`, `cli.py`, `models.py`, `dav_app.py`, `curator_app.py`,
  `dav_protocol.py`, `core/state.py`, `core/constants.py`, `core/curator.py`,
  `core/events.py`, `core/media.py`, `core/media_server.py`,
  `core/subtitles.py`, `core/utils.py`.

- **Docstrings for public APIs** — add Google-style docstrings to all public
  classes, `__init__` methods, and functions in every source file. Private
  helpers (`_` prefix) are excluded.

- **Type aliases** (`buzz/core/state.py`):
  - Define `SnapshotNode`, `Snapshot`, `TorrentInfo`, `TorrentSummary`,
    `SyncReport`, `StatusReport`, `ChangeClassification`, `OperationResult`.
  - Apply aliases throughout `LibraryBuilder` and `BuzzState` method signatures.

- **Model refactors** (`buzz/models.py`):
  - Extract `SubtitleConfig.from_raw()` and `SubtitleConfig.from_env()`.
  - Replace `PresentationConfig.__init__` loading logic with
    `PresentationConfig.load()` factory method.
  - Extract `_env_flag()` helper.
  - Add docstrings to all public functions and validator methods.

- **Bug fix** (`buzz/core/state.py`):
  - Rename stale `_add_to_trashcan` call to `_add_to_archive` (missed during
    the trashcan → archive rename in a prior commit).

- **SIM fixes**:
  - `is_probably_media_content_type` in `media.py`: return the set membership
    test directly instead of `if … return True; return False`.
  - `_result_matches_query` in `subtitles.py`: collapse nested `if` into a
    single boolean expression.

- **Import hygiene** (auto-fixed by `ruff --fix`):
  - Remove unused imports (`sys` in `cli.py`, `urllib.error/request` in
    `curator.py`).
  - Sort import blocks to satisfy isort rules across all files.

## Acceptance Criteria

- `uv run ruff check buzz/` reports zero violations.
- `uv run pyright buzz/` reports zero errors.
- `uv run pytest tests/ --ignore=tests/test_buzz.py` passes all 60 tests.
- Every public class, `__init__`, and function in `buzz/` carries a docstring.
- No `dict[str, Any]` appears as a return type on public methods in `state.py`.

## Metadata

### id

code-quality-improve-1

### type

Chore
