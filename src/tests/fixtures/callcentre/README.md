# CallCentre fixture — curated DBC export slice (PR4)

A 26-file hand-curated subset of the live CallCentre data product's DBC
export. Lives here so the SHIPS test suite can exercise harvest →
inspect → package against a realistic reverse-harvested input on every
CI run, instead of waiting for a developer to point the pipeline at a
local export.

The handover (`HANDOVER-ships-deterministic-deploy.md` §PR4) names this
fixture as the regression gate for PR1–PR3. The Option B sizing it
asks for — "representative, ~30 files, every artefact class" — is what
this fixture is shaped to.

## Source

```
C:\temp\My Reflections\bionic-edcfj1glkxghau5f.env.trial.teradata.com\
    2026-06-12\DBC\DataProducts\CallCentre
```

(Local-only; not committed.)

## What's curated, and why

Every artefact class the deterministic-deploy programme cares about is
represented at least once:

| Artefact            | Examples in fixture                                    |
|---------------------|--------------------------------------------------------|
| Root database `.db` | `CallCentre.db` (FROM `DataProducts` — external parent)|
| Root `.grants`      | `CallCentre.grants`                                    |
| Module base table   | `CallCentre_DOM_STD_T.Agent_H.tbl`, `Call_H.tbl`       |
| Module base `.db`   | `CallCentre_DOM_STD_T.db`, MEM, etc.                   |
| Module base `.grants` | per module                                           |
| Locking-view layer  | `CallCentre_DOM_STD_V.Agent_H.viw`                     |
| Business view       | `CallCentre_DOM_BUS_V.Agent_Current.viw`               |
| Column spec `.col`  | `CallCentre_DOM_BUS_V.Agent_Current.col`               |

Three modules are represented (`DOM`, `MEM`, `SEM`) so cross-module
references and the inter-database grant inference path are both
exercised. `SEM_BUS_V` is included as a "view-only" module — no
matching `_STD_T` in the fixture — to exercise the external-grantee
path (PR6's `warn_external_grants` finding).

## How the harness uses it

Two test files target this fixture:

- `src/tests/test_harvest_determinism.py` — runs the byte-identical
  double-harvest probe (PR1a invariant). After PR4 lands, the probe
  parametrises across both the inline synthetic fixture and this
  real-DBC-shaped fixture, so any non-determinism in the harvest
  pipeline that the synthetic fixture can't expose now surfaces here.
- `src/tests/test_callcentre_golden.py` (added in this PR) — runs
  the full inspect-after-harvest gate against the fixture and
  asserts that inspect's token-coverage check (PR2) agrees with
  package's; that the harvest output is non-empty and contains the
  expected database and grant files; and that re-harvest leaves no
  stale tokenised filenames.

## Curation method

Reproducible from the source. The script that built this slice lives
in the PR description rather than in-repo to avoid coupling the
fixture to a path that's only present on the maintainer's machine.
Re-curate via `cp` from the source export and prune to the file set
listed above.
