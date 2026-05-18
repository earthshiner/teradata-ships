# PR: Resolve SQLJ JAR paths relative to install scripts

## Summary

Fixes JAR deployments where `.sjr` scripts reference binaries beside the script with paths such as `CJ!./GCFR_QB.jar`.

Teradata SQLJ client-file paths are opened by the client driver, so relative paths were being resolved from the Python process working directory instead of the `.sjr` file directory. The deployer now rewrites SQLJ `CJ!` paths to absolute paths relative to the owning script before execution.

## Changes

- Added SQLJ client-file path resolution for JAR direct execution.
- Kept resolution local to the executed SQL text so package metadata and source files remain unchanged.
- Avoided changing process cwd, which keeps parallel deployment waves safe.
- Added regression tests for direct path resolution and JAR deployment execution.

## Verification

- `uv run ruff format src/database_package_deployer/deployer.py src/tests/test_deployer_models.py`
- `uv run pytest src/tests/test_deployer_models.py::TestSqljClientFilePathResolution -q`
- `uv run pytest src/tests/ -q`

