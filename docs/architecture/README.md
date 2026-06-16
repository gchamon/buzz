# Architecture Documentation

This directory contains maintainer-facing architecture documentation for buzz.

## Documents

- [system.md](system.md): product-scope system architecture, runtime topology,
  service boundaries, state model, and deployment architecture.
- [subsystems.md](subsystems.md): internal state subsystems, especially the
  `BuzzState` responsibilities that coordinate provider polling, task
  execution, hooks, streams, and operator-visible state.
- [glossary.md](glossary.md): project-specific terminology — UI pages, task
  kinds and statuses, hook phases, provider error codes, cache and archive
  concepts, event codes, and configuration vocabulary.
- [decisions/](decisions/README.md): architecture and methodology decision
  records.

Keep operator setup instructions in the root [README](../../README.md). Keep
work tracking notes in [work-items](../work-items/README.md).
