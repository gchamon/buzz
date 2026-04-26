# Typed Interface Boundaries

## Status

planned

## Outcome

The codebase should avoid meaningless type annotations such as `Any` at
module boundaries. Places where one component depends on another component's
shape should use explicit protocols, typed aliases, or concrete domain models
so pyright can verify the contract and readers can see what methods and fields
are required.

## Decision Changes

- **Replace structural `Any` dependencies with protocols.** For example, the
  TLS HTTP companion app should not accept `dav_owner: Any`; it should depend
  on a named protocol that exposes only the ASGI app plus health/readiness
  methods it needs.
- **Prefer domain aliases over raw `dict`/`Any`.** Existing event, config,
  status, and API payload shapes should get named aliases or models when they
  cross module boundaries.
- **Keep local dynamic data local.** `Any` remains acceptable only at true
  ingestion boundaries or narrow casts where external libraries provide
  incomplete typing.

## Main Quests

- Audit `buzz/` for `Any`, unparameterized `dict`, unparameterized `list`, and
  untyped callables in public or cross-module signatures.
- Introduce small protocols for component contracts, starting with DAV/UI
  ownership used by the TLS HTTP companion app.
- Replace broad payload types in event/config/status helpers with named type
  aliases or Pydantic models where the shape is stable.
- Tighten pyright coverage by removing avoidable casts after protocols and
  aliases are in place.
- Add focused tests only where type tightening reveals behavioral ambiguity.

## Acceptance Criteria

- No public or cross-module function signature uses `Any` when the required
  shape is known.
- The TLS HTTP companion app has a typed owner protocol instead of `Any`.
- `uvx pyright buzz tests` remains clean.
- The cleanup does not introduce broad behavioral refactors.
