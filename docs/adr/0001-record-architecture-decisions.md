# ADR 0001: Record Architecture Decisions

## Status

Accepted | 2026-05-04

## Context

SHIPS has accumulated a non-trivial set of architectural decisions —
the `{{TOKEN}}` syntax, eponymous filenames, `_T`/`_V` database
separation, deployer-owned idempotency, package-level rollback via
pre-flight snapshot, grants being forward-only on rollback, and others.

The reasoning behind each of these decisions currently lives in:

- Conversation transcripts that compact and drift over time
- Commit messages, which capture *what* changed but rarely *why*
- `Claude_Coding_Discipline.md`, which records the current state of
  rules but not the history of how those rules came to be
- The author's working memory

This is sustainable while the project has one or two contributors and
the decisions are recent. It will not survive scaling to a wider team,
or even six months of memory drift on the original author. When someone
asks "why did SHIPS use double-brace tokens instead of `${VAR}` like
every shell script in the world?" the answer needs to be findable
without archaeology through chat history.

## Decision

SHIPS will record architecture decisions as **Architecture Decision
Records (ADRs)** in `docs/adr/`, following the format established by
this ADR.

Each ADR is a short Markdown file capturing:

- **Status** — Proposed, Accepted, Superseded, Deprecated
- **Context** — the forces and constraints that made the question matter
- **Decision** — the choice made, in plain language
- **Consequences** — positive, negative, and neutral effects
- **Alternatives considered** — paths not taken, with brief reasoning
- **References** — links to commits, related ADRs, transcripts

The following disciplines apply:

1. **Numbered, never renumbered.** Each ADR has a permanent four-digit
   number assigned at creation time. Numbers are never reused or
   reordered, even when an ADR is superseded.

2. **Status changes; content does not.** When a decision is reversed,
   a *new* ADR is written that supersedes the old one. The old ADR's
   status changes to `Superseded by ADR NNNN`, but its body is not
   edited. The decision log is append-only.

3. **Stored in the repo.** ADRs version with the code. When `git blame`
   points to a commit that introduces or removes a rule, the commit
   message references the relevant ADR number.

4. **Short.** One page per ADR is the target. If a decision needs more
   than that to explain, it probably contains two decisions and should
   be split.

5. **Written when the decision is made**, not retroactively curated
   months later. (The exception is a one-time backfill of pre-existing
   decisions — see Consequences below.)

## Consequences

**Positive**

- The project's reasoning becomes durable. Future contributors —
  human or AI — can trace any rule back to the trade-off that produced it.
- New decisions are forced to be explicit. The act of writing the ADR
  surfaces alternatives that might otherwise be skipped.
- Rule reversals become visible rather than silent. Superseding ADR
  0009 with ADR 0023 leaves a permanent record of the change.
- `Claude_Coding_Discipline.md` can reference ADR numbers, removing
  the need to inline rationale in the rule list.

**Negative**

- Writing ADRs is friction. Some decisions that would otherwise be
  made in a 5-minute exchange now require 20 minutes of structured
  writing. The tax must feel worth paying, or it won't be paid.
- A backlog of retroactive ADRs (the 7 known decisions listed in this
  ADR's references) needs to be written. This is one-time work but
  non-zero.
- The repo grows a `docs/adr/` directory that contributors must learn
  to navigate.

**Neutral**

- ADRs are written in plain Markdown with no special tooling required.
  An optional `make adr-new TITLE="..."` target may be added to scaffold
  new ADRs, but is not required for the pattern to function.

## Alternatives considered

**Inline rationale in `Claude_Coding_Discipline.md`.** Rejected: the
file would balloon, and superseded reasoning would have to be deleted
(losing history) or kept (cluttering the current spec). ADRs separate
the *what* from the *why*, and let *why* be append-only.

**A single `DECISIONS.md` log file.** Rejected: merge conflicts on a
shared file scale poorly with multiple contributors. One file per
decision is harder to conflict.

**Notion / Confluence / wiki.** Rejected: decisions drift from code.
Architecture lives with the artefact it governs. ADRs in-repo are
versioned, branchable, reviewable through PR.

**No formal record.** This is the status quo and is the problem this
ADR exists to fix.

## References

- Michael Nygard's original blog post, "Documenting Architecture
  Decisions" (2011) — the canonical format this ADR follows.
- ADR 0009 (Configurable deploy_intent rule with audit waiver) — the
  first ADR written under this scheme.
- Retroactive ADRs to be written:
  - ADR 0002: Double-brace token syntax (`{{TOKEN}}` not `${TOKEN}`)
  - ADR 0003: Eponymous filenames after build
  - ADR 0004: `_T` / `_V` database separation for tables and views
  - ADR 0005: Deployer owns idempotency (DDL files use CREATE, not REPLACE)
  - ADR 0006: Package-level rollback via pre-flight snapshot
  - ADR 0007: Grants are forward-only on rollback
  - ADR 0008: `.yaml` extension, not `.yml`
