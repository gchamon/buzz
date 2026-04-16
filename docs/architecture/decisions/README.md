# Architecture Decisions

This directory holds durable decisions about the methodology scaffold and the seedling structure.

## Decision Standard

Record an ADR when a choice is:

- structural
- likely to affect future work
- worth preserving beyond a single work item

ADR filenames should follow:

`NNNN-short-decision-title.md`

where `NNNN` is a zero-padded sequence number.

Use this standard structure unless a work item explicitly adopts a different
format:

- `Status`
- `Context`
- `Decision`
- `Alternatives Considered`
- `Rationale`
- `Consequences`

## Scope

This directory carries methodology decisions only; product-scope architecture lives in `docs/architecture.md`. Reference ADR 0002 of Orisun as the lineage for that boundary.

## Reference

See [0001-defer-genesis-and-epics.md](0001-defer-genesis-and-epics.md) as the canonical local ADR.
