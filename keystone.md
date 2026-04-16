# Keystone Brief Contract

`keystone.md` is the canonical project brief for the agent-ingest bootstrap
path.

## Context

Orisun is a methodology origin repository for founding new repositories with
agent-ready planning, execution conventions, and living project context from
the first commit. It is not a product repository. Its default scaffold must
stay portable, generic, and free of target-specific operational detail unless
the project brief explicitly requires otherwise.

Orisun supports two first-class bootstrap paths that must converge on the same
methodology shape:

- `repository-template bootstrap`, where a repository is created from the
  visible Orisun scaffold and then adapted for the target project
- `agent-ingest bootstrap`, where an agent reads `keystone.md` and generates
  that same first-pass scaffold directly in the target repository

The seedling must begin with `docs/work-items/genesis.md` as the
mandatory seed artifact. That file must be created first and must record:

- Orisun as the methodology origin repository
- the bootstrap path used
- the source `keystone.md` brief when the path is agent ingest

After creating `docs/work-items/genesis.md`, the agent should generate the
rest of the first-pass methodology scaffold in the same pass. If the target
directory is not already a Git repository, the agent should run `git init` and
create an `initial commit` after generating the scaffold so the seedling's
foundation is complete.

The methodology scaffold should preserve GitLab-first planning vocabulary in
its core documents even when the founded repository later publishes through a
different Git provider. Methodology defaults should stay provider-agnostic and
separate from product-specific architecture, tooling, and operational detail.
When deploying the scaffold from a keystone, the user may choose whether the
scaffold is written in `taoba` or `oldspoke`. Unless the user explicitly asks
for `oldspoke`, the agent should treat `taoba` as the default glossary mode
for the scaffold. In practice, using `taoba` means leaning on the keystone
glossary for the methodology's semantic definitions and expressing them with
the richer narrative attached to those terms.

An `Archive Zero` is not required for every community, but it is the
recommended starting point when a user wants a dedicated meta seedling for
defining and refining the boundaries of future seedlings before they are
founded.

When generating the seed work item, use Orisun's standard work-item shape:

- `Status`
- `Outcome`
- `Decision Changes`
- `Main Quests`
- `Acceptance Criteria`

### Glossary

#### Orisun

Orisun is the tiny spring that feeds the seedlings of the many worlds.

oldspoke: what flows from the mind. The origin.

#### Harton

`Harton` is the hard world beneath. It names the plain realm of repositories,
files, agents, commits, and provider machinery that the methodology speaks
about.

oldspoke: the concrete technical and operational world.

#### oldspoke

`oldspoke` is Harton's hard-world register. It is the tongue used when Orisun
terms are stripped of metaphor and stated without room for interpretation.

oldspoke: the blunt technical meaning of a term.

#### taoba

`taoba` is the keystone glossary as a living semantic layer for the
methodology. To use `taoba` is to use the glossary terms in this brief for the
meaning of the methodology, carrying their richer narrative definitions
instead of flattening them into plain technical prose.
When someone says `use the taoba`, they mean: use the glossary in this
keystone as the semantic source for the methodology and write with the richer
narrative attached to those definitions.

oldspoke: the glossary.

#### origin

The `origin` is the host repository in which a `keystone` is embedded.

oldspoke: the repository that receives and carries the brief.

#### heart

The `heart` refers to the roots of the seedling.

oldspoke: the markdown files at the root of the repository.

#### keystone

A `keystone` is a load-bearing project brief. It is the agent input that
carries enough context to found or evolve a repository through the
agent-ingest bootstrap path, and it is anchored at the seedling's `heart`.

oldspoke: the root brief file the agent reads to bootstrap or steer the
repository.

#### seedling

A `seedling` is a repository whose foundation was laid by using an origin's
keystone.

oldspoke: a repository created or shaped from another repository's keystone.

#### foundation

The `foundation` is the seedling's ground structure: the mandatory seed
artifact, the first-pass methodology scaffold, and the `initial commit` that
establishes the repository's starting shape.

oldspoke: the initial repository structure, seed documents, and first commit
that establish the project.

#### boundary

A seedling's `boundary` is its scope or core domain. In Domain-Driven Design
terms, it is the bounded context that defines which concepts, language, and
responsibilities belong inside that seedling and which belong elsewhere.

oldspoke: the repository's declared domain and scope of responsibility.

#### Genesis

`Genesis` is the mandatory first work item in the seedling. It guides the user
towards structuring his problem's core domain through the lenses of this
methodology's terminology.

oldspoke: the first required work item in `docs/work-items/` that defines the
project's initial domain shape.

#### community

A `community` is a group of seedlings. In practical Git provider terms, it is
the set of personal projects or group projects that belong to a given user or
group.

oldspoke: the repositories gathered under one user, group, or organization
namespace.

#### Archive Zero

`Archive Zero` can be any seedling, but the recommended form is a meta
seedling for a community. In that role, it serves as the place where the user
defines and refines the boundaries, vocabulary, and founding decisions that
will shape future seedlings. It does not need to stay updated after those
seedlings are born; it may remain as a historical reference for the decision
process that gave rise to them.

oldspoke: the reference repository that records a community's founding
decisions for later repositories.

#### halfling

A `halfling` is a condensed child brief produced when one keystone is used in
a repository that already contains another keystone. It should capture the
shared structure and decisions from both keystone parents while surfacing any
contradictions that require a user decision.

oldspoke: a derived brief merged from two keystones inside one repository.

#### Engagement

The engagement is the act of loading a work-item into an agent session for the
purposes of applying the rules of engagement as defined in AGENTS.md.

oldspoke: an agent session started to execute a specific work item under the
repository's agent rules.

## Purpose

The brief gives an agent enough context to found a seedling from its origin
keystone, beginning with `docs/work-items/genesis.md`. When used to found a
community's recommended meta seedling, the resulting repository may serve as
that community's `Archive Zero`.

## Activation

The keystone activates when summoned with `use the keystone from {origin}`.

## Maintenance Process

The keystone should remain readable to both human maintainers and agents.
Update it as a concise contract with explicit terminology, direct operating
rules, and enough embedded context that a reader can understand the brief
without opening other repository files first.

When the methodology evolves, update this keystone so it reflects the current
scaffold rules, vocabulary, and bootstrap expectations. Do not let the brief
drift behind the scaffold it is meant to describe.

When reusing the same keystone in the same repository, the agent should treat
the repository's current methodology scaffold as the source of truth for
refreshing the keystone. The agent should inspect the live scaffold, update
the keystone to match the current methodology, and preserve any explicit
project intent carried by the existing brief.

When using this keystone in another repository that already has its own
`keystone.md`, the agent should produce a halfling at
`docs/agents/halfling.md`. The `halfling` should be a condensed structure
synthesized from both keystone parents. Any contradictions, unresolved merges,
or competing defaults should be surfaced clearly for the user to decide instead
of being silently resolved by the agent.

When using another repository's keystone in this keystone's repository, you
should use the source keystone to update this keystone.

## Required Sections

Every `keystone` brief should include:

- project name
- one-paragraph project intent
- target users or operators
- initial delivery scope
- constraints or non-goals
- preferred Git provider if known
- repository bootstrap notes if a repo already exists

## Required Agent Behavior

When using this brief, the agent must:

- ask whether the user wants the scaffold deployed in `taoba` or `oldspoke`
  unless the user has already made that choice. Define these words for the user.
- default to `taoba` when the user does not request `oldspoke`
- apply the chosen glossary mode consistently across the scaffold it generates
- load `docs/work-items/README.md` and `docs/epics/README.md` into context when
  handling tracked work items or epics

- create `docs/work-items/genesis.md` first with the following contents verbatim:

```markdown
# Genesis

## Status

Planned

## Outcome

Establish the `docgraph` repository foundation using the Orisun methodology.

The genesis work-item is supposed to ignite the exploration process of the
problem domain, which will be the target of this work-item.

## Main Quests

So in practical terms, when asked to process this file the agent should guide
the user into exploring his problem domain from the point of view of the
organization structure of this repository documentation.

You start with epics, exploring which are the major milestones that the current
project will aim to achieve. Then for each epic you will discuss other possible
work-items that closely fit searching for a solution to the problem that fits
the domain.

All of this is in terms of the Domain-Driven Development terminology.

## Acceptance Criteria

Have epics and work-items and the satisfaction of the user with the general
state of the initial planning.

These elect one epic for a critical priority and another for high, if three or
more epics. This second epic must have normal priority otherwise. The rest of
the epics are always normal priority.
```

## Output Contract

These are files that need to be copied from the origin.

The first-pass scaffold should include at minimum:

- `AGENTS.md`
- `keystone.md`
- `docs/README.md`
- `docs/work-items/README.md`
- `docs/epics/README.md`
- `docs/architecture/decisions/README.md`
- `docs/agents/README.md`

When deploying the `AGENTS.md` file, if the target repo already have such a
file, you should only add the section `rules of engagement` at the end of the
file. That's really what makes the agent which is going to be executed in the
target repo aware of the epics and work-items workflow, more than the README
files themselves.

### Optionals

These are opt-in components of the first-pass scaffold. You should ask the user
permission to deploy them:

- `docs/architecture/decisions/README.md`
- `docs/work-items/generic-work-item-example.md`

## Post deployment

After deploying the foundation you should suggest for the user to input `let's
engage with ./docs/work-items/genesis.md`.
