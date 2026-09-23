# Changelog

## 2.4.0 - 2026-09-22

- Delete browser_screenshot.png
- Add parse_regex override, passthrough for unparsed files, and anime show-style layout
- update deps
- update UI props
- Fix curator producing duplicate/stale curated folders
- improve morphdom IDs still need to fix duplicate phantom div in series metadata mgmt view
- realdebrid IDs now also prefixed with rd:
- Support pending debird cache adds and category config
- feat: local provider
- feat: reseed support
- docs: add work items
- fix CVEs and gate container publishing
- download image artifact for container scanning
- docs: timestamp work items
- wip: durable magnet intake flow
- fix: file selection confirmation stuck after RD 204 page
- move Add to Cache into cache intake provider row
- space-between detail rows and group cache provider control
- fallback to next provider on RD infringing_file; auto-expand files-ready intake
- feat: expose deployment identity and uptime in health endpoints
- docs: regenerate changelog from derivation script
- feat: cache intake retry and removal actions
- docs: derive version only on work-item closure
- fix: unify cache metadata row class; make non-ready intake rows non-expanding
- refactor: rename intake to ingest; forward-migrate schema to version 15
- refactor: squash intake-to-ingest migration into direct v14 schema
- fix: fail file selection confirmation loudly with entry-scoped logs
- fix: remove files-ready ingest rows whose upstream torrent disappeared
- fix: keep downloaded cache rows at the bottom of the cache table
- update dependencies to fix vulnerabilities found by ci

Previous baseline: 2026-06-16 01:38:46 +0000 (last explicit version bump).
