# ADR 0012: Package Trust Score Design

## Status

Accepted | 2026-04-16

## Context

The Package phase produces a release artefact. Before shipping
that artefact to a target environment, an operator or an automated
pipeline needs to answer a single question: **"Is this package
safe to deploy?"**

Prior to SHIPS, this question was answered implicitly: if the
build did not error, the package was assumed to be deployable.
This assumption fails in two ways:

1. **Binary pass/fail conceals severity gradients.** A package
   that passed Inspect with one WARNING-severity finding is
   treated the same as a package with zero findings. An operator
   reviewing the package has no quick signal for how much
   residual risk the package carries.

2. **Individual Discipline metrics are not comparable.** An
   operator seeing "3 token warnings, 0 naming violations,
   1 incomplete sidecar" cannot quickly judge whether this
   package is safer or riskier than "0 token warnings, 2 naming
   violations, 0 incomplete sidecars." The metrics are in
   different spaces.

A composite trust score was proposed to address both problems:
a single number summarising the package's overall deployability
signal, computed from a weighted combination of individual
quality dimensions.

Several design decisions were required:

**1. Should the score be 0–100 or 0–1?** A 0–100 integer is more
immediately interpretable in the context of a deployment review.
"97%" is more natural than "0.97 trust factor."

**2. Should a perfect score (100%) be achievable?** A score of
100% implies that the package is fully safe to deploy — a claim
that no static analysis tool can make. Real-world deployments
depend on target environment state (privileges, space, lock
availability) that the Package phase cannot inspect. A score
ceiling below 100% communicates that the Trust Score is a
signal, not a guarantee.

**3. How many dimensions?** Too few dimensions (one or two) and
the score collapses to a pass/fail. Too many dimensions (ten or
more) and the score is impossible to interpret: why did quality
drop from 94% to 89%? Six dimensions were chosen as the point at
which each dimension is meaningfully distinct and individually
actionable.

**4. Should dimensions be equally weighted?** Equal weighting
was the initial proposal. After review, Quality and Safety were
given equal and highest weight (20% each) on the grounds that a
package with poor DDL quality or a safety violation represents a
higher residual deployment risk than a package with incomplete
provenance metadata. The remaining four dimensions share 60%
equally (15% each).

## Decision

The Package Trust Score is a composite metric in the range
0–97%, computed at Package time and embedded in the release
manifest. The score is:

```
Trust Score = Σ(dimension_score_i × weight_i) × 0.97
```

The 0.97 ceiling is applied as a final multiplier. A package
with a perfect raw score across all dimensions reaches 97%, not
100%, communicating that the score is a deployability signal
rather than a deployment guarantee.

### Dimensions and Weights

| Dimension | Weight | Measures |
|-----------|--------|---------|
| Quality | 20% | Zero ERROR-severity Discipline violations; low WARNING count; naming convention compliance; token completeness. |
| Safety | 20% | No destructive operations (DROP on objects not in the package's own payload); no unrecognised file extensions; no `--force` flag overrides active in the run. |
| Completeness | 15% | All expected object types present for the declared module type; companion `.stt` files present for all table files; properties file conformance. |
| Isolation | 15% | Inter-database grants exist for all cross-database references in views; no direct table-database references from non-Domain databases; no hardcoded database names (unresolved tokens). |
| Verifiability | 15% | SHA-256 sidecar present and matches archive content; manifest is parseable and internally consistent; all manifest entries reference files that exist in the archive. |
| Provenance | 15% | `ships.yaml` present with project name, version, and author fields populated; harvest timestamp recorded; token map version recorded. |

### Score Computation

Each dimension contributes a value in [0, 1]:

- A dimension with no violations contributes 1.0.
- Each ERROR-severity violation against that dimension's
  contributing rules reduces the dimension score linearly,
  to a minimum of 0.0. WARNING-severity violations reduce
  the score by a smaller factor (configurable, default 0.25×
  of the ERROR reduction factor).
- The raw composite is the weighted sum of dimension scores.
- The final Trust Score is the raw composite multiplied by 0.97,
  rounded to the nearest integer.

### Surfacing

The Trust Score is:

1. Printed to the CLI at the end of `ships package` in the form
   `Package Trust Score: 94% (Quality 18/20, Safety 20/20,
   Completeness 13/15, Isolation 12/15, Verifiability 15/15,
   Provenance 13/15)`.

2. Embedded in `manifest.json` under `trust_score` with full
   dimension breakdown and the contributing violations per
   dimension.

3. Surfaced in the deploy report HTML as a prominently placed
   gauge with colour coding: ≥90% green, 75–89% amber, <75% red.

### CI/CD Integration

`ships package` accepts a `--min-trust-score N` flag. If the
computed Trust Score is below N, the command exits non-zero.
This allows a pipeline to gate on a minimum acceptable trust
level (e.g. `--min-trust-score 90` for a production pipeline,
`--min-trust-score 75` for a development pipeline).

## Consequences

**Positive**

- A single number communicates overall package health to
  operators and reviewers without requiring familiarity with
  the full Discipline rule set.
- The 97% ceiling is a design statement: SHIPS is explicit that
  it cannot guarantee deployment success, only characterise the
  static risk profile of the package.
- CI/CD gating on `--min-trust-score` makes the quality bar
  explicit and configurable per environment.
- The dimension breakdown in the manifest makes score drops
  actionable: a fall in the Safety dimension points to a
  different set of files than a fall in the Isolation dimension.

**Negative**

- The score is not self-explaining. An operator seeing "87%"
  cannot immediately know which dimension dropped or why.
  Mitigation: the CLI output shows the per-dimension breakdown;
  the deploy report makes the contributing violations visible.
- Weighting is inherently subjective. The 20% / 15% split was
  agreed by the project author; a different project may weight
  Provenance higher (for regulatory traceability) or Safety
  lower (for internal development pipelines). The weights are
  currently hardcoded, not configurable.
- A package that passes `--min-trust-score 90` is not
  guaranteed to deploy successfully. Operators who treat the
  trust score as a deployment guarantee rather than a
  deployability signal will be surprised when a 95% package
  fails on a privilege error.

**Neutral**

- The 97% ceiling was chosen as a prime number close to 100
  to avoid the anchoring effect of round numbers (95% implies
  "5% unsafe"; 97% is less psychologically loaded). This is
  an aesthetic decision; any value in [93%, 99%] would serve
  the same purpose.
- Weight configurability is deferred. A `ships.yaml` section
  for `trust_score.weights` is a natural future extension of
  the `discipline:` section introduced in ADR 0009.
- The six-dimension model is extensible. A seventh dimension
  (e.g. `Compatibility` covering target environment version
  checks) can be added without changing the score computation
  formula — only the weights need rebalancing.

## Alternatives considered

**Binary pass/fail (Inspect result only).** Rejected: Inspect
is binary by design — it gates on the absence of ERROR-severity
violations. WARNING-severity issues are surfaced but do not
block. A Trust Score captures the WARNING-severity residual risk
that Inspect's binary gate cannot express.

**Raw violation count.** Considered: surface the total number
of Discipline violations as the quality signal. Rejected: a
count is not normalised. "12 violations" on a 300-file package
is very different from "12 violations" on a 20-file package.
A percentage-based score normalises by package size implicitly.

**Five equal dimensions (20% each).** Rejected: treating
Quality and Safety as equal to Provenance inflates the weight
of metadata completeness at the expense of the most deployment-
critical factors. An incomplete `ships.yaml` author field is not
comparable in risk to an ERROR-severity DDL quality violation.

**External scoring service.** Rejected at project inception:
the Trust Score must be computable at Package time with no
network access. A package built offline must carry its Trust
Score.

## References

- `td_release_packager/builder.py` — Trust Score computation
  and embedding in `manifest.json`.
- `td_release_packager/validate.py` — source of the Discipline
  violation data that feeds the Quality and Safety dimensions.
- `database_package_deployer/report.py` — Trust Score gauge rendering in
  the deploy report HTML.
- ADR 0002: SHIPS pipeline phase structure — Trust Score is
  computed during the Package phase (phase 4).
- ADR 0004: Atomic eponymous DDL files — file count and type
  distribution inform the Completeness dimension.
- ADR 0007: Package-level rollback via pre-flight snapshot —
  SHA-256 sidecar presence and integrity feed the Verifiability
  dimension.
- ADR 0008: DCL subdirectory structure — inter-database grant
  coverage feeds the Isolation dimension.
