# PR: Deploy release packages without manual extraction

## Summary

Adds an operator-friendly deployment path for SHIPS packages:

- `python -m td_release_packager deploy <package.zip> ...`
- `python -m td_release_packager deploy <release_group_dir> ...`
- generated `deploy_release.py` in every release-group directory

The launcher extracts package archives automatically into a short `.ships-work`
directory beside the artifact, then invokes the generated package-local
`deploy.py` so existing integrity, trust, logging, wave, and report behavior is
preserved.

## Behavior

- Single package zip: extracts and runs that package's generated `deploy.py`.
- Release group directory: extracts archives listed in `release_group.json`, runs
  any `environment_prereqs` package first for live deploys, then runs the selected
  role (`main` by default).
- Dry-run release group: runs only the selected role so environment prerequisites
  are not deployed live during validation.
- Extracted package directory: still works and simply invokes its `deploy.py`.

## Verification

- `uv run ruff format src/`
- `uv run ruff check src/td_release_packager/deploy_launcher.py src/td_release_packager/cli.py src/td_release_packager/builder.py src/tests/test_deploy_launcher.py src/tests/test_builder_auto_split.py`
- `uv run pytest src/tests/test_deploy_launcher.py src/tests/test_builder_auto_split.py::TestBuildPackageAutoSplit::test_release_group_directory_manifest_for_split -q`
- `uv run python -m td_release_packager deploy --help`
- `uv run pytest src/tests/ -q`

