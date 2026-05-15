# ADR 0009: `deploy_intent` Rule — WARNING Default, REPLACE Permitted

## Status

Accepted | 2026-05-15

_Supersedes the earlier "Proposed" draft of this ADR, which described
a waiver-machinery approach that was not implemented._

## Context

ADR 0006 established that DDL source files should prefer `CREATE` and
that the deployer owns idempotency via pre-flight snapshot and
DROP+CREATE or REPLACE_IN_PLACE strategies.  A `deploy_intent` Inspect
rule was introduced to enforce this at ERROR severity — any `REPLACE`
verb in a source file blocked packaging.

In practice this proved a significant barrier to adoption.  The key
facts that changed the analysis:

1. **REPLACE is not valid for tables in Teradata.**  `REPLACE` applies
   only to views, procedures, macros, functions, and triggers.  There
   is no risk of accidental table data loss from permitting `REPLACE`
   in source files.

2. **The deployer already handles REPLACE safely.**  The
   `_deploy_replace_in_place` strategy in `deployer.py` captures the
   existing object definition via `SHOW` *before* executing the
   `REPLACE`, storing it in `_rollback/`.  Rollback coverage is
   therefore identical whether the source verb is `CREATE` or
   `REPLACE`.

3. **Adoption cost was too high.**  Many Teradata developers have
   years of muscle memory for `REPLACE VIEW`, `REPLACE PROCEDURE`,
   `REPLACE MACRO`.  Requiring mass remediation of existing codebases
   before they could adopt SHIPS is an unreasonable barrier.

The previous draft of this ADR proposed a waiver-machinery approach
(a `discipline_waiver` log in `decisions.json`, a `ships normalise`
rewrite tool, a `ships.yaml` discipline section).  This was rejected
as over-engineered for the actual problem: the rule's severity was
simply too high given that the deployer already provides equivalent
safety for both verbs.

## Decision

The `deploy_intent` rule default is changed from **ERROR** to
**WARNING**.

1. **REPLACE is permitted.**  A DDL source file may use either
   `CREATE` or `REPLACE` for replaceable object types (views,
   procedures, macros, functions, triggers).  Both are handled safely
   by the deployer.

2. **CREATE remains the preferred convention.**  The WARNING nudges
   authors toward `CREATE` without blocking packaging.  New
   development under the SHIPS Coding Discipline should use `CREATE`.

3. **Projects may escalate to ERROR.**  Teams that want to enforce
   strict `CREATE`-only (e.g. for uniformity in new greenfield
   projects) may set `deploy_intent=ERROR` in `inspect.conf`.  The
   default for projects that do not configure the rule is WARNING.

4. **Projects may silence it entirely.**  Teams migrating large
   codebases with pervasive `REPLACE` usage may set
   `deploy_intent=OFF` to suppress the advisory entirely until
   remediation is complete.

5. **Deployer behaviour is unchanged.**  `REPLACE` source files
   continue to route via the existing `REPLACE_IN_PLACE` strategy.
   No new deployer code paths are introduced.  The change is purely
   an Inspect-layer severity adjustment.

## Consequences

**Positive**

- Existing codebases with `REPLACE` work out of the box.  No mass
  remediation required before adopting SHIPS.
- Rollback safety is unchanged — the deployer captures a pre-flight
  snapshot for both `CREATE` (DROP+CREATE path) and `REPLACE`
  (REPLACE_IN_PLACE path).
- New development is still nudged toward `CREATE` via the WARNING.
- Projects that want strict enforcement retain the `ERROR` option.

**Negative**

- Two DDL styles (`CREATE` and `REPLACE`) will coexist in some
  project repositories.  Reviewers cannot infer deployment intent from
  the verb alone.
- The `REPLACE_IN_PLACE` path has subtly different dependent-
  revalidation semantics to `DROP+CREATE` on some Teradata versions.
  Projects with complex cross-database view dependencies should
  validate behaviour before relying on `REPLACE` in production.

**Neutral**

- The `deploy_intent` rule remains in the rule set; its default
  severity changes.  No rule is removed or renamed.
- The earlier waiver-machinery proposal (discipline_waiver log,
  ships normalise CLI, ships.yaml discipline section) is deferred
  indefinitely.  It may be revisited if a finer-grained audit
  requirement emerges.

## References

- ADR 0006: Deployer owns idempotency — the original decision; updated
  to reflect that REPLACE is now advisory rather than blocked.
- `td_release_packager/validate.py` — `_check_deploy_intent` function
  and `DEFAULT_RULES["deploy_intent"]`.
- `docs/references/inspect_rules.md` — rule reference table.
- `database_package_deployer/deployer.py` — `_deploy_replace_in_place`
  (pre-flight snapshot + REPLACE execution).
