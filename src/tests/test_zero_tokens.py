"""
test_zero_tokens.py — Tests for the zero_tokens Inspect rule.

A deployable DDL or DML object that contains no {{TOKEN}} references has
hardcoded environment assumptions and cannot be safely promoted across
DEV → TST → PRD.  The rule fires at ERROR severity by default.

Covers:
    - Pass: file uses at least one {{TOKEN}} → no finding.
    - Fail: table with hardcoded database name → ERROR.
    - Fail: view with hardcoded database name → ERROR.
    - Fail: stored procedure with hardcoded names → ERROR.
    - Pass: system-scope object (MAP, ROLE) → not in scope, no finding.
    - Pass: file with token only in DB part → counts as tokenised, passes.
    - Integration: validate_directory surfaces zero_tokens findings.
    - Config: zero_tokens=WARNING reduces severity; zero_tokens=OFF suppresses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_release_packager.validate import (
    _check_zero_tokens,
    validate_directory,
    DEFAULT_RULES,
)


# ---------------------------------------------------------------
# Default severity
# ---------------------------------------------------------------


def test_zero_tokens_default_is_error():
    """zero_tokens defaults to ERROR — not WARNING or OFF."""
    assert DEFAULT_RULES["zero_tokens"] == "ERROR"


# ---------------------------------------------------------------
# Pass: file uses a token
# ---------------------------------------------------------------


def test_zero_tokens_pass_table_with_token():
    """Table file with {{DB}} token → no finding."""
    content = (
        "CREATE MULTISET TABLE {{MY_DB}}.Customer (\n"
        "     customer_id  INTEGER     NOT NULL\n"
        "    ,customer_name VARCHAR(100) NOT NULL\n"
        ") PRIMARY INDEX (customer_id);\n"
    )
    issues = _check_zero_tokens("ddl/Customer.tbl", content)
    assert issues == []


def test_zero_tokens_pass_view_with_token():
    """View file with {{V_DB}} token → no finding."""
    content = (
        "REPLACE VIEW {{V_DB}}.v_ActiveCustomers AS\n"
        "SELECT customer_id, customer_name\n"
        "FROM {{T_DB}}.Customer\n"
        "WHERE is_active = 1;\n"
    )
    issues = _check_zero_tokens("viw/v_ActiveCustomers.viw", content)
    assert issues == []


def test_zero_tokens_pass_procedure_with_token():
    """Stored procedure with {{MY_DB}} token → no finding."""
    content = (
        "REPLACE PROCEDURE {{MY_DB}}.sp_UpdateCustomer(\n"
        "    IN i_customer_id INTEGER\n"
        ")\n"
        "BEGIN\n"
        "    UPDATE {{MY_DB}}.Customer SET last_updated = CURRENT_TIMESTAMP\n"
        "    WHERE customer_id = :i_customer_id;\n"
        "END;\n"
    )
    issues = _check_zero_tokens("ddl/sp_UpdateCustomer.spl", content)
    assert issues == []


# ---------------------------------------------------------------
# Fail: zero token references in deployable objects
# ---------------------------------------------------------------


def test_zero_tokens_fail_hardcoded_table():
    """Table with hardcoded database name and no tokens → ERROR."""
    content = (
        "CREATE MULTISET TABLE DEV_MyDb.Customer (\n"
        "     customer_id  INTEGER     NOT NULL\n"
        ") PRIMARY INDEX (customer_id);\n"
    )
    issues = _check_zero_tokens("ddl/Customer.tbl", content)
    assert len(issues) == 1
    assert issues[0].rule == "zero_tokens"
    assert issues[0].severity == "ERROR"
    assert "{{TOKEN}}" in issues[0].message


def test_zero_tokens_fail_hardcoded_view():
    """View with hardcoded names and no tokens → ERROR."""
    content = (
        "REPLACE VIEW DEV_ViewDb.v_Orders AS\n"
        "SELECT order_id FROM DEV_TablesDb.Orders;\n"
    )
    issues = _check_zero_tokens("viw/v_Orders.viw", content)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"


def test_zero_tokens_fail_hardcoded_procedure():
    """Procedure with hardcoded database name and no tokens → ERROR."""
    content = (
        "REPLACE PROCEDURE ProdDb.sp_ProcessOrders()\n"
        "BEGIN\n"
        "    UPDATE ProdDb.Orders SET processed = 'Y';\n"
        "END;\n"
    )
    issues = _check_zero_tokens("ddl/sp_ProcessOrders.spl", content)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"


def test_zero_tokens_fail_no_qualifier_at_all():
    """Table with no database qualifier AND no tokens → ERROR.

    Covers the case where a developer has written raw SQL without any
    environment awareness at all — no hardcoded name, but also no token.
    Both the db_qualifier rule (missing qualifier) and this rule (zero tokens)
    fire on such a file.
    """
    content = (
        "CREATE MULTISET TABLE Customer (\n"
        "     customer_id  INTEGER     NOT NULL\n"
        ") PRIMARY INDEX (customer_id);\n"
    )
    issues = _check_zero_tokens("ddl/Customer.tbl", content)
    assert len(issues) == 1
    assert issues[0].rule == "zero_tokens"
    assert issues[0].severity == "ERROR"


def test_zero_tokens_fail_message_contains_guidance():
    """ERROR message mentions auto-tokenise as the remediation path."""
    content = "REPLACE VIEW BadDb.v_X AS SELECT 1 AS x;\n"
    issues = _check_zero_tokens("viw/v_X.viw", content)
    assert any("auto-tokenise" in i.message for i in issues)


# ---------------------------------------------------------------
# Pass: system-scope objects are excluded
# ---------------------------------------------------------------


def test_zero_tokens_skip_role():
    """GRANT ROLE with no tokens → not in scope (system-scope type)."""
    # MAP is classified as system-scope; ROLE is omitted from _CLASSIFY_PATTERNS
    # but MAP is caught — test with a MAP which IS in _CLASSIFY_PATTERNS.
    # For ROLE/PROFILE: they're in _VALIDATE_OMIT so don't appear in
    # _CLASSIFY_PATTERNS — the rule returns [] because obj_type is None.
    content = "CREATE ROLE my_deploy_role;\n"
    issues = _check_zero_tokens("00_system/my_deploy_role.rol", content)
    # ROLE is in _VALIDATE_OMIT → obj_type = None → no finding
    assert issues == []


def test_zero_tokens_skip_unrecognised_file():
    """File with no classifiable DDL object → no finding."""
    content = "-- This is a comment-only file with no DDL.\n"
    issues = _check_zero_tokens("notes.txt", content)
    assert issues == []


# ---------------------------------------------------------------
# Integration: validate_directory surfaces the finding
# ---------------------------------------------------------------


def test_zero_tokens_integration_validate_directory(tmp_path):
    """validate_directory with zero_tokens=ERROR reports the violation."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "HardCoded.tbl").write_text(
        "CREATE MULTISET TABLE ProdDb.HardCoded (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path))
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert len(issues) >= 1
    assert all(i.severity == "ERROR" for i in issues)
    assert not result.passed


def test_zero_tokens_integration_passes_with_token(tmp_path):
    """validate_directory with a properly tokenised file → no zero_tokens finding."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "Customer.tbl").write_text(
        "CREATE MULTISET TABLE {{MY_DB}}.Customer (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path))
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert issues == []


# ---------------------------------------------------------------
# Config: severity is tunable
# ---------------------------------------------------------------


def test_zero_tokens_config_warning(tmp_path):
    """zero_tokens=WARNING downgrades the severity for legacy migration."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "Legacy.tbl").write_text(
        "CREATE MULTISET TABLE LegacyDb.Legacy (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path), rules_config={"zero_tokens": "WARNING"})
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert len(issues) >= 1
    assert all(i.severity == "WARNING" for i in issues)
    # WARNING does not set passed=False on its own
    assert result.errors == 0


def test_zero_tokens_config_off(tmp_path):
    """zero_tokens=OFF suppresses the rule entirely."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "NoToken.tbl").write_text(
        "CREATE MULTISET TABLE SomeDb.NoToken (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path), rules_config={"zero_tokens": "OFF"})
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert issues == []
