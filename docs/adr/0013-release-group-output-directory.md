# ADR 0013 — Release group output directory

## Status

Accepted

## Context

A SHIPS build can now produce more than one deployable archive. A single logical release may contain an environment prerequisite package, an application prerequisite package, and a main package. Keeping these archives directly under `releases/` makes related files harder to see as one release and can separate package pairs when sorted by filename.

SHIPS also needs a group-level handoff point for humans, CI/CD jobs, dashboards, MCP tools, and agents. The package archives each contain their own `context/ships.index.json`, but the release as a whole needs a simple place to discover deploy order and package roles.

## Decision

Every SHIPS build writes output into a release-group directory:

```text
releases/<release_group>/
```

This applies even when the build produces only one package archive.

A single-package release uses:

```text
releases/DEV_OMR_BUILD_0005_20260515120000/
    DEV_OMR_BUILD_0005_20260515120000_01_main.zip
    DEV_OMR_BUILD_0005_20260515120000_01_main.zip.sha256
    release_group.json
    README.txt
```

A multi-package release may use:

```text
releases/DEV_GCFR_BUILD_0012_20260515144900/
    DEV_GCFR_BUILD_0012_20260515144900_00_environment_prereqs.zip
    DEV_GCFR_BUILD_0012_20260515144900_00_environment_prereqs.zip.sha256
    DEV_GCFR_BUILD_0012_20260515144900_01_prereqs.zip
    DEV_GCFR_BUILD_0012_20260515144900_01_prereqs.zip.sha256
    DEV_GCFR_BUILD_0012_20260515144900_02_main.zip
    DEV_GCFR_BUILD_0012_20260515144900_02_main.zip.sha256
    release_group.json
    README.txt
```

The shared `release_group` value remains the unsuffixed identity, for example `DEV_GCFR_BUILD_0012_20260515144900`.

## Consequences

- Operators hand off the release-group directory, not a loose zip file.
- Agents and dashboards should scan `releases/**/*.zip` or read `release_group.json`.
- Package archives remain immutable artefacts. Extracted package directories, runtime logs, and rollback captures are not written into the release-group directory by default.
- `release_group.json` is the group-level index for deploy order, package roles, checksum sidecars, and package-local `context/ships.index.json` entrypoints.
- The package-local context contract remains unchanged inside each archive.

## Rejected alternatives

### Keep flat `releases/*.zip`

Rejected because related package artefacts can be visually separated and require filename parsing to reconstruct the release group.

### Use group directories only for multi-package releases

Rejected because it makes scripts and documentation branch on the number of generated archives. Consistent group output is simpler for humans and agents.
