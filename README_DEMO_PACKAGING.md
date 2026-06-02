# SHIPS Demo Packaging Quick Guide

This guide is for teams who want a low-friction way to turn a demo SQL
repository into a SHIPS package without first learning the full SHIPS project
workflow.

Demo mode is designed for repositories that contain ordinary SQL scripts,
especially numbered demo scripts such as:

```text
workspace/
  src/
    my-demo/
      00-setup.sql
      01-tables.sql
      02-views.sql
      03-grants.sql
```

It stages the SQL into a generated SHIPS project, tokenises detected database
names, runs relaxed inspection and dependency analysis, then builds deployable
ZIP packages.

## Install

From this repository:

```powershell
pip install -e .
```

If you use `uv`:

```powershell
uv sync
```

## Package a GitHub Demo

Use a short output directory on Windows. This avoids path-length pain and keeps
the generated artefacts easy to inspect.

```powershell
python -m td_release_packager demo `
  --source-github NathanG-TD/cargointelligence-data-product `
  --source-ref master `
  --name CargoIntelligence `
  --root-parent DEMO_ROOT_DB `
  --output C:\ships
```

For Linux/macOS shells:

```bash
python -m td_release_packager demo \
  --source-github NathanG-TD/cargointelligence-data-product \
  --source-ref master \
  --name CargoIntelligence \
  --root-parent DEMO_ROOT_DB \
  --output ./releases
```

## Package a Local Demo

```powershell
python -m td_release_packager demo `
  --source C:\path\to\demo-repo `
  --name MyDemo `
  --root-parent DEMO_ROOT_DB `
  --output C:\ships
```

## What the Command Produces

Demo mode prints a summary with the important paths:

```text
Source:      <detected SQL source folder>
Project:     <generated SHIPS project>
Env config:  <generated DEV.conf>
Token map:   <generated token_map.conf>
Archive:     <main package ZIP>
Report:      <short package_report_*.html sidecar>
Release grp: <release group folder>
```

The release group folder contains the package ZIPs, checksums, release metadata,
and short `package_report_*.html` files that can be opened directly. You do not
need to open the ZIP just to inspect the package report.

## Root Parent

Teradata defaults parentless `CREATE DATABASE` and `CREATE USER` statements to
the user running the deployment. That is surprising for demos because it gives
you little control over where the demo hierarchy is created.

Use `--root-parent` to make that explicit:

```powershell
--root-parent DEMO_ROOT_DB
```

Demo mode rewrites staged parentless prerequisite scripts from:

```sql
CREATE DATABASE Demo_DB AS PERMANENT = 1000000;
```

to:

```sql
CREATE DATABASE Demo_DB FROM {{ROOT_PARENT}} AS PERMANENT = 1000000;
```

The generated environment config then resolves:

```text
ROOT_PARENT=DEMO_ROOT_DB
```

Existing `FROM SomeParent` clauses are preserved.

## Prepare Only

Use `--prepare-only` when you want to inspect the generated SHIPS project before
building a package:

```powershell
python -m td_release_packager demo `
  --source C:\path\to\demo-repo `
  --name MyDemo `
  --root-parent DEMO_ROOT_DB `
  --prepare-only
```

This stages, tokenises, inspects, and analyses the demo without creating ZIPs.

## Deploy or Dry Run

After packaging, dry-run the release group:

```powershell
python -m td_release_packager deploy C:\ships\DEV_CargoIntelligence_BUILD_1_<timestamp> --dry-run
```

Deploy when the dry run looks good:

```powershell
python -m td_release_packager deploy C:\ships\DEV_CargoIntelligence_BUILD_1_<timestamp> --host <td-host> --user <deploy-user>
```

You can also ask demo mode to package and then deploy in one command:

```powershell
python -m td_release_packager demo `
  --source C:\path\to\demo-repo `
  --name MyDemo `
  --root-parent DEMO_ROOT_DB `
  --output C:\ships `
  --deploy -- --dry-run
```

## Useful Options

`--source-github OWNER/REPO` fetches a GitHub repository. SHIPS tries the GitHub
API first, then falls back to local `git clone` credentials.

`--source-ref REF` selects the branch, tag, or commit. Use `master` for older
repos that do not have `main`.

`--name NAME` sets the package and generated project name. Keep this short on
Windows.

`--output PATH` or `--output-dir PATH` chooses where release packages are
written.

`--work-dir PATH` chooses where the generated SHIPS project is staged. The
default is `.ships-demo`.

`--root-parent NAME` injects an explicit parent for parentless `CREATE DATABASE`
and `CREATE USER` statements.

`--prepare-only` skips package creation.

`--deploy -- <args>` deploys the generated release group and forwards arguments
after `--` to the deploy command.

## Troubleshooting

If GitHub returns `404`, check the branch name first:

```powershell
--source-ref master
```

For private repositories, either set `GITHUB_TOKEN` or make sure `git clone`
works from the same shell.

If Windows cannot open a ZIP or report path, use shorter names:

```powershell
--name CI --output C:\ships
```

Open the sidecar report next to the ZIP:

```text
package_report_main.html
package_report_prereqs.html
```

If tokenisation looks too aggressive, inspect the generated staged SQL under:

```text
.ships-demo\<name>\payload\
```

Demo mode should tokenise database/container names, not table names, column
names, SQL keywords, or datatypes.

## Adoption Pattern

For team demos, the recommended pattern is:

1. Keep demo SQL in a simple numbered-script repository.
2. Use `--root-parent` for predictable hierarchy placement.
3. Use a short Windows output path such as `C:\ships`.
4. Review the sidecar `package_report_*.html`.
5. Dry-run the release group.
6. Deploy the same release group when the dry run is clean.

This gives colleagues a fast path from "interesting demo repository" to a
reviewable SHIPS package while still retaining SHIPS inspection, dependency
ordering, package reports, checksums, and deploy evidence.
