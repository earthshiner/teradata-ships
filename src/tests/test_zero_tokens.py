"""
test_zero_tokens.py — Tests for the zero_tokens Inspect rule.

The rule enforces that every deployable DDL/DML source file is usable by SHIPS
for tokenisation.  Three cases:

    1. {{TOKEN}} present  → PASS  (already tokenised)
    2. Hardcoded Database.Object name  → PASS  (SHIPS can auto-tokenise it)
    3. No qualifier AND no token  → ERROR  (SHIPS has nothing to work with)

Only case 3 raises an ERROR.  Case 2 is handled by the separate hardcoded_name
WARNING rule.
"""

from __future__ import annotations


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
# Case 1 — PASS: file already has {{TOKEN}} references
# ---------------------------------------------------------------


def test_zero_tokens_pass_table_with_token():
    """Table with {{DB}} token → no finding (case 1)."""
    content = (
        "CREATE MULTISET TABLE {{MY_DB}}.Customer (\n"
        "     customer_id  INTEGER     NOT NULL\n"
        ") PRIMARY INDEX (customer_id);\n"
    )
    assert _check_zero_tokens("ddl/Customer.tbl", content) == []


def test_zero_tokens_pass_view_with_token():
    """View with {{V_DB}} token → no finding (case 1)."""
    content = (
        "REPLACE VIEW {{V_DB}}.v_ActiveCustomers AS\n"
        "SELECT customer_id FROM {{T_DB}}.Customer;\n"
    )
    assert _check_zero_tokens("viw/v_ActiveCustomers.viw", content) == []


def test_zero_tokens_pass_procedure_with_token():
    """Procedure with {{MY_DB}} token → no finding (case 1)."""
    content = (
        "REPLACE PROCEDURE {{MY_DB}}.sp_Update(IN i_id INTEGER)\n"
        "BEGIN\n"
        "    UPDATE {{MY_DB}}.Customer SET ts = CURRENT_TIMESTAMP\n"
        "    WHERE id = :i_id;\n"
        "END;\n"
    )
    assert _check_zero_tokens("ddl/sp_Update.spl", content) == []


# ---------------------------------------------------------------
# Case 2 — PASS: hardcoded qualifier present (SHIPS can auto-tokenise)
# ---------------------------------------------------------------


def test_zero_tokens_pass_hardcoded_table():
    """Table with hardcoded database name → PASS (SHIPS can auto-tokenise it).

    The separate hardcoded_name WARNING rule surfaces this for the developer.
    This rule does not duplicate that finding.
    """
    content = (
        "CREATE MULTISET TABLE DevDb.Customer (\n"
        "     customer_id  INTEGER     NOT NULL\n"
        ") PRIMARY INDEX (customer_id);\n"
    )
    assert _check_zero_tokens("ddl/Customer.tbl", content) == []


def test_zero_tokens_pass_hardcoded_view():
    """View with hardcoded database names → PASS (case 2)."""
    content = "REPLACE VIEW DevViews.v_Orders AS SELECT id FROM DevTables.Orders;\n"
    assert _check_zero_tokens("viw/v_Orders.viw", content) == []


def test_zero_tokens_pass_hardcoded_procedure():
    """Procedure with hardcoded database name → PASS (case 2)."""
    content = (
        "REPLACE PROCEDURE ProdDb.sp_Process()\n"
        "BEGIN\n"
        "    UPDATE ProdDb.Orders SET done = 'Y';\n"
        "END;\n"
    )
    assert _check_zero_tokens("ddl/sp_Process.spl", content) == []


# ---------------------------------------------------------------
# Case 3 — FAIL: no qualifier AND no token (SHIPS cannot help)
# ---------------------------------------------------------------


def test_zero_tokens_fail_no_qualifier_no_token_table():
    """Table with no database qualifier and no token → ERROR (case 3).

    This is the case SHIPS cannot auto-tokenise: there is no database name
    to detect and replace.  The developer must add a qualifier themselves.
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


def test_zero_tokens_fail_no_qualifier_no_token_view():
    """View with no qualifier and no token → ERROR (case 3)."""
    content = "REPLACE VIEW v_Orders AS SELECT id FROM Orders;\n"
    issues = _check_zero_tokens("viw/v_Orders.viw", content)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"


def test_zero_tokens_fail_no_qualifier_no_token_procedure():
    """Procedure with no qualifier and no token → ERROR (case 3)."""
    content = (
        "REPLACE PROCEDURE sp_Process()\n"
        "BEGIN\n"
        "    UPDATE Orders SET done = 'Y';\n"
        "END;\n"
    )
    issues = _check_zero_tokens("ddl/sp_Process.spl", content)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"


def test_zero_tokens_fail_message_mentions_qualifier():
    """Error message tells the developer to add a database qualifier."""
    content = "REPLACE VIEW v_X AS SELECT 1 AS x;\n"
    issues = _check_zero_tokens("viw/v_X.viw", content)
    assert any("qualifier" in i.message.lower() for i in issues)


# ---------------------------------------------------------------
# DML seed/registration scripts — qualifier lives in the statement
# target (INSERT INTO Db.Object), not a CREATE clause (issue #410)
# ---------------------------------------------------------------


def test_zero_tokens_pass_qualified_dml_insert():
    """Fully-qualified INSERT seed → PASS (case 2b).

    Regression for the false ERROR where a qualified DML file
    (e.g. CustomerDNA_SEM_STD_T.value_domain.dml) tripped zero_tokens
    because the DDL-shaped qualifier regex ignored INSERT INTO.
    """
    content = (
        "INSERT INTO CustomerDNA_SEM_STD_T.value_domain "
        "( domain_name, domain_value, value_label, sort_order )\n"
        "VALUES ( 'booking_channel', 'WEB', 'Website', 1 );\n"
        "INSERT INTO CustomerDNA_SEM_STD_T.value_domain "
        "( domain_name, domain_value, value_label, sort_order )\n"
        "VALUES ( 'member_status', 'ACTIVE', 'Active', 1 );\n"
    )
    assert (
        _check_zero_tokens("DML/CustomerDNA_SEM_STD_T.value_domain.dml", content) == []
    )


def test_zero_tokens_pass_tokenised_dml_insert():
    """INSERT into a {{TOKEN}}.Object target → PASS (case 1)."""
    content = "INSERT INTO {{SEM_DB}}.value_domain (a) VALUES ('x');\n"
    assert _check_zero_tokens("DML/value_domain.dml", content) == []


def test_zero_tokens_pass_qualified_dml_update_delete_merge():
    """UPDATE / DELETE / MERGE with a qualified target → PASS (case 2b)."""
    for stmt in (
        "UPDATE CustomerDNA_DOM_STD_T.member SET status = 'A';\n",
        "DELETE FROM CustomerDNA_DOM_STD_T.member WHERE id = 1;\n",
        "DELETE CustomerDNA_DOM_STD_T.member WHERE id = 1;\n",
        "MERGE INTO CustomerDNA_DOM_STD_T.member AS t USING s ON t.id = s.id;\n",
    ):
        assert _check_zero_tokens("DML/seed.dml", stmt) == [], stmt


def test_zero_tokens_fail_unqualified_dml():
    """Unqualified INSERT target → ERROR (case 3) — the fix must not
    relax this."""
    content = "INSERT INTO value_domain (a) VALUES ('x');\n"
    issues = _check_zero_tokens("DML/value_domain.dml", content)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"


def test_zero_tokens_fail_unqualified_dml_with_dotted_literal():
    """A dotted string literal in an unqualified INSERT must NOT be
    mistaken for a qualifier — the target regex is anchored to the
    statement, so this still ERRORs (case 3)."""
    content = "INSERT INTO value_domain (host) VALUES ('db.table.col');\n"
    issues = _check_zero_tokens("DML/value_domain.dml", content)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"


# ---------------------------------------------------------------
# Excluded: system-scope objects and unclassifiable files
# ---------------------------------------------------------------


def test_zero_tokens_skip_role():
    """ROLE is in _VALIDATE_OMIT → obj_type is None → no finding."""
    content = "CREATE ROLE my_deploy_role;\n"
    assert _check_zero_tokens("00_system/my_deploy_role.rol", content) == []


def test_zero_tokens_skip_unrecognised_file():
    """File with no classifiable DDL content → no finding."""
    content = "-- Comment-only file, no DDL.\n"
    assert _check_zero_tokens("notes.txt", content) == []


def test_zero_tokens_skip_payload_directory():
    """Files under payload/ are already resolved — not checked."""
    content = "CREATE MULTISET TABLE SomeDb.t (id INT) PRIMARY INDEX (id);\n"
    # Path contains 'payload' component — rule is skipped
    assert _check_zero_tokens("payload/03_ddl/tables/SomeDb.t.tbl", content) == []


# ---------------------------------------------------------------
# Integration with validate_directory
# ---------------------------------------------------------------


def test_zero_tokens_integration_no_qualifier_fails(tmp_path):
    """validate_directory surfaces zero_tokens when no qualifier present."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "NoQual.tbl").write_text(
        "CREATE MULTISET TABLE NoQual (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path))
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert len(issues) >= 1
    assert not result.passed


def test_zero_tokens_integration_hardcoded_passes(tmp_path):
    """validate_directory does NOT raise zero_tokens for a hardcoded qualifier."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "Customer.tbl").write_text(
        "CREATE MULTISET TABLE DevDb.Customer (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path))
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert issues == []


def test_zero_tokens_integration_token_passes(tmp_path):
    """validate_directory does NOT raise zero_tokens when a token is present."""
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
# Severity is configurable
# ---------------------------------------------------------------


def test_zero_tokens_config_warning(tmp_path):
    """zero_tokens=WARNING downgrades severity without blocking Package."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "NoQual.tbl").write_text(
        "CREATE MULTISET TABLE NoQual (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path), rules_config={"zero_tokens": "WARNING"})
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert len(issues) >= 1
    assert all(i.severity == "WARNING" for i in issues)
    assert result.errors == 0


def test_zero_tokens_config_off(tmp_path):
    """zero_tokens=OFF suppresses the rule entirely."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "NoQual.tbl").write_text(
        "CREATE MULTISET TABLE NoQual (id INT) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )
    result = validate_directory(str(tmp_path), rules_config={"zero_tokens": "OFF"})
    issues = [i for i in result.issues if i.rule == "zero_tokens"]
    assert issues == []
