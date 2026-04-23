# SHIPS Test Suite — README

## Prerequisites

Install `pytest` once. This is the only external dependency for the test suite.

```powershell
pip install pytest
```

## Directory Layout

The `tests/` folder must sit alongside the two source packages so that
Python's module resolution finds them without needing a `pip install` step:

```
src/
    td_release_packager/       ← Packager pipeline (Scaffold → Ship)
    ddl_deployer/              ← Deployment engine
    tests/                     ← Test suite (this directory)
        __init__.py
        conftest.py            ← Shared fixtures (temp projects, sample DDL)
        test_token_engine.py   ← Token interpolation, properties files
        test_ingest.py         ← DDL classification, harvest pipeline
        test_validate.py       ← Inspector / linter rules, --strict mode
        test_analyser.py       ← Dependency graph, structural anchors, topological sort, cycles
        test_graph_export.py   ← Graph export (DOT, Mermaid, JSON, CSV, OpenLineage)
        test_new_anchors.py    ← 8 new structural anchors (COLLECT STATS, CALL, EXEC, etc.)
        test_ddl_parser.py     ← Deployer's parser, intent detection
        test_build_counter.py  ← Build number management
        test_builder.py        ← Filename resolution, co-artefact handling
        test_deployer_models.py ← State machine, strategy mappings, scope mapping
```

All commands below assume your working directory is `src/`.

## Running the Full Suite

```powershell
python -m pytest tests/ -v --tb=short
```

| Argument      | What it does |
|---------------|-------------|
| `python -m pytest` | Runs pytest as a Python module. This is preferred over calling `pytest` directly because it adds the current directory to `sys.path`, ensuring `td_release_packager` and `ddl_deployer` are importable without a formal install. |
| `tests/`      | The directory to scan for test files. Pytest discovers any file matching the pattern `test_*.py`, and within each file discovers classes prefixed `Test` and functions prefixed `test_`. |
| `-v`          | **Verbose.** Prints each test on its own line with PASSED/FAILED status. Without this flag, pytest shows only a compact dot (`.`) per passing test. |
| `--tb=short`  | **Traceback style.** Controls how much detail is shown when a test fails. `short` shows the failing assertion and a few lines of context — enough to diagnose without flooding the terminal. Other options: `long` (full traceback), `line` (one line per failure), `no` (suppress tracebacks entirely). |

Expected output when all tests pass:

```
tests/test_analyser.py::TestStripNoise::test_line_comment_removed PASSED [  0%]
tests/test_analyser.py::TestStripNoise::test_block_comment_removed PASSED [  0%]
...
tests/test_new_anchors.py::TestAnchorInteractions::test_system_db_refs_excluded PASSED [100%]

============================= 474 passed in 2.90s ==============================
```

## Running a Subset of Tests

### Single module

Run only the tests in one file:

```powershell
python -m pytest tests/test_analyser.py -v
```

| Argument              | What it does |
|-----------------------|-------------|
| `tests/test_analyser.py` | Path to a single test file. Only tests within this file are collected and executed. |

### Single test class

Run only the tests within one class:

```powershell
python -m pytest tests/test_ddl_parser.py::TestDetectDeployIntent -v
```

| Argument | What it does |
|----------|-------------|
| `::TestDetectDeployIntent` | The `::` separator selects a specific node within the file. Here it selects the class `TestDetectDeployIntent`, running all `test_*` methods inside it. |

### Single test function

Run one specific test:

```powershell
python -m pytest tests/test_token_engine.py::TestResolveInternalReferences::test_chained_references -v
```

| Argument | What it does |
|----------|-------------|
| `::TestResolveInternalReferences::test_chained_references` | Two levels of `::` selection — class then method. Runs exactly one test. Useful when debugging a specific failure. |

### Keyword filtering

Run any test whose name contains a keyword:

```powershell
python -m pytest tests/ -k "multiset" -v
```

| Argument | What it does |
|----------|-------------|
| `-k "multiset"` | **Keyword expression.** Runs only tests whose full node ID contains the substring `multiset` (case-insensitive). Supports boolean operators: `-k "multiset and not inject"` runs multiset tests that are not about injection. |

## Useful Flags for Development

### Stop on first failure

```powershell
python -m pytest tests/ -x -v
```

| Argument | What it does |
|----------|-------------|
| `-x`     | **Exit on first failure.** Stops the entire run as soon as one test fails. Useful during active development — fix the first problem before worrying about downstream failures. |

### Stop after N failures

```powershell
python -m pytest tests/ --maxfail=3 -v
```

| Argument | What it does |
|----------|-------------|
| `--maxfail=3` | Stop the run after 3 failures. A middle ground between `-x` (stop at 1) and running everything. Useful when you suspect a single root cause is producing multiple failures and you want to see the pattern. |

### Quiet summary

```powershell
python -m pytest tests/ -q
```

| Argument | What it does |
|----------|-------------|
| `-q`     | **Quiet.** Minimal output — shows only dots for passes, `F` for failures, and a one-line summary. The opposite of `-v`. Good for CI pipelines or quick sanity checks. |

### Show print/log output

```powershell
python -m pytest tests/ -v -s
```

| Argument | What it does |
|----------|-------------|
| `-s`     | **No capture.** By default pytest captures `stdout` and `stderr` and only shows them for failing tests. `-s` disables capture, letting `print()` statements and logger output appear in real time. Useful when debugging with print statements. |

### Show local variables in tracebacks

```powershell
python -m pytest tests/ -v --tb=long -l
```

| Argument | What it does |
|----------|-------------|
| `--tb=long` | Full tracebacks with all stack frames. |
| `-l`     | **Show locals.** Includes the values of local variables in each stack frame of the traceback. Very helpful when an assertion fails and you need to see what values the code actually produced. |

### Run previously failed tests only

```powershell
python -m pytest tests/ --lf -v
```

| Argument | What it does |
|----------|-------------|
| `--lf`   | **Last failed.** Re-runs only the tests that failed in the previous run. Pytest stores this state in `.pytest_cache/`. Ideal for the fix-and-retest cycle — avoids re-running 470+ passing tests while you're working on a fix. |

### Run failed tests first, then the rest

```powershell
python -m pytest tests/ --ff -v
```

| Argument | What it does |
|----------|-------------|
| `--ff`   | **Failed first.** Runs previously-failed tests before the rest. If any fail again, you see them immediately rather than waiting for the full suite. |

## Test Coverage

### What is currently covered

| Module | Tests | Key areas |
|--------|------:|-----------|
| `test_token_engine.py` | 48 | Properties parsing, `{{TOKEN}}` resolution, circular refs, scanning, validation, substitution, token map I/O |
| `test_ingest.py` | 64 | DDL classification (all types), name extraction, MULTISET injection, REPLACE VIEW, token candidates, file discovery, ingest pipeline |
| `test_validate.py` | 60 | All linter rules, `--strict` mode, directory validation, configurable rules, inspect.conf I/O |
| `test_analyser.py` | 53 | Noise stripping, body extraction, structural-anchor reference scanning (11 original anchors), cycle detection, topological sort, `_waves.txt` generation, full analysis |
| `test_graph_export.py` | 51 | DOT, Mermaid, JSON, CSV, OpenLineage export. Edge direction, format validation, Gephi compatibility, NDJSON structure |
| `test_new_anchors.py` | 35 | 8 new structural anchors: COLLECT STATISTICS ON, CALL, EXEC/EXECUTE, LOCKING FOR, CREATE INDEX ON, RENAME TABLE, DROP object, COMMENT ON. Cross-anchor interactions, tokenised names, system DB exclusion |
| `test_ddl_parser.py` | 75 | Object type detection (all types), deploy intent, strategy derivation, MULTISET injection, name splitting, SPECIFIC function names |
| `test_build_counter.py` | 16 | Read, increment, atomic write, reset, `--no-increment` promotion |
| `test_builder.py` | 19 | Filename resolution across all object types, tokenised names, co-artefacts |
| `test_deployer_models.py` | 53 | State machine transitions, strategy mapping, scope mapping, SHOW commands, deploy ordering, result properties |
| **Total** | **474** | |

### What is not yet covered

| Module | Reason | Approach needed |
|--------|--------|----------------|
| `deployer.py` | Requires Teradata connection | Mock `teradatasql` cursor and connection |
| `scaffolder.py` | File/directory creation | `tmp_path` fixtures, verify generated structure |
| `wave_executor.py` | Concurrent execution, DB connection | Mock cursor, threading assertions |
| `report.py` | HTML/text output generation | Snapshot testing or string assertions |
| `preflight.py` | DBC system view queries | Mock cursor returning test data |
| `privilege_check.py` | DBC.AllRightsV queries | Mock cursor, script generation assertions |
| `manifest.py` | Thread safety, file I/O | `tmp_path` fixtures, concurrent write tests |

## Recommended Workflow

During a development iteration:

```powershell
# 1. Make your code change

# 2. Run the full suite
python -m pytest tests/ -v --tb=short

# 3. If something fails, isolate it
python -m pytest tests/test_analyser.py::TestScanReferences -v -s --tb=long -l

# 4. Fix and re-run only previously failed tests
python -m pytest tests/ --lf -v

# 5. Confirm everything is green
python -m pytest tests/ -v --tb=short
```
