# ADR 0009: Configurable `deploy_intent` Rule with Audit Waiver

## Status

Accepted | 2026-05-04

## Context

The `deploy_intent` Discipline rule (introduced as part of the
deployer-owned idempotency model — see ADR 0005) requires DDL files
in source to use `CREATE`, never `REPLACE`. The reasoning was sound:

- **Source is declarative.** A `CREATE` statement says "this is
  what the object IS" — a definition. A `REPLACE` statement says
  "modify this existing thing" — a transformation. Source should
  describe definitions; transformations are the deployer's job.
- **Uniform repository convention.** A reviewer reading any DDL
  file should see the same verb across the project. Mixed
  `CREATE` / `REPLACE` files would carry no consistent signal —
  the choice would reflect the author's muscle memory rather than
  a deliberate intent.
- **One mental model for the developer.** "Always type `CREATE`,
  regardless of object type." No "is this a view? then I should
  use `REPLACE`..." judgement call.

In practice, the rule has friction. Many Teradata developers — the
project author included — have years of muscle memory for `REPLACE
VIEW`, `REPLACE PROCEDURE`, `REPLACE MACRO`. These get typed
automatically. The current Discipline rejects these files at
Inspect time and forces a rewrite to `CREATE`, which feels
punitive given that the deployer can already handle either form
through its `REPLACE_IN_PLACE` strategy.

There is also a defensible argument that — *if* a pre-deploy
backup snapshot is captured (which SHIPS does at package level via
ADR 0006) — then `REPLACE` and `DROP+CREATE` provide equivalent
recovery guarantees. Mechanically the rollback path is the same:
replay the captured DDL.

So the question is not "is `REPLACE` recoverable?" — it is. The
question is "what does the `CREATE`-only rule give us that we lose
by relaxing it?"

Three properties were identified:

1. **Audit of intent.** A file using `CREATE` declares "this is
   the object's definition." A file using `REPLACE` declares "I
   am modifying an existing object." With both allowed, two
   developers working on the same view will pick different forms
   based on muscle memory, and the file no longer carries that
   signal.

2. **Engine-semantics neutrality at source.** The deployer
   dispatches per object type via `STRATEGY_MAP`:
   - `DROP_AND_CREATE` for tables, indexes, triggers (Teradata
     does not allow `REPLACE` on these types).
   - `REPLACE_IN_PLACE` for views, macros, procedures, functions,
     script table operators (Teradata's `REPLACE` keyword exists
     for these).

   The choice between `DROP+CREATE` semantics and `REPLACE`
   semantics — which differ in how Teradata revalidates dependents
   — is the deployer's choice, made consistently per type. If
   source uses `REPLACE`, that decision leaks into the source
   file: different developers might write `CREATE VIEW` or
   `REPLACE VIEW` based on intuition about dependent behaviour,
   making the staged payload inconsistent across files of the
   same type. CREATE-only in source preserves the property that
   the deployer's strategy choice is uniform per type.

3. **Default behaviour for new projects.** A configurable rule
   with the wrong default silently changes the project posture.
   If the default became "off," every new SHIPS project would
   inherit the relaxed posture without an explicit opt-in.

The author proposed making the rule configurable. The bare
proposal "make it configurable, default off" was rejected because
it reverses SHIPS' default safety posture. A constrained form was
agreed.

A separate alignment question surfaced during ADR review. If
source uses `CREATE` for a type whose deployer strategy is
`REPLACE_IN_PLACE` (e.g. `CREATE VIEW`), the deployer's
`_deploy_replace_in_place` function executes the source DDL
**as-is**. Executing `CREATE VIEW x.v ...` against a target where
the view already exists fails with "object already exists."
Something between source and runtime has to make the statement
idempotent. After weighing alternatives (deploy-time rewriting,
abandoning `REPLACE_IN_PLACE` entirely), harvest-time rewriting
was chosen as the cleanest answer: the staged payload carries
the deploy-ready statement, the rewrite is mechanical and visible
in the staged tree, and the deployer's strategy dispatch is
unchanged.

## Decision

The `deploy_intent` rule becomes **project-configurable**,
defaulting to **ON**, with the following constraints:

1. **Configuration site.** A new key in `ships.yaml` controls the
   rule:

   ```yaml
   discipline:
     deploy_intent:
       enabled: true   # default if key absent
   ```

   Configuration is project-level. There is no user-level or
   environment-level override. Either the project enforces the
   rule for every developer and every environment, or it does
   not.

2. **Default posture.** Absence of the key, an empty value, or
   `enabled: true` keeps the existing behaviour. `REPLACE` in
   source continues to fail Inspect with rule `deploy_intent` at
   ERROR severity.

3. **Opt-out mechanism.** Setting `enabled: false` in `ships.yaml`
   relaxes the rule. The `validate.py` rule still scans every DDL
   file for `REPLACE`, but instead of emitting an ERROR-severity
   issue, it emits a `discipline_waiver` entry recorded to
   `decisions.json` for that run. Each waiver entry includes:

   - `rule`: `"deploy_intent"`
   - `file`: the DDL path
   - `line`: line number where `REPLACE` was found
   - `waived_by`: the `enabled: false` setting in `ships.yaml`
   - `recorded_at`: timestamp

   The waiver entry is also surfaced in the HTML deploy report so
   reviewers can see at a glance which files exercised the
   relaxation.

4. **Author-time normalisation tool.** A new `ships normalise`
   (or `ships fix`) CLI command rewrites `REPLACE` → `CREATE` in
   source files. This is the *recommended* path for developers
   who type `REPLACE` from muscle memory: they keep typing what
   their fingers know, and the tool fixes it before commit. The
   opt-out (decision 3) is reserved for projects with a
   deliberate reason to keep `REPLACE` in source.

5. **Harvest-time rewrite for replaceable types.** When source
   uses `CREATE` and the object's deployer strategy (per
   `STRATEGY_MAP`) is `REPLACE_IN_PLACE` — views, macros,
   procedures, functions, script table operators — `harvest`
   rewrites `CREATE` → `REPLACE` in the staged payload. The
   rewrite is:

   - **Mechanical.** Driven by the strategy map, not by per-file
     judgement.
   - **Type-driven.** Only applies to types where
     `REPLACE_IN_PLACE` is the strategy. Tables, indexes, and
     triggers remain `CREATE` in the staged payload — their
     deployer strategy is `DROP_AND_CREATE`, which expects
     `CREATE`.
   - **Visible.** The rewritten content is what lands in
     `payload/database/...`. Anyone inspecting the staged tree
     sees what will actually execute.
   - **Logged.** Each rewrite is recorded in the harvest output
     so the developer sees the transformation.

   Source files retain `CREATE` (the canonical declarative form).
   The staged payload carries the deploy-ready form. The
   relationship between source and staged is documented and
   uniform across the project.

6. **Deployer behaviour is unchanged.** The deployer continues to
   dispatch per `STRATEGY_MAP`:

   - Tables, indexes, triggers → `DROP_AND_CREATE` (drops
     existing, executes the staged `CREATE`).
   - Views, macros, procedures, functions, STOs →
     `REPLACE_IN_PLACE` (captures via SHOW, executes the staged
     `REPLACE` as-is).

   No new deployer paths. No source-rewriting at deploy time. The
   harvest-time rewrite (decision 5) ensures the staged payload
   always matches what each strategy expects to see.

7. **Eval coverage.** The Stage 9 eval suite gains a new synthetic
   project `011_replace_with_waiver/` that exercises a project
   with `enabled: false` and seeded `REPLACE` source files. The
   test asserts:

   - The deployer dispatches files through their strategy-map-
     correct strategy.
   - For `REPLACE_IN_PLACE` types, the staged payload contains
     `REPLACE` (regardless of whether the source had `REPLACE`
     or `CREATE` rewritten by harvest).
   - For `DROP_AND_CREATE` types, the staged payload contains
     `CREATE`.
   - `discipline_waiver` entries appear in `decisions.json` for
     any source files that retained `REPLACE`.
   - The deploy report surfaces the waivers.

   Recall on the existing `002_replace_violation` case (with
   default config) must remain 1.00 — the default-ON path cannot
   regress.

## Consequences

**Positive**

- Single mental model for developers: always write `CREATE`. The
  `CREATE → REPLACE` rewrite for replaceable types is harvest's
  job, not the developer's.
- Source remains declarative. Every file says "this is what the
  object is," uniformly.
- Recovery story unchanged. The deployer still captures snapshots
  and executes per `STRATEGY_MAP`. Nothing about runtime
  semantics changes.
- Default posture unchanged. Projects that don't set the key keep
  the strict behaviour.
- A clean migration path exists for muscle-memory: `ships
  normalise` rewrites at author time, so the opt-out becomes
  optional rather than necessary.
- Auditability preserved. The `discipline_waiver` log makes any
  opt-out visible.

**Negative**

- Two configurations of SHIPS now exist in the wild. Future
  Discipline changes that interact with `deploy_intent` need to
  consider both.
- The `discipline_waiver` mechanism is new infrastructure. It
  needs schema definition in the manifest, surfacing in the
  report, and test coverage — non-trivial but bounded work.
- Some reviewers may dislike `ships normalise` rewriting source
  on the grounds that "tools should not alter the intent of
  code." Mitigation: `normalise` only runs when explicitly
  invoked (CLI or pre-commit hook); it is not implicit.
- Harvest now performs a content-altering rewrite (decision 5).
  The change is mechanical and type-driven, but it is a
  transformation between source and staged. Documentation needs
  to be clear: source is canonical, staged is the deploy-ready
  form, the rewrite is type-driven by `STRATEGY_MAP`. A
  one-line note in the harvest output and the deploy report
  addresses the most likely confusion ("why does staged say
  REPLACE when my source says CREATE?").

**Neutral**

- The Discipline rule list grows by zero — `deploy_intent` is
  the same rule, just newly configurable.
- `ships.yaml` schema gains a `discipline:` section that may host
  future rule configurations. Worth designing the section to be
  extensible from the start (each rule a sub-key, each rule with
  `enabled` plus optional rule-specific keys).
- The harvest rewrite leverages the existing `STRATEGY_MAP` from
  `ddl_deployer/models.py`. No new mapping table is introduced;
  the rule "rewrite `CREATE` → `REPLACE` if strategy is
  `REPLACE_IN_PLACE`" is computable from existing data.

## Alternatives considered

**Status quo: keep `deploy_intent` strict and non-configurable.**
Rejected: the friction for muscle-memory `REPLACE` is real, and
the strict rule provides no recovery benefit over what ADR 0006's
pre-flight snapshot already gives. `ships normalise` plus an
auditable opt-out covers the friction with proportionate
infrastructure.

**Make the rule configurable, default OFF.** Rejected explicitly.
This silently reverses SHIPS' posture for every project that does
not opt in. New projects, in particular, would inherit the
relaxed behaviour without anyone consciously choosing it. Default
ON is the only defensible position.

**User-level or environment-level configuration.** Rejected: the
Discipline must be uniform within a project, otherwise reviewer A
and reviewer B see different rules applied to the same files
based on their personal config. Project-level is the right scope.

**`ships normalise` only, no opt-out.** Considered. This is the
cleanest solution architecturally — no new configuration, no new
manifest entries, no waiver log to maintain. Rejected because
some teams may have a deliberate reason to keep `REPLACE` in
source (legacy compatibility with existing review tooling, for
example), and an opt-out with audit logging serves them without
compromising the default posture for everyone else.

**Severity reduction (`REPLACE` → WARNING instead of ERROR).**
Rejected: warnings get ignored at scale. Either the rule applies
or it does not. A WARNING-level rule provides the worst of both
worlds — the friction of the inspection without the certainty of
the enforcement.

**Deployer rewrites `CREATE` → `REPLACE` at execution time
(rather than at harvest time).** Considered as Option B during
ADR drafting. Rejected: the rewrite would be invisible to anyone
reviewing the staged payload, making deploy debugging harder
("why does the deploy log show `REPLACE` when the staged file
says `CREATE`?"). Harvest-time rewrite (decision 5) keeps the
transformation visible in the staged tree, where it can be
diffed and reviewed.

**Eliminate `REPLACE_IN_PLACE` entirely; use `DROP_AND_CREATE`
for all types.** Considered as Option C during ADR drafting.
Rejected: loses Teradata's dependent-revalidation semantics,
particularly for views with cross-database dependents. `REPLACE`
revalidates dependents only as needed; `DROP+CREATE` invalidates
them all. Real semantic loss in exchange for code-path
simplification. Not worth it.

## References

- ADR 0005: Deployer owns idempotency (DDL files use CREATE, not
  REPLACE) — the original principle this ADR operationalises.
  *(To be backfilled per ADR 0001's references list.)*
- ADR 0006: Package-level rollback via pre-flight snapshot — the
  mechanism that makes recovery equivalent between `CREATE` and
  `REPLACE` in source. *(To be backfilled.)*
- `td_release_packager/validate.py` — the `_check_deploy_intent`
  function that implements the source-level Inspect rule.
- `td_release_packager/ships_yaml.py` — the schema for
  `ships.yaml` that gains the new `discipline:` section.
- `td_release_packager/ingest.py` — gains the harvest-time
  `CREATE` → `REPLACE` rewrite for `REPLACE_IN_PLACE` types
  (decision 5). Already contains a `_inject_replace_view` helper
  with related logic; that helper will be generalised across the
  full `REPLACE_IN_PLACE` type set.
- `ddl_deployer/models.py` — `STRATEGY_MAP` (lines 131-144)
  drives both the deployer dispatch and the harvest rewrite.
- `ddl_deployer/deployer.py` — `_deploy_drop_and_create`
  (line 1821) and `_deploy_replace_in_place` (line 1910) implement
  the two strategies. Both remain unchanged by this ADR.
