# Agent Guide

## Rules of engagement

This section provides instructions for how to behave when engaging with the
implementation of work item tasks.

These rules are engaged when starting a session with `engage with
{docs/work-items/[work-item-name].md}` or similar, where the `[work-item-name]`
pattern is documented in [the work items readme](./docs/work-items/README.md).

When asked to engage you should always enter plan mode if not already.

### Restrictions

- Never update a work item that is already completed
  - You can only update a decision changes in the output section
  - the output's decision changes should be reserved exclusively for necessary
  changes during the implementation of the work-item that needed to be
  reflected after the work-item went to done
  - these decision changes must be actual design decisions or changes in
  implementation direction, not delivery details, completion summaries, test
  coverage notes, or other descriptions of what was built

### Preparation phase

First always use the start of the session to ground yourself in the context of
the work item. You are free to pull data from ~/.codex/sessions/ whenever
necessary, but always ask when doing so because this can be token-intensive.

When handling work items, always include `docs/work-items/README.md` in the working context. Note that epics are not used in the buzz seedling's current phase (see ADR 0001).
You are free to pull data from ~/.codex/sessions/ whenever necessary, but
always ask when doing so because this can be token-intensive.

### Execution phase

The execution phase will go on until the user is satisfied with reviewing the
changes, before then the agent and the user are going to iterate on the
implementation.

It's important to always consider if there is need for a final pass over the
work item's acceptance criteria before exiting the Execution phase to catch
anything that was overlooked.

### Post phase

In the post phase of implementing a work item, propagate changes in the design
and decisions to the next work item in the sequence, if there are any, in which
case these changes have to be added to the last work item under `Decision
changes` section.

Never mark a work item as completed without first checking that all of its main
quests, side-quests that were taken on as part of the implementation, and exit
criteria were actually fulfilled. If any accepted quest or exit criterion is
still open, do not mark the work item as complete; leave it in an appropriate
non-complete status and record the remaining work explicitly.
