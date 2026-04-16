# Work Items

Work items define executable changes and preserve the reasoning needed to make
those changes durable.

## Standard Shape

Each work item should use:

- `Status`
- `Outcome`
- `Decision Changes`
- `Main Quests`
- `Acceptance Criteria`

## Naming

While the full Orisun convention is `{epic-name}-{work-item-number}-{work-item-name}.md`, buzz currently uses the simplified `{work-item-name}.md` because epics are deferred ([ADR 0001](../architecture/decisions/0001-defer-genesis-and-epics.md)). 

When epics are introduced, files should be renamed to adopt the full convention while preserving the stable `id` in metadata.

## Metadata

Work items may include a `## Metadata` section when they need to override
defaults or preserve important planning attributes.

- `id` is required for tracked work items and must remain stable across renames
  and moves
- `type` defaults to `Issue` when omitted
- supported explicit values are `Issue`, `OKR`, and `Test case`
- each metadata key should be a `###` heading under `## Metadata`

Example:

```md
## Metadata

### id

expose-logs-in-the-interface

### type

Issue
```

## Style

The work-item should tell a story. Each section should introduce the user to
the concepts required and use technical prose to instruct the user. The only
exception is in tasks, which will be either in the format of simple lists, or
subsections if these tasks information need to have more structure.

## GitLab Mapping

Orisun keeps GitLab-first planning vocabulary as the default methodology
glossary mapping:

- work items correspond to GitLab work items
- main quests and side-quests correspond to GitLab tasks
- `OKR` corresponds to GitLab OKRs
- `Test case` corresponds to GitLab test cases

Currently, work items carry no parent-epic field as epics are deferred.

## Status Convention

If a work item includes `## Status`, use short prose values such as:

- `backlog`
- `planned`
- `doing`
- `done`
- `cancelled`: the work item no longer makes sense because priority or focus
  changed
- `abandoned`: the work item still makes sense, but the repository will not
  spend resources on it

`killed` is reserved for GitLab graveyard history when a managed work item is
removed from the repository. Do not write `killed` in repo work-item markdown.

If status is omitted, treat the work item as `backlog`.

## Migration

Older repositories founded before stable IDs were introduced should add a
stable `id` to each tracked work item. Keep the ID stable even if the file is
renamed or moved.

## Reference

New work items should follow the Standard Shape above; see the two existing tracked items in this directory for a reference.
