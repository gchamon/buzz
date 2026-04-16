# Agents

This directory contains reusable agent inputs and conventions for operating on the buzz seedling.

## Methodology Core

- [../../keystone.md](../../keystone.md): the heart-level brief contract.

## Operating Rule

Agent-ingest engagements read the heart-level `keystone.md` and produce or refresh scaffold per the contract. 

**Explicit Adaptation:** Buzz's current scaffold defers genesis and epics ([ADR 0001](../architecture/decisions/0001-defer-genesis-and-epics.md)). The operating rule the agent follows is to load `docs/work-items/README.md` into context when handling work items, not the epic README.

The generated scaffold preserves GitLab-first planning vocabulary even when the founded repository later publishes through another Git provider.
