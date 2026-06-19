"""
test_tokenisation_report.py — Tests for the tokenisation preview (#325).

Covers the redaction helper (secret-safe display) and the tokenisation tab:
per-environment matrix, before/after rendering, masking of secrets, and the
collision / empty / undefined checks.
"""

from __future__ import annotations

from td_release_packager.reporting import redaction
from td_release_packager.reporting.tokenisation import (
    list_env_configs,
    parse_raw_conf,
    tokenisation_tab,
)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_secret_ref_detected(self):
        assert redaction.is_secret_ref("$env:DB_PWD")
        assert redaction.is_secret_ref("vault:secret/data#pwd")
        assert not redaction.is_secret_ref("A_D01")

    def test_sensitive_name_detected(self):
        assert redaction.is_sensitive_name("AUTH_PASSWORD")
        assert redaction.is_sensitive_name("api_key")
        assert not redaction.is_sensitive_name("ENV_PREFIX")

    def test_classify(self):
        assert redaction.classify("X", "$env:Y") == "secret-ref"
        assert redaction.classify("DB_SECRET", "literal") == "sensitive"
        assert redaction.classify("ENV_PREFIX", "A_D01") == "plain"

    def test_masked_display_never_reveals_secret(self):
        # Secret ref: resolved value must never appear.
        assert (
            redaction.masked_display("PWD", "$env:DB_PWD", "hunter2") == "«secret ref»"
        )
        # Sensitive name with a plain literal: still masked.
        assert (
            redaction.masked_display("DB_PASSWORD", "hunter2", "hunter2") == "«masked»"
        )
        # Empty plain value.
        assert redaction.masked_display("X", "", "") == "«empty»"
        # Plain value shown.
        assert redaction.masked_display("ENV_PREFIX", "A_D01", "A_D01") == "A_D01"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_env(project_dir, env, lines):
    d = project_dir / "config" / "env"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{env}.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_payload(project_dir, rel, content):
    p = project_dir / "payload" / "database" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_parse_raw_conf_does_not_resolve(tmp_path):
    """Raw parse keeps secret refs and nested tokens verbatim."""
    _write_env(tmp_path, "DEV", ["DB_PWD=$env:SECRET", "STD={{ENV_PREFIX}}_STD"])
    raw = parse_raw_conf(str(tmp_path / "config" / "env" / "DEV.conf"))
    assert raw["DB_PWD"] == "$env:SECRET"
    assert raw["STD"] == "{{ENV_PREFIX}}_STD"


def test_list_env_configs_sorted(tmp_path):
    _write_env(tmp_path, "PRD", ["SHIPS_ENV=PRD"])
    _write_env(tmp_path, "DEV", ["SHIPS_ENV=DEV"])
    envs = [name for name, _ in list_env_configs(str(tmp_path))]
    assert envs == ["DEV", "PRD"]


# ---------------------------------------------------------------------------
# tokenisation_tab
# ---------------------------------------------------------------------------


def test_no_env_configs_friendly_note(tmp_path):
    html = tokenisation_tab(str(tmp_path))
    assert "No environment configs found" in html


def test_no_tokens_friendly_note(tmp_path):
    _write_env(tmp_path, "DEV", ["ENV_PREFIX=A_D01"])
    _write_payload(tmp_path, "DDL/tables/DB.T.tbl", "CREATE TABLE DB.T (Id INT);")
    html = tokenisation_tab(str(tmp_path))
    assert "No" in html and "references" in html


def test_before_after_rendering(tmp_path):
    _write_env(tmp_path, "DEV", ["ENV_PREFIX=A_D01", "SHIPS_PROJECT=OMR"])
    _write_payload(
        tmp_path,
        "DDL/tables/T.tbl",
        "CREATE TABLE {{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD.Customer (Id INT);",
    )
    html = tokenisation_tab(str(tmp_path))
    # Matrix + examples present
    assert "Resolution by environment" in html
    assert "Rendered examples" in html
    # The "after" shows the resolved identifier
    assert "A_D01_OMR_STD.Customer" in html


def test_secret_value_never_rendered(tmp_path):
    _write_env(
        tmp_path,
        "DEV",
        ["DB_PASSWORD=hunter2", "HOST_REF=$env:DB_HOST", "ENV_PREFIX=A_D01"],
    )
    _write_payload(
        tmp_path,
        "DDL/authorizations/Auth.auth",
        "CREATE AUTHORIZATION {{ENV_PREFIX}}.A PASSWORD '{{DB_PASSWORD}}' "
        "HOST '{{HOST_REF}}';",
    )
    html = tokenisation_tab(str(tmp_path))
    # The actual secret values must never appear in the report
    assert "hunter2" not in html
    # Masked placeholders appear instead
    assert "«masked»" in html
    assert "«secret ref»" in html


def test_undefined_token_flagged_and_kept_literal(tmp_path):
    _write_env(tmp_path, "DEV", ["ENV_PREFIX=A_D01"])
    _write_payload(
        tmp_path,
        "DDL/tables/T.tbl",
        "CREATE TABLE {{ENV_PREFIX}}_{{MISSING_DB}}.T (Id INT);",
    )
    html = tokenisation_tab(str(tmp_path))
    assert "Undefined" in html
    assert "MISSING_DB" in html
    # Status badge for the env should be error-styled (✗ icon from common)
    assert "✗" in html


def test_empty_token_banner_carries_edit_hint(tmp_path):
    """An "Empty" warning must tell the operator which .conf to edit.

    Replicates the BionicCC_17 case where ``bootstrap-env-config``
    parked the referenced tokens in DEV.conf with empty values
    ready to be filled in. Without the hint, the report names the
    problem ("Empty") but not the fix.
    """
    _write_env(tmp_path, "DEV", ["DB_T="])  # defined but empty
    _write_payload(
        tmp_path,
        "DDL/tables/T.tbl",
        "CREATE TABLE {{DB_T}}.X (Id INT);",
    )
    html = tokenisation_tab(str(tmp_path))
    assert "Empty" in html
    assert "DB_T" in html
    # Banner points the operator at the exact file to edit
    assert "DEV.conf" in html
    assert "fill in the values" in html


def test_undefined_token_banner_carries_edit_hint(tmp_path):
    """The Undefined banner must also point at the .conf to edit."""
    _write_env(tmp_path, "DEV", ["ENV_PREFIX=A_D01"])
    _write_payload(
        tmp_path,
        "DDL/tables/T.tbl",
        "CREATE TABLE {{ENV_PREFIX}}_{{MISSING_DB}}.T (Id INT);",
    )
    html = tokenisation_tab(str(tmp_path))
    assert "Undefined" in html
    assert "DEV.conf" in html
    assert "fill in the values" in html


def test_collision_detected(tmp_path):
    _write_env(tmp_path, "DEV", ["DB_A=SAME", "DB_B=SAME"])
    _write_payload(
        tmp_path,
        "DDL/tables/T.tbl",
        "CREATE TABLE {{DB_A}}.X (Id INT); CREATE TABLE {{DB_B}}.Y (Id INT);",
    )
    html = tokenisation_tab(str(tmp_path))
    assert "Collisions" in html
    assert "DB_A" in html and "DB_B" in html


def test_real_vs_benign_collision_split(tmp_path):
    """Matrix splits Real (identity clobber) from Benign (scalar / env-label).

    A real clobber must render with the REAL badge; PERM_SPACE/SPOOL_SPACE
    sharing a value must classify as SCALAR (benign).
    """
    _write_env(
        tmp_path,
        "DEV",
        ["DB_A=SAME", "DB_B=SAME", "PERM_SPACE=1e9", "SPOOL_SPACE=1e9"],
    )
    # Two distinct sources resolving to the SAME physical name → clobber.
    # Both files name an object MyView, qualified by different tokens that
    # resolve to identical values.
    _write_payload(
        tmp_path,
        "DDL/views/{{DB_A}}.MyView.viw",
        "CREATE VIEW {{DB_A}}.MyView AS SELECT 1;",
    )
    _write_payload(
        tmp_path,
        "DDL/views/{{DB_B}}.MyView.viw",
        "CREATE VIEW {{DB_B}}.MyView AS SELECT 1 "
        "WHERE 1=1 AND PERM={{PERM_SPACE}} AND SPOOL={{SPOOL_SPACE}};",
    )
    html = tokenisation_tab(str(tmp_path))
    # Matrix headers now show both columns.
    assert "Real collisions" in html
    assert "Benign collisions" in html
    # The REAL badge appears for the identity clobber.
    assert "REAL" in html
    # Both DB tokens are surfaced.
    assert "DB_A" in html and "DB_B" in html


def test_scalar_collision_is_benign_not_real(tmp_path):
    """PERM/SPOOL pair classifies as SCALAR, not REAL."""
    _write_env(
        tmp_path,
        "DEV",
        ["ENV_PREFIX=A_D01", "PERM_SPACE=1e9", "SPOOL_SPACE=1e9"],
    )
    _write_payload(
        tmp_path,
        "DDL/tables/{{ENV_PREFIX}}.X.tbl",
        "CREATE TABLE {{ENV_PREFIX}}.X (Id INT) AS PERM = {{PERM_SPACE}}, "
        "SPOOL = {{SPOOL_SPACE}};",
    )
    html = tokenisation_tab(str(tmp_path))
    # SCALAR badge for the benign pair.
    assert "SCALAR" in html
    # Status badge is NOT error-styled (no real collisions).
    # A real clobber would show ✗; a benign-only matrix is at most warning (⚠).
    assert "✗" not in html


def test_multi_env_matrix_lists_all(tmp_path):
    _write_env(tmp_path, "DEV", ["ENV_PREFIX=A_D01"])
    _write_env(tmp_path, "PRD", ["ENV_PREFIX=A_P01"])
    _write_payload(
        tmp_path, "DDL/tables/T.tbl", "CREATE TABLE {{ENV_PREFIX}}_STD.T (Id INT);"
    )
    html = tokenisation_tab(str(tmp_path))
    assert "DEV" in html
    assert "PRD" in html


def test_tab_appears_in_pipeline_report(tmp_path):
    """End-to-end: the Tokenisation tab is wired into the pipeline report."""
    import json

    from td_release_packager.reporting import generate_pipeline_report

    (tmp_path / "ships.decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": {},
                "runs": [
                    {
                        "run_id": "r1",
                        "command": "scan",
                        "final_status": "success",
                        "duration_ms": 5,
                        "stages": [
                            {
                                "stage": "scan",
                                "status": "success",
                                "started_at": "2026-06-17T00:00:00+00:00",
                                "duration_ms": 5,
                                "inputs": {},
                                "outputs": {"unique_tokens": 1},
                                "issues": [],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_env(tmp_path, "DEV", ["ENV_PREFIX=A_D01"])
    _write_payload(
        tmp_path, "DDL/tables/T.tbl", "CREATE TABLE {{ENV_PREFIX}}_STD.T (Id INT);"
    )
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / "output" / "reports" / "pipeline_report.html").read_text(
        encoding="utf-8"
    )
    assert "tab-tokens" in html
    assert "Tokenisation" in html
