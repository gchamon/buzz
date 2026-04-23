# Expose Logs In The Interface

## Status

done

## Outcome

An operator working with the buzz seedling should be able to inspect and follow
the execution logs of the two buzz services — `buzz-dav` and `buzz-curator` —
directly from the buzz UI, and restart either service from the same surface.
Host-level tools (`docker logs`, `docker compose restart`) remain available as
an escape hatch, but the routine operational loop ("what just happened? retry
it.") should live inside buzz so that operators without shell access to the
host can still reason about the stack.

The surface must be selective. The DAV service emits a large volume of
per-request noise (PROPFIND, range reads) that would drown the signals
operators actually care about: poll runs, hook triggers, sync results, curator
rebuild reports, Real-Debrid errors. The UI should expose curated event
categories, not a raw tail of the container log.

## Decision Changes

- Logs are captured in-process through a shared ring buffer, not scraped from
  the container log file. Existing `print(..., flush=True)` and `verbose_log`
  call sites in `buzz/core/state.py`, `buzz/core/curator.py`,
  `buzz/curator_app.py`, and `buzz/dav_app.py` route through a shared
  `record_event` helper so that every operationally meaningful line carries a
  service tag, a level, and a category.
- Restart is service-scoped, not stack-scoped. Restarting the full compose
  stack from inside a container requires either mounting the docker socket or
  a privileged sidecar. Neither earns its complexity for the current boundary.
  Instead, buzz exposes per-service restart by exiting the uvicorn process
  cleanly; Docker's `restart: unless-stopped` policy (already set in
  `docker-compose.yml` for `buzz-dav` and `buzz-curator`) brings each service
  back up. Restarting `rclone`, `jellyfin`, and `plex` from the UI is out of
  scope.
- Sensitive values (Real-Debrid token, Jellyfin/Plex API keys) must never be
  emitted by the event stream. The event helper is the single choke point that
  enforces this.

## Main Quests

- Introduce a shared event-capture module at `buzz/core/events.py` exposing:
  - `record_event(service, level, category, message, **extras)`
  - a thread-safe bounded ring buffer (target: last 2 000 events per service)
  - an async subscription primitive that yields new events to SSE consumers
- Migrate operationally meaningful `verbose_log` and `print(..., flush=True)`
  call sites in `buzz/core/state.py`, `buzz/core/curator.py`,
  `buzz/curator_app.py`, and `buzz/dav_app.py` to emit categorized events.
  Categories: `poll`, `sync`, `hook`, `rebuild`, `jellyfin`, `error`. Per-dav-
  request logs are tagged `protocol` and hidden by default.
- Add UI-backing endpoints:
  - `GET /api/logs` on both services, supporting `?service=`, `?level=`,
    `?category=`, `?since=<event_id>` query filters
  - `GET /api/logs/stream` returning `text/event-stream` for live tailing
  - `POST /api/restart` on both services, gated behind a same-origin check
    and a confirmation token in the request body, that schedules a graceful
    shutdown and lets Docker relaunch the container
- Extend `buzz/templates/torrents.html` with a Logs panel: service selector
  (dav/curator), level filter, category checkboxes, live/paused toggle, and a
  Restart Service button that prompts for confirmation. Lift shared styles
  into `buzz/static/buzz.css` and a small JS module under `buzz/static/`.
- Document the new endpoints, filter grammar, and restart semantics in
  `docs/architecture.md` under a new `Logs and Restart` section, and surface a
  one-line pointer in the root `README.md` Configuration Reference.
- Add tests under `tests/` covering:
  - ring-buffer bounds
  - category/level filtering
  - SSE stream produces new events after a recorded event
  - `/api/restart` returns 202 and triggers process exit (mock `sys.exit`)
  - template renders the logs panel and exposes both service choices

## Acceptance Criteria

- An operator can open the buzz UI and see a live, filterable event stream
  for both `buzz-dav` and `buzz-curator` without tailing container logs.
- Enabling `logging.verbose: true` in `buzz.yml` expands the visible categories
  but does not remove the filter controls.
- Triggering Restart Service from the UI causes the selected container to
  exit and Docker to relaunch it within its existing healthcheck window; the
  event stream clearly records the intent, the exit, and the post-restart
  reconnection.
- Sensitive values (RD token, Jellyfin/Plex API keys) never appear in the
  event stream even in verbose mode.
- New unit/integration tests pass under
  `uv run python -m unittest discover -s tests`.

## Metadata

### id

expose-logs-in-the-interface

### type

Issue
