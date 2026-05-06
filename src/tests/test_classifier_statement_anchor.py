"""
test_classifier_statement_anchor.py — pin the start-of-statement
anchoring on the classifier's CREATE/REPLACE/GRANT patterns.

Bug: a file containing only a GRANT statement that lists CREATE
PROCEDURE among its privileges (``GRANT CREATE PROCEDURE, ALTER
PROCEDURE ON db TO user``) used to misclassify as PROCEDURE. The
substring ``CREATE PROCEDURE`` matched the unanchored pattern mid-
line, the file was renamed ``.spl`` and dropped into
``DDL/procedures/`` -- wildly wrong for a permissions script.

Fix: every type-specific pattern is anchored to start-of-statement
via ``^\\s*`` plus ``re.MULTILINE``. These tests pin the new
behaviour for the canonical ``classifier`` module AND its duplicate
copies in ``validate`` and ``analyser`` so a future drift
between them re-introducing the bug fails CI immediately.
"""

from __future__ import annotations

from td_release_packager.classifier import classify


# ---------------------------------------------------------------
# The reported case: GCFR Step_03_AssignPermissions
# ---------------------------------------------------------------


GCFR_GRANT_FILE = (
    ".IF ERRORCODE <> 0 THEN .GOTO ERR\n"
    "\n"
    "GRANT CREATE PROCEDURE, CREATE EXTERNAL PROCEDURE, "
    "ALTER EXTERNAL PROCEDURE ON $GCFR_P_UT TO $ADMIN_USER;\n"
)


class TestGrantWithProcedurePrivileges:
    """The headline regression: GRANT statement listing CREATE
    PROCEDURE / ALTER PROCEDURE privileges must classify as GRANT,
    not PROCEDURE."""

    def test_grant_create_procedure_classifies_as_grant(self):
        result = classify("Step_03_AssignPermissions.spl", GCFR_GRANT_FILE)
        assert result.type == "GRANT"

    def test_grant_create_procedure_emits_filename_mismatch_warning(self):
        """The .spl extension says PROCEDURE but content is GRANT --
        warn so the operator can rename the source file."""
        result = classify("Step_03_AssignPermissions.spl", GCFR_GRANT_FILE)
        mismatch_warnings = [w for w in result.warnings if "Filename mismatch" in w]
        assert len(mismatch_warnings) == 1
        assert "GRANT" in mismatch_warnings[0]

    def test_grant_with_proc_privileges_correct_extension(self):
        """When the same content lives in a .dcl file, it
        classifies cleanly with no filename-mismatch warning."""
        result = classify("Step_03_AssignPermissions.dcl", GCFR_GRANT_FILE)
        assert result.type == "GRANT"
        assert not any("Filename mismatch" in w for w in result.warnings)


# ---------------------------------------------------------------
# Defence-in-depth: other CREATE-substring-in-GRANT cases
# ---------------------------------------------------------------


class TestGrantPrivilegeSubstringsDoNotLeak:
    """Every CREATE-foo privilege name a GRANT statement might list
    must NOT trigger the matching CREATE-foo classifier."""

    def test_grant_create_table_classifies_as_grant(self):
        ddl = "GRANT CREATE TABLE ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_grant_create_view_classifies_as_grant(self):
        ddl = "GRANT CREATE VIEW ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_grant_create_macro_classifies_as_grant(self):
        ddl = "GRANT CREATE MACRO ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_grant_create_function_classifies_as_grant(self):
        ddl = "GRANT CREATE FUNCTION ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_grant_create_trigger_classifies_as_grant(self):
        ddl = "GRANT CREATE TRIGGER ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_grant_create_database_classifies_as_grant(self):
        ddl = "GRANT CREATE DATABASE ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_grant_create_user_classifies_as_grant(self):
        ddl = "GRANT CREATE USER ON MyDB TO MyRole;"
        assert classify("any.dcl", ddl).type == "GRANT"

    def test_revoke_create_procedure_classifies_as_revoke(self):
        ddl = "REVOKE CREATE PROCEDURE ON MyDB FROM MyRole;"
        assert classify("any.dcl", ddl).type == "REVOKE"

    def test_grant_multiple_create_privileges(self):
        ddl = (
            "GRANT CREATE PROCEDURE, CREATE TABLE, CREATE VIEW, "
            "CREATE MACRO ON MyDB TO MyRole;"
        )
        assert classify("any.dcl", ddl).type == "GRANT"


# ---------------------------------------------------------------
# Real CREATE statements still classify correctly
# ---------------------------------------------------------------


class TestRealCreateStatementsStillWork:
    """Anchoring must not regress any of the canonical CREATE
    happy paths."""

    def test_plain_create_table(self):
        ddl = "CREATE MULTISET TABLE MyDB.T (Id INT) PRIMARY INDEX (Id);"
        assert classify("MyDB.T.tbl", ddl).type == "TABLE"

    def test_plain_create_view(self):
        ddl = "CREATE VIEW MyDB.V AS SELECT 1;"
        assert classify("MyDB.V.viw", ddl).type == "VIEW"

    def test_plain_create_procedure(self):
        ddl = "CREATE PROCEDURE MyDB.sp_X () BEGIN SET v = 1; END;"
        # Sub-type defaults to PROCEDURE_SPL when no LANGUAGE clause.
        assert classify("MyDB.sp_X.spl", ddl).type == "PROCEDURE_SPL"

    def test_plain_create_function(self):
        ddl = (
            "CREATE FUNCTION MyDB.fn_X (x INT) RETURNS INT "
            "LANGUAGE SQL CONTAINS SQL DETERMINISTIC RETURN x * 2;"
        )
        assert classify("MyDB.fn_X.fnc", ddl).type == "FUNCTION_SQL"

    def test_create_with_leading_whitespace(self):
        ddl = "    \n  CREATE MULTISET TABLE MyDB.T (Id INT);"
        assert classify("any.tbl", ddl).type == "TABLE"

    def test_create_with_leading_bteq_command(self):
        """A real CREATE TABLE preceded by a BTEQ control command
        on its own line still classifies as TABLE."""
        ddl = (
            ".LOGON dbc/dbc\n"
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n"
            "\n"
            "CREATE MULTISET TABLE MyDB.Customer (Id INT) "
            "PRIMARY INDEX (Id);\n"
            ".LOGOFF\n"
        )
        assert classify("MyDB.Customer.tbl", ddl).type == "TABLE"

    def test_replace_view_classifies(self):
        ddl = "REPLACE VIEW MyDB.V AS SELECT 1;"
        assert classify("MyDB.V.viw", ddl).type == "VIEW"

    def test_create_procedure_with_multiline_body(self):
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X (IN x INTEGER)\n"
            "BEGIN\n"
            "    DECLARE v INTEGER;\n"
            "    SET v = x * 2;\n"
            "    UPDATE MyDB.t SET col = v;\n"
            "END;\n"
        )
        assert classify("MyDB.sp_X.spl", ddl).type == "PROCEDURE_SPL"


# ---------------------------------------------------------------
# Procedure body containing GRANT-like text
# ---------------------------------------------------------------


class TestProcedureBodyWithGrantText:
    """A procedure whose body mentions GRANT in dynamic SQL or
    comments must still classify as PROCEDURE."""

    def test_procedure_with_grant_in_dynamic_sql_string(self):
        """The dynamic-SQL string literal mentions GRANT but the
        leading verb is CREATE PROCEDURE, so PROCEDURE wins."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_GrantStuff ()\n"
            "BEGIN\n"
            "    DECLARE vSQL VARCHAR(500);\n"
            "    SET vSQL = 'GRANT SELECT ON foo TO bar';\n"
            "    CALL DBC.SysExecSQL(:vSQL);\n"
            "END;\n"
        )
        assert classify("MyDB.sp_GrantStuff.spl", ddl).type == "PROCEDURE_SPL"

    def test_procedure_with_replace_in_body_still_procedure(self):
        """Defence in depth: a procedure body that includes a
        REPLACE VIEW in dynamic SQL still classifies as PROCEDURE
        because the leading verb at line 1 is CREATE PROCEDURE."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X ()\n"
            "BEGIN\n"
            "    SET vSQL = 'REPLACE VIEW MyDB.tmp AS SELECT 1';\n"
            "END;\n"
        )
        assert classify("MyDB.sp_X.spl", ddl).type == "PROCEDURE_SPL"


# ---------------------------------------------------------------
# Mirror-copy parity: validate.py and analyser.py
# ---------------------------------------------------------------


class TestDuplicateClassifierTablesMatch:
    """The pattern lists in validate.py and analyser.py must match
    classifier.py's behaviour on the GCFR case. Without these
    parity tests, the duplicates can drift silently and the bug
    re-emerges in only some stages."""

    def test_validate_duplicate_classifies_grant_as_grant_via_db_qualifier(self):
        """validate.py's _CLASSIFY_PATTERNS gates several rule
        checks. _check_db_qualifier short-circuits for system-scope
        types — a GRANT file should NOT trigger the db_qualifier
        ERROR because GRANT has no qualified Database.Object name."""
        from td_release_packager.validate import _check_db_qualifier

        issues = _check_db_qualifier("any.dcl", GCFR_GRANT_FILE)
        # Pre-fix, this misclassified as PROCEDURE and then complained
        # about the missing qualifier on "EXTERNAL" (the next token).
        # Post-fix, the pattern doesn't match PROCEDURE so the rule
        # exits cleanly — no spurious ERROR.
        assert all(i.rule != "db_qualifier" for i in issues)

    def test_analyser_pattern_classifies_grant_with_create_procedure_as_grant(self):
        """analyser.py now includes GRANT in _CLASSIFY_PATTERNS so that
        grant files appear in _waves.txt with correct ordering.

        The original concern was misclassification as PROCEDURE. A GRANT
        listing CREATE PROCEDURE as a privilege should match GRANT, not
        PROCEDURE (PROCEDURE requires CREATE at line start). Verify the
        result is GRANT, not PROCEDURE or None."""
        from td_release_packager.analyser import _CLASSIFY_PATTERNS

        type_for = None
        for pattern, type_ in _CLASSIFY_PATTERNS:
            if pattern.search(GCFR_GRANT_FILE):
                type_for = type_
                break
        # Must classify as GRANT (not PROCEDURE). The PROCEDURE pattern
        # requires CREATE at line start; GRANT CREATE PROCEDURE has GRANT
        # at line start, so the GRANT pattern wins.
        assert type_for == "GRANT"


# ---------------------------------------------------------------
# The second GCFR regression: GRANT with a single CREATE PROCEDURE
# privilege landing as ``ON.dcl`` (the harvester picked ``ON`` as
# the eponymous object name).
# ---------------------------------------------------------------


GCFR_ON_DCL_FILE = (
    ".IF ERRORCODE <> 0 THEN .GOTO ERR\n"
    "\n"
    "GRANT CREATE PROCEDURE ON $GCFR_P_PP TO $ADMIN_USER;\n"
)


class TestGrantSinglePrivilegeOnFile:
    """The second GCFR reproducer: a GRANT statement with exactly
    one CREATE PROCEDURE privilege made the eponymous-rename
    extractor capture ``ON`` (the next token after PROCEDURE) as
    the object name. Result: file harvested as ``ON.dcl`` and
    inspect's db_qualifier rule complained about ``Object 'ON'
    missing database qualifier``."""

    def test_classifier_returns_grant(self):
        result = classify("any.dcl", GCFR_ON_DCL_FILE)
        assert result.type == "GRANT"

    def test_eponymous_extractor_returns_no_name(self):
        from td_release_packager.eponymous_rename import extract_eponymous_name

        # Pre-fix: returned ('ON.spl', 'ON', 'PROCEDURE') -- the
        # rename then staged the file as ON.spl. Post-fix: returns
        # None because no anchored DDL verb matches the file.
        assert extract_eponymous_name(GCFR_ON_DCL_FILE) is None

    def test_db_qualifier_rule_does_not_misfire(self):
        from td_release_packager.validate import _check_db_qualifier

        # Pre-fix: _QUALIFIED_NAME_RE captured 'ON' as the object
        # name (no database qualifier) and raised a db_qualifier
        # ERROR. Post-fix: the anchored regex doesn't match and
        # the rule exits cleanly.
        issues = _check_db_qualifier("ON.dcl", GCFR_ON_DCL_FILE)
        assert all(i.rule != "db_qualifier" for i in issues)


# ---------------------------------------------------------------
# Latent shadowing fix: validate.py used to define a local
# _strip_sql_comments that shadowed the imported variant. The local
# function only stripped SQL comments, not string literals -- so
# dynamic-SQL strings like ``'CREATE TABLE'`` inside procedure
# bodies could still trigger qualifier / multiset warnings.
# ---------------------------------------------------------------


class TestStripCommentsAlsoStripsStringLiterals:
    """validate.py's ``_strip_sql_comments`` is the imported
    ``strip_comments_and_string_literals`` (no local override).
    A regression here means dynamic-SQL string literals leak into
    rule scans again."""

    def test_strip_function_blanks_string_literals(self):
        from td_release_packager.validate import _strip_sql_comments

        # The literal 'CREATE TABLE foo' must be blanked, not just
        # the comment around it.
        content = "SET vSQL = 'CREATE TABLE foo';"
        cleaned = _strip_sql_comments(content)
        assert "CREATE TABLE" not in cleaned

    def test_procedure_with_create_table_in_string_literal_no_set_multiset(
        self, tmp_path
    ):
        """End-to-end: a procedure whose body builds a CREATE TABLE
        via dynamic SQL must NOT trigger set_multiset."""
        from td_release_packager.validate import validate_directory

        ddl_dir = tmp_path / "DDL" / "procedures"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDb.foo.spl").write_text(
            "CREATE PROCEDURE MyDb.foo (IN iName VARCHAR(128))\n"
            "BEGIN\n"
            "    DECLARE vSQL VARCHAR(1000);\n"
            "    SET vSQL = 'CREATE TABLE ' || iName || ' (id INT)';\n"
            "    CALL DBC.SysExecSQL(:vSQL);\n"
            "END;\n",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))
        triggered = {i.rule for i in result.issues if "foo.spl" in i.file}
        assert "set_multiset" not in triggered
