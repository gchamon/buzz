# `buzz.providers`

Concrete upstream provider clients and the contract they implement.

Buzz is provider-neutral: `BuzzState` and the streaming code only ever talk to
the `ProviderClient` protocol, never to a specific provider's API. The protocol
and its normalized data types are defined in
[`../core/providers.py`](../core/providers.py); the concrete clients live here.

For how state uses these clients (priority ordering, sync, caching) see
[`../core/README.md`](../core/README.md) and
[`../../docs/architecture/subsystems.md`](../../docs/architecture/subsystems.md).

## The `ProviderClient` contract

`ProviderClient` ([`../core/providers.py`](../core/providers.py)) is a
`typing.Protocol`. An implementation declares its `kind` and provides seven
methods:

| Member | Contract |
| --- | --- |
| `kind: ProviderKind` | Literal identifying the provider (`"real_debrid"`, `"torbox"`). |
| `list_torrents()` | Return normalized `ProviderTorrentSummary` list. |
| `get_torrent(torrent_id)` | Return a normalized `ProviderTorrentDetail`. |
| `add_magnet(magnet)` | Add a magnet, return the provider torrent id. |
| `select_files(torrent_id, file_ids)` | Select files for download (no-op if unsupported). |
| `delete_torrent(torrent_id)` | Delete the torrent from the account. |
| `fetch_details(torrent_ids, on_progress=None)` | Batch-fetch details, reporting progress per network call. |
| `resolve_stream(stream_ref)` | Resolve a provider stream ref to a direct download URL. |

### Normalized data types

All providers return the same frozen dataclasses so the rest of the codebase is
provider-agnostic:

- `ProviderFile` — `id`, `path`, `bytes`, `selected`, `stream_ref`.
- `ProviderTorrentSummary` — `id`, `name`, `bytes`, `progress`, `status`,
  `ended`, `stream_refs`.
- `ProviderTorrentDetail` — adds `hash`, `original_name`, `added`, and a tuple
  of `ProviderFile`.

Statuses are normalized via the module's `_status()` helper to a small set
(`downloaded`, `error`, …).

### IDs and errors

Torrent ids are prefixed `provider:id` so multiple providers coexist in one
state space. `split_provider_torrent_id(torrent_id)` splits a prefixed id, and
defaults to `real_debrid` when no prefix is present.

Implementations raise:

- `ProviderDeleteError(status_code, text, attempts)` — delete failure with HTTP
  status metadata.
- `ProviderStreamError(stream_ref, code)` — stream resolution failure.

## Existing providers

| Class | File | Notes |
| --- | --- | --- |
| `RealDebridProviderClient` | [`real_debrid.py`](real_debrid.py) | Backed by the `rdapi` `RD()` client. Paginates `list_torrents` (100/page), retries transient failures (3 attempts), resolves streams via the unrestrict/link endpoint, and treats the "already selected" API error as idempotent. |
| `TorBoxProviderClient` | [`torbox.py`](torbox.py) | HTTP client against `api.torbox.app` with bearer-token auth. Caches the torrent list for performance, retries `DATABASE_ERROR` on delete, and implements `select_files` as a no-op (TorBox selects all files). |

Both are re-exported from [`__init__.py`](__init__.py).

## Adding a new provider

Using the existing TorBox wiring as the template:

1. **Implement the protocol.** Add `buzz/providers/<name>.py` with a class that
   satisfies every `ProviderClient` method and sets `kind = "<name>"`. Map the
   upstream API into the normalized dataclasses; raise `ProviderDeleteError` /
   `ProviderStreamError` on failure.
2. **Export it.** Import the class in [`__init__.py`](__init__.py) and add it to
   `__all__`.
3. **Widen the type.** Add `"<name>"` to the `ProviderKind` literal in
   [`../core/providers.py`](../core/providers.py).
4. **Wire it into `DavApp`.** In [`../dav_app.py`](../dav_app.py):
   - In `_build_provider_clients`, add a guard in the `provider_priority` loop
     that skips your provider when it is disabled or has no token.
   - In `_build_provider_client`, add a branch
     `if provider == "<name>" and config.<name>_enabled and config.<name>_token:`
     that constructs and returns the client (mirror the existing `torbox`
     branch).
5. **Extend config.** In [`../models.py`](../models.py) add
   `<name>_enabled: bool` and `<name>_token: str` to `DavConfig`, and include
   `"<name>"` in the `provider_priority` default tuple
   (currently `("real_debrid", "torbox")`).
6. **Surface it in the UI.** Add the new config fields to the config view in
   [`../ui_live.py`](../ui_live.py) and
   [`../pyview_templates/config_live.html`](../pyview_templates/config_live.html)
   so operators can enable it and enter a token.
