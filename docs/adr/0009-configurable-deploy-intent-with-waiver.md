# ADR 0009: Configurable `deploy_intent` Rule with Audit Waiver

## Status

Proposed | 2026-05-04

## Context

The `deploy_intent` Discipline rule (introduced as part of the
deployer-owned idempotency model — see ADR 0005) requires DDL files
to use `CREATE`, never `REPLACE`. The reasoning was sound:

- Idempotency is owned by the deployer (DROP + CREATE with pre-flight
  snapshot), not by the engine's `REPLACE` semantics
- A uniform repository convention means a reviewer can read intent
  from a file alone — `CREATE` means "I'm declaring this object",
  full stop
- The deployer code path stays single — capture snapshot, drop,
  create, replay snapshot on failure

In practice, the rule has friction. Many Teradata developers — the
project author included — have years of muscle memory for `REPLACE
VIEW`, `REPLACE PROCEDURE`, `REPLACE MACRO`. These get typed
automatically. The current Discipline rejects these files at Inspect
time and forces a rewrite to `CREATE`, which feels punitive given that
the deployer already handles both `CREATE` and `REPLACE` verbs via its
strategy map.

There is also a defensible argument that — *if* a pre-deploy backup
snapshot is captured (which `teradata-deployment-agent` does at package
level via ADR 0006)
— then `REPLACE` and DROP+CREATE provide equivalent recovery
guarantees. Mechanically the rollback path is the same: replay the
captured DDL.

So the question is not "is `REPLACE` recoverable?" — it is. The
question is "what other properties does the `CREATE`-only rule give
us that we lose by relaxing it?"

Three properties were identified:

1. **Audit of intent.** A file using `CREATE` declares "this is the
   object's definition." A file using `REPLACE` declares "I am
   modifying an existing object." With both allowed, two developers
   working on the same view will pick different forms based on
   muscle memory, and the file no longer carries that signal.

2. **Deploy-path semantics.** The deployer already dispatches on DDL
   verb: `CREATE VIEW` routes to `CREATE_ONLY`; `REPLACE VIEW` routes
   to `REPLACE_IN_PLACE` (capture-via-SHOW then execute REPLACE).
   Allowing both verbs in source means both paths remain active. The
   `REPLACE_IN_PLACE` path has subtle Teradata-specific behaviour:
   views with dependent views can fail at `REPLACE` time in ways
   `DROP+CREATE` does not, because Teradata revalidates dependents at
   `REPLACE` time but not always identically across versions.

3. **Default behaviour for new projects.** A configurable rule with
   the wrong default silently changes the project posture. If the
   default became "off", every new `teradata-deployment-agent` project
   would inherit the
   relaxed posture without an explicit opt-in.

The author proposed making the rule configurable. After discussion,
the bare proposal "make it configurable, default off" was rejected
because it reverses the project's default safety posture. A constrained
form was agreed.

## Decision

The `deploy_intent` rule becomes **project-configurable**, defaulting
to **ON**, with the following constraints:

1. **Configuration site.** A new key in `ships.yaml` controls the
   rule:

   ```yaml
   discipline:
     deploy_intent:
       enabled: true   # default if key absent
   ```

   Configuration is project-level. There is no user-level or
   environment-level override. Either the project enforces the rule
   for every developer and every environment, or it does not.

2. **Default posture.** Absence of the key, an empty value, or
   `enabled: true` keeps the existing behaviour. `REPLACE` continues
   to fail Inspect with rule `deploy_intent` at ERROR severity.

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
   reviewers can see at a glance which files exercised the relaxation.

4. **Author-time normalisation tool.** A new `ships normalise` (or
   `ships fix`) CLI command rewrites `REPLACE` → `CREATE` in source
   files. This is the *recommended* path for developers who type
   `REPLACE` from muscle memory: they keep typing what their fingers
   know, and the tool fixes it before commit. The opt-out is reserved
   for projects with a deliberate reason to keep `REPLACE` in source.

5. **Deployer behaviour is unchanged.** When `deploy_intent` is waived
   and source uses `REPLACE`, the deployer routes via its existing
   `REPLACE_IN_PLACE` strategy — capture the existing definition via
   SHOW for rollback, then execute the `REPLACE` statement as-is. No
   new deployer code paths are introduced. The waiver is purely an
   Inspect-layer permission; deploy-time dispatch is driven by the DDL
   verb, exactly as it is today.

6. **Eval coverage.** The Stage 9 eval suite gains a new synthetic
   project `011_replace_with_waiver/` that exercises a project with
   `enabled: false` and seeded `REPLACE` files. The test asserts
   that the deployer routes files via `REPLACE_IN_PLACE` (not
   CREATE_ONLY), `discipline_waiver` entries appear in
   `decisions.json`, and the deploy report surfaces them. Recall on the existing
   `002_replace_violation` case (with default config) must remain
   1.00 — the default-ON path cannot regress.

## Consequences

**Positive**

- Developer friction reduced for projects that opt in. Authors who
  prefer `REPLACE` in source can have it.
- Auditability preserved. The `discipline_waiver` log makes the
  relaxation visible rather than silent. A reviewer of `decisions.json`
  can see exactly when the safety net was opened.
- Default posture unchanged. Projects that don't set the key keep
  the strict behaviour.
- Recovery story unchanged. The deployer captures a rollback snapshot
  (via SHOW) before any `REPLACE_IN_PLACE` execution — the same
  pre-flight protection applied to `DROP+CREATE` types.
- A clean migration path exists for muscle-memory: `ships normalise`
  rewrites at author time, so the opt-out becomes optional rather
  than necessary.

**Negative**

- Two configurations of `teradata-deployment-agent` now exist in the
  wild. Future Discipline
  changes that interact with `deploy_intent` need to consider both.
- The `discipline_waiver` mechanism is new infrastructure. It needs
  schema definition in the manifest, surfacing in the report, and
  test coverage — non-trivial but bounded work.
- Some reviewers may dislike `ships normalise` rewriting source on
  the grounds that "tools should not alter the intent of code."
  Mitigation: `normalise` only runs when explicitly invoked (CLI or
  pre-commit hook); it is not implicit.
- The `REPLACE_IN_PLACE` path has different dependent-revalidation
  semantics to `DROP+CREATE` (see Context point 2). Projects using
  the waiver should validate behaviour against views with cross-
  database dependents before relying on it in production.

**Neutral**

- The Discipline rule list grows by zero — `deploy_intent` is the
  same rule, just newly configurable.
- `ships.yaml` schema gains a `discipline:` section that may host
  future rule configurations. Worth designing the section to be
  extensible from the start (each rule a sub-key, each rule with
  `enabled` plus optional rule-specific keys).

## Alternatives considered

**Status quo: keep `deploy_intent` strict and non-configurable.**
Rejected: the friction for muscle-memory `REPLACE` is real, and
the strict rule provides no recovery benefit over what
ADR 0006's pre-flight snapshot already gives.

**Make the rule configurable, default OFF.** Rejected explicitly.
This silently reverses the project's posture for every project that does
not opt in. New projects, in particular, would inherit the relaxed
behaviour without anyone consciously choosing it. Default ON is
the only defensible position.

**User-level or environment-level configuration.** Rejected: the
Discipline must be uniform within a project, otherwise reviewer A
and reviewer B see different rules applied to the same files based
on their personal config. Project-level is the right scope.

**`ships normalise` only, no opt-out.** Considered. This is the
cleanest solution architecturally — no new configuration, no new
manifest entries, no second deployer path to document. Rejected
because some teams may have a deliberate reason to keep `REPLACE`
in source (legacy compatibility with existing review tooling, for
example), and an opt-out with audit logging serves them without
compromising the default posture for everyone else.

**Severity reduction (`REPLACE` → WARNING instead of ERROR).**
Rejected: warnings get ignored at scale. Either the rule applies
or it does not. A WARNING-level rule provides the worst of both
worlds — the friction of the inspection without the certainty of
the enforcement.

## References

- ADR 0005: Deployer owns idempotency (DDL files use CREATE, not
  REPLACE) — the original decision this ADR partially relaxes.
- ADR 0006: Package-level rollback via pre-flight snapshot — the
  mechanism that makes recovery equivalent between `CREATE` and
  `REPLACE` in source.
- `td_release_packager/validate.py` — the `_check_deploy_intent`
  function that implements the rule.
- `td_release_packager/ships_yaml.py` — the schema for `ships.yaml`
  that gains the new `discipline:` section.
- `ddl_deployer/deployer.py` — `_deploy_replace_in_place` and
  `_deploy_drop_and_create` implement the existing two-path dispatch;
  `_detect_deploy_intent` in `ddl_parser.py` selects the path from
  the DDL verb.
