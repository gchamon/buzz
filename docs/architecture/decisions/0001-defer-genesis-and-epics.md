# ADR 0001: Defer Genesis Seed And Epics In The Buzz Seedling

## Status

Accepted

## Context

Buzz is a pre-existing product seedling (Real-Debrid WebDAV bridge for
Jellyfin and Plex) founded from the Orisun origin through the agent-ingest
bootstrap path. The Orisun keystone contract mandates two artifacts that make
sense for greenfield seedlings but do not earn their complexity here:

- `docs/work-items/genesis.md`, whose purpose is to ignite problem-domain
  exploration for a new project. Buzz's problem domain is already expressed
  in code, `README.md`, and `docs/architecture.md`; there is no domain to
  explore from scratch.
- `docs/epics/` and the epic-number naming convention, which assume a
  planning surface with enough work items to justify grouping. Buzz currently
  has two tracked work items. Introducing an epic layer now would add a
  coordination artifact that immediately carries less content than the work
  items it groups.

## Decision

- The buzz seedling deploys the Orisun scaffold **without**
  `docs/work-items/genesis.md` and **without** `docs/epics/`.
- Work-item filenames follow the shorter convention `{work-item-name}.md`
  instead of `{epic-name}-{work-item-number}-{work-item-name}.md`.
- `keystone.md` remains verbatim from the Orisun origin so the seedling can
  re-engage against a future methodology refresh; the buzz-local scaffold is
  authoritative where it diverges from the verbatim contract (keystone
  lines 207-212 explicitly allow this).

## Alternatives Considered

### 1. Follow the keystone strictly and create a retroactive genesis

Rejected because a genesis written after the product already exists becomes a
ceremonial document that duplicates the product README without adding
decisions. The methodology's value comes from genuine exploration, not from
filling in the slot.

### 2. Create a single holding epic (for example `planning`) to satisfy the scaffold

Rejected because an epic whose only justification is to host the current
work items adds a layer with no coordination content, and encourages the
next contributor to treat epics as mandatory folders rather than as real
planning coordination artifacts.

## Rationale

The keystone expects methodology to serve the seedling, not the other way
around. Applying the full contract to a mature product repository would
produce boilerplate that dilutes rather than strengthens the planning
surface. Deferring genesis and epics keeps the scaffold honest about what
buzz currently needs.

## Consequences

- `docs/work-items/README.md` documents the simplified naming convention and
  points readers to this ADR when they notice the absent epic prefix.
- `AGENTS.md` rules of engagement reference only `docs/work-items/README.md`,
  not `docs/epics/README.md`, to avoid instructing agents to load a file that
  does not exist.
- When the work-item count grows to the point where coordination requires
  grouping, a future work item should introduce epics, retroactively rename
  existing work items to include an epic prefix while preserving their
  stable `id` metadata, and supersede this ADR.
- The keystone's verbatim text still describes genesis and epics. That
  divergence between the contract and the living scaffold is intentional and
  documented; the scaffold is the source of truth for buzz's day-to-day
  methodology.
