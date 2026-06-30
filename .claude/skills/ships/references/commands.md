# SHIPS CLI Commands

Load this when a task needs exact SHIPS command names, flags, or phase sequencing.

## Core pipeline

- `python -m td_release_packager scaffold --name <project> --output <dir>`: create an idempotent project tree.
- `python -m td_release_packager harvest --source <raw-ddl> --project <project> --prefix-token <Prefix>=DB_PREFIX --force`: write tokenised payload (prefix mode). Per-database mode reads the canonical `config/tokenise.conf` automatically; `--token-map config/token_map.conf` / `--generate-token-map` are the deprecated legacy path.
- `python -m td_release_packager inspect --project <project>`: lint tokenised payload (auto-loads `config/inspect.conf`); Package must not run after a non-zero Inspect.
- `python -m td_release_packager scan --project <project> --all-envs --fail-on-orphan`: validate token references across env configs.
- `python -m td_release_packager package --project <project> --env <env> --name <name> --env-config config/env/<env>.conf --output releases/`: build a release ZIP.
- `python -m td_release_packager process --project <project> --source <raw-ddl> --env <env> --env-config config/env/<env>.conf --name <name>`: run scaffold/harvest/inspect/package flow for CI/CD.
- `python -m td_release_packager process --project .`: argless full pipeline when `ships.yaml` has a `packaging:` profile (#384); precedence is explicit flag > profile > convention.
- `python -m td_release_packager stage --project <project>`: gate on scan + inspect, then `git add` exactly the SHIPS-owned paths (`ships.yaml`, `config/`, `payload/`) (#487). Flags: `--dry-run`, `--strict`. Does not commit. Also exposed as the `ships_stage` MCP tool.

## Plan, wizard, changeset

- `python -m td_release_packager plan --source <raw-ddl> --project <project> --env <env-list> --name <name> --json plan.json`: detect-and-recommend (#379). Non-interactive; auto-detects tokenised/atomic/source-type/DCL-DML and emits the recommended command sequence + rationale + `plan.json`. Flags: `--mode quick|detailed`, `--strict`, `--scaffolded`, `--no-generate`.
- `python -m td_release_packager wizard --source <raw-ddl> --json plan.json`: interactive terminal wizard over the same decision model (#381); works over SSH.
- `python -m td_release_packager changeset --project <project> --since-tag <tag>`: preview changed objects + dependants (#114). Also `--since-commit <sha>`, `--objects DB.A,DB.B`, or `--update-baseline` (capture the content-hash baseline for git-less detection).
- `python -m td_release_packager package --project <project> --env <env> --name <name> --env-config config/env/<env>.conf --since-tag <tag>`: changeset-scoped package (#115). Also `--since-commit`/`--objects`.

## Catalogue metadata export

Export AI-native data-product metadata from a built package to an enterprise catalogue (#244):

```bash
python -m td_release_packager metadata export-alation  --package-dir <unpacked-pkg> --output ./metadata
python -m td_release_packager metadata export-collibra --package-dir <unpacked-pkg> --output ./metadata
python -m td_release_packager metadata export-datahub  --package-dir <unpacked-pkg> --output ./metadata
```

Flags: `--include-internal` (expose internal objects as interfaces), `--strict` (fail on missing metadata). File-only export; never fabricates business metadata.

## GitHub source packaging

Use this pattern when CI packages directly from a repository ref:

```bash
python -m td_release_packager package \
  --source-github myorg/myrepo --source-ref main \
  --env PRD --name MyProject --env-config config/env/PRD.conf
```

## Ship/deploy

- `python -m database_package_deployer deploy --dry-run <package_dir>`: validate deployment order without executing changes.
- `python -m database_package_deployer deploy --host <host> --user <user> <package_dir>`: execute the package against Teradata.
- `python -m database_package_deployer resume <path/to/.deploy_manifest.json>`: continue an interrupted deployment.
- `python -m database_package_deployer rollback <path/to/.deploy_manifest.json>`: use captured snapshots to roll back.
- `python -m database_package_deployer status <path/to/.deploy_manifest.json>`: inspect deployment state.

## Test command

```bash
uv run pytest src/tests/ -q
```

Format before committing: `uv run ruff format src/`. (Legacy invocation without uv: `PYTHONPATH=src python -m pytest src/tests/ -q`.)
