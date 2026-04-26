# Implement Basic Security

## Status

backlog

## Outcome

The Buzz web UI today serves plain HTTP on a port the operator is expected
to keep firewalled to localhost. That assumption is reasonable for a
single-host home setup but is the only thing standing between the wider
network and a configuration surface that can already mutate state and
trigger restarts. It is also the blocker that keeps secret-bearing fields —
Real-Debrid token, Jellyfin API key, OpenSubtitles credentials — out of the
web UI: exposing a password field over plain HTTP with no authentication
would be worse than the current "edit `buzz.yml` on the host" workflow.

This work item ships the minimum security surface required to safely edit
secrets from the UI: the Buzz web UI is reachable over HTTPS with a
self-signed or operator-supplied certificate, and every UI route plus every
mutating API endpoint requires basic-auth credentials configured by the
operator. Once both are in place, secret fields graduate from "file-only"
to "UI-editable", `buzz.overrides.yml` becomes the canonical home for
operator-set credentials, and follow-up work like `jellyfin-auto-bootstrap`
can write the Jellyfin API key into overrides without leaking it through a
plaintext form.

This work item is intentionally narrow: HTTPS + basic auth, nothing more.
Identity providers, multi-user roles, session management, and TLS
auto-renewal via ACME are all explicitly out of scope and deferred to
follow-ups.

## Decision Changes

- **Basic auth, not session cookies.** Every UI route and every mutating
  API endpoint requires HTTP Basic credentials. The browser handles the
  prompt; Buzz validates against a single operator-configured
  username/password pair. Session cookies, login forms, CSRF tokens, and
  multi-user accounts are deferred — basic auth over HTTPS is the
  minimum-viable surface and avoids inventing a session store.
- **Credentials live in `buzz.overrides.yml` only.** A new top-level
  `security` section accepts `username` and `password_hash` (bcrypt or
  argon2id — pick one and stick with it; argon2id preferred). The plain
  password is never persisted. A small CLI helper
  (`python3 -m buzz.scripts.set_password`) prompts for a password,
  generates the hash, and writes it via `save_overrides`. Env var
  overrides (`BUZZ_AUTH_USERNAME`, `BUZZ_AUTH_PASSWORD_HASH`) follow the
  precedence rules `setup-docs-truth` settles on.
- **HTTPS is mandatory once auth is enabled.** If `security.username` is
  set but no TLS material is configured, the service refuses to start with
  a clear error directing the operator to either provide a cert/key pair
  or generate a self-signed one with the bundled helper. There is no
  "auth-over-plain-HTTP" mode, even as an opt-in.
- **TLS material is operator-supplied with a self-signed fallback.** Two
  paths in `buzz.yml`: `tls.cert_path` and `tls.key_path`,
  both pointing inside the container (typical mount: `./config/tls/`). If
  both are unset, a one-shot helper
  (`python3 scripts/generate_self_signed_cert.py`) writes a 10-year
  self-signed cert/key pair into `data/tls/` on first run, and the server
  uses those. Operators wanting Let's Encrypt or a corporate CA put a
  reverse proxy in front of Buzz; ACME is out of scope.
- **The DAV endpoint stays unauthenticated and on plain HTTP.** Buzz's
  WebDAV surface is consumed by the local `rclone` container over the
  Docker network and has no business being on the public internet. Adding
  auth here would require teaching `rclone` to send credentials and
  re-authenticate on token rotation — out of scope. The DAV port (`9999`)
  remains plain HTTP and is documented as "bind to the Docker network
  only; never publish to a host interface that's reachable from outside."
  The UI moves to a separate port (`9443` for HTTPS) so the two surfaces
  are unambiguously different.
- **Secret fields graduate to UI-editable once this lands.** Update
  `UI_MANAGED_CONFIG_FIELDS` and the config-page form in
  `buzz/dav_app.py` / `buzz/templates/config.html` to expose
  `provider.token`, `media_server.jellyfin.api_key`,
  `media_server.plex.token`, and the OpenSubtitles credentials as
  password-type form inputs. The masking behavior in the YAML view stays
  (still shown as `***`); the form just stops being read-only for these
  fields. This is the user-visible payoff of the work item and should be
  treated as part of "done", not a follow-up.

## Main Quests

- **Auth middleware** (`buzz/dav_app.py`):
  - Add a Starlette middleware (or PyView equivalent) that gates every
    route except `/dav/*`, `/healthz`, `/readyz`, and `/static/*`.
  - On missing/invalid credentials, return `401` with
    `WWW-Authenticate: Basic realm="buzz"`.
  - Constant-time compare on the username; argon2id `verify` on the
    password against `security.password_hash`.
  - When `security.username` is unset, the middleware is a no-op and the
    server logs a single warning at startup ("auth disabled — UI is
    open"). This keeps existing single-host deployments working without a
    forced migration.
- **TLS bootstrap** (`buzz/server.py` or wherever uvicorn is launched):
  - If `tls.cert_path` and `tls.key_path` are set, hand
    them to uvicorn's `ssl_keyfile`/`ssl_certfile`.
  - If both unset and `security.username` is set, abort startup with a
    clear remediation message.
  - If both unset and `security.username` is also unset, run plain HTTP
    on port 9999 (current behavior preserved for the no-auth case).
- **Self-signed cert helper** (`scripts/generate_self_signed_cert.py`):
  - Generates a 10-year RSA-2048 (or ed25519) cert/key pair using
    `cryptography`, writes to `data/tls/buzz.crt` and `data/tls/buzz.key`
    with mode 600, and prints the SHA-256 fingerprint so operators can
    pin it in their browser.
  - Idempotent: refuses to overwrite an existing cert; operator must
    delete to regenerate.
- **Password helper** (`buzz/scripts/set_password.py`):
  - Prompts for username and password (no echo), hashes with argon2id,
    writes to `buzz.overrides.yml` via `save_overrides`. Refuses to run
    if `security.password_hash` already exists unless `--force` is
    passed.
- **Override schema extension** (`buzz/models.py`):
  - Add `security: { username: True, password_hash: True }` to
    `_OVERRIDE_SCHEMA`.
  - Add `tls.cert_path` / `tls.key_path` to the schema and
    to `UI_MANAGED_CONFIG_FIELDS`.
  - `password_hash` is treated as a secret by `_strip_secrets` /
    `mask_secrets` — the YAML view shows `***`.
- **Compose changes** (`docker-compose.yml`):
  - Publish the new HTTPS port (`9443:9443`) on `buzz-dav`.
  - Mount `./config/tls:/app/config/tls:ro` for operator-supplied certs.
  - The existing `9999` DAV port stays unchanged.
- **README updates**:
  - New **Enable HTTPS and login** subsection in Quick Start, placed
    after **Configure Jellyfin**, walking through cert generation, the
    password helper, and the resulting `https://localhost:9443` URL.
  - Update the **Configure Jellyfin** subsection: once auth is enabled,
    secrets *can* be entered through the UI; remove the "API key must
    live in `buzz.yml`" caveat and link to the new subsection.
  - Add `security` and `tls` rows to the configuration reference
    table.
- **Tests** (`tests/test_buzz_security.py`):
  - Unauthenticated requests to UI/API routes return 401 with the
    correct `WWW-Authenticate` header.
  - DAV routes remain unauthenticated.
  - Wrong password returns 401; correct password returns 200.
  - `set_password` round-trip: hash written, `verify` succeeds.
  - Server refuses to start when `security.username` is set but TLS
    paths are not.

## Side Quests

- Document a reverse-proxy recipe (Caddy or nginx) for operators who want
  Let's Encrypt — link from the README rather than shipping ACME in-tree.
- Add a "rotate password" button in the config UI once the basic flow is
  stable. Defer until the password helper is exercised in anger.

## Acceptance Criteria

- A fresh `docker compose up -d` with `security.username` unset behaves
  exactly as today: plain HTTP on `9999`, no auth, single warning logged.
- Running the password helper, providing TLS material (or running the
  self-signed helper), and restarting the stack moves the UI to
  `https://localhost:9443` behind a basic-auth prompt.
- `provider.token`, `media_server.jellyfin.api_key`,
  `media_server.plex.token`, and OpenSubtitles credentials are editable
  through the config UI form once auth is enabled, with values written to
  `buzz.overrides.yml`. The YAML view continues to show `***` for these
  fields.
- The DAV port (`9999`) remains plain HTTP and unauthenticated; rclone
  continues to mount without configuration changes.
- Server refuses to start with a clear error if auth is enabled but no
  TLS is configured.
- All existing tests pass; new tests cover auth gating, DAV pass-through,
  and the TLS-required-with-auth invariant.
- README documents the full enable-security flow end-to-end, including
  the self-signed cert fingerprint and how to trust it in a browser.

## Open Questions

- **Argon2id vs bcrypt**: argon2id is the modern default and ships in the
  `argon2-cffi` package, but bcrypt has wider deployment familiarity.
  Default to argon2id unless dependency footprint is a concern.
- **Per-route exemption list**: should `/healthz` and `/readyz` stay
  open? Yes for healthz (Docker healthcheck consumer); readyz can go
  either way. Decide before implementation.
- **DAV auth in a future iteration**: leaving DAV open is a deliberate
  scope cut, but a follow-up work item should track adding token-based
  auth on the DAV port for operators who do want to publish it. Not
  blocking this work item; flagged so it isn't forgotten.

## Metadata

### id

implement-basic-security

### type

Issue
