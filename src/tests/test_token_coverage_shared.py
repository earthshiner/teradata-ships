"""
test_token_coverage_shared.py — PR2 tests.

Locks down the **PR2 invariant**: inspect's Step-0 token check now
shares package's coverage scanner, so a payload that references a
token undefined in any project env config fails inspect with the same
issue code (``TOKEN_UNDEFINED``) package would emit. Inspect can no
longer pass a payload that package would later reject on undefined
tokens — closing the structural betrayal documented in
``HANDOVER-ships-deterministic-deploy.md`` §PR2.

The contract is enforced at two levels:

  * Unit — ``validate_payload_token_coverage`` returns the expected
    shape for content tokens, filename tokens, multi-env, and the
    no-config degraded case.
  * Integration — coming in PR4's golden harness, which will assert
    that any payload that fails the new shared coverage scanner also
    fails inspect end-to-end. (Not in this PR; PR4 owns the harness.)
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.token_engine import validate_payload_token_coverage


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _scaffold_project(
    root: Path,
    payload_files: dict,
    env_configs: dict,
) -> Path:
    """Build a tiny SHIPS-like tree under ``root``.

    ``payload_files`` maps ``<relpath under payload/database>`` →
    content. ``env_configs`` maps ``<ENV_NAME>`` → text of
    ``config/env/<ENV_NAME>.conf``. Returns the project root.
    """
    payload = root / "payload" / "database"
    payload.mkdir(parents=True)
    for rel, body in payload_files.items():
        target = payload / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    env_dir = root / "config" / "env"
    env_dir.mkdir(parents=True)
    for name, body in env_configs.items():
        (env_dir / f"{name}.conf").write_text(body, encoding="utf-8")

    return root


# ---------------------------------------------------------------
# Coverage scanner
# ---------------------------------------------------------------


def test_no_env_configs_returns_empty_dict(tmp_path: Path) -> None:
    """No ``config/env/*.conf`` → empty result. Inspect treats this
    as ``coverage unverifiable`` rather than silent pass."""
    project = tmp_path / "project"
    (project / "payload" / "database").mkdir(parents=True)
    (project / "payload" / "database" / "X.tbl").write_text(
        "CREATE TABLE {{TOK}}.X (id INTEGER);", encoding="utf-8"
    )
    assert (
        validate_payload_token_coverage(
            str(project / "payload" / "database"), str(project)
        )
        == {}
    )


def test_all_tokens_defined_in_all_envs(tmp_path: Path) -> None:
    """Every payload token is defined in every env → no undefined."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DDL/tables/{{DB_PREFIX}}_STD_T.Customer.tbl": (
                "CREATE TABLE {{DB_PREFIX}}_STD_T.Customer (id INTEGER);"
            ),
        },
        env_configs={
            "DEV": "DB_PREFIX=devCallCentre\n",
            "PRD": "DB_PREFIX=prdCallCentre\n",
        },
    )
    result = validate_payload_token_coverage(
        str(project / "payload" / "database"), str(project)
    )
    assert set(result.keys()) == {"DEV", "PRD"}
    for env_summary in result.values():
        assert env_summary["undefined"] == []


def test_undefined_token_surfaced_per_env(tmp_path: Path) -> None:
    """A token referenced in the payload but absent from an env is
    reported under that env. The same token is reported across both
    envs when both lack it."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DDL/tables/Customer.tbl": (
                "CREATE TABLE {{DB_PREFIX}}_STD_T.Customer "
                "(id INTEGER, name VARCHAR(100));"
            ),
        },
        env_configs={
            "DEV": "OTHER_TOKEN=value\n",
            "PRD": "DB_PREFIX=prd\n",
        },
    )
    result = validate_payload_token_coverage(
        str(project / "payload" / "database"), str(project)
    )
    assert result["DEV"]["undefined"] == ["DB_PREFIX"]
    assert result["PRD"]["undefined"] == []
    # The undefined token is mapped to its source file path so the
    # inspect surface can render a clickable reference.
    assert result["DEV"]["token_files"]["DB_PREFIX"]
    assert all(
        path.endswith("Customer.tbl")
        for path in result["DEV"]["token_files"]["DB_PREFIX"]
    )


def test_filename_token_is_covered_by_scanner(tmp_path: Path) -> None:
    """The handover explicitly calls out filename tokens. A payload
    with no content tokens but a tokenised filename must still trigger
    coverage analysis for that token."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            # Filename is tokenised; content references no tokens.
            "DCL/inter_db/{{DB_PREFIX}}_BUS_V.dcl": (
                "GRANT SELECT ON SomeDb TO Other WITH GRANT OPTION;\n"
            ),
        },
        env_configs={"DEV": "OTHER_TOKEN=value\n"},
    )
    result = validate_payload_token_coverage(
        str(project / "payload" / "database"), str(project)
    )
    assert result["DEV"]["undefined"] == ["DB_PREFIX"]
    assert "DB_PREFIX" in result["DEV"]["filename_tokens"]
    # ``token_files`` (content references) stays empty for this token
    # because the only reference is in the filename.
    assert "DB_PREFIX" not in result["DEV"]["token_files"]


def test_undefined_lists_are_sorted(tmp_path: Path) -> None:
    """The undefined list is sorted so output is stable across runs —
    important for PR1 byte-determinism on downstream artefacts that
    embed it (e.g. ``ships.decisions.json``)."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DDL/tables/A.tbl": (
                "CREATE TABLE {{Z_TOKEN}}.A (id INTEGER);\n"
                "CREATE TABLE {{A_TOKEN}}.B (id INTEGER);\n"
                "CREATE TABLE {{M_TOKEN}}.C (id INTEGER);\n"
            ),
        },
        env_configs={"DEV": "# empty\n"},
    )
    result = validate_payload_token_coverage(
        str(project / "payload" / "database"), str(project)
    )
    assert result["DEV"]["undefined"] == ["A_TOKEN", "M_TOKEN", "Z_TOKEN"]


def test_combined_content_and_filename_dedup(tmp_path: Path) -> None:
    """A token referenced in both content AND a filename appears once
    in ``undefined`` and is tracked in both ``token_files`` and
    ``filename_tokens``."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DCL/inter_db/{{DB_PREFIX}}_BUS_V.dcl": (
                "GRANT SELECT ON {{DB_PREFIX}}_STD_T TO {{DB_PREFIX}}_BUS_V;\n"
            ),
        },
        env_configs={"DEV": "OTHER=value\n"},
    )
    result = validate_payload_token_coverage(
        str(project / "payload" / "database"), str(project)
    )
    assert result["DEV"]["undefined"] == ["DB_PREFIX"]
    assert "DB_PREFIX" in result["DEV"]["token_files"]
    assert "DB_PREFIX" in result["DEV"]["filename_tokens"]
