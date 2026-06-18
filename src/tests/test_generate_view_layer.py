"""
test_generate_view_layer.py — Unit and integration tests for the
Object Placement Standard view layer generator.

Layered tests:
    1. Token & filename helpers
    2. Column extraction from CREATE TABLE
    3. Locking view emission
    4. FROM/JOIN parsing
    5. SELECT * expansion (unqualified, alias.*, collision)
    6. _T to _V reference rewrite (with comment/string masking)
    7. Database & grants emission
    8. Idempotent file write
    9. End-to-end run() across a fixture project

Author: Paul Dancer / Ecosystem Architect - Teradata Field Technology Group
"""

from pathlib import Path

import pytest

# Engine lives in the package; the ``tools/generate_view_layer.py``
# CLI shim is just an entry point, not the source of truth.
from td_release_packager.view_layer_generator import (
    TableSpec,
    _build_arg_parser,
    companion_token,
    expand_select_star,
    generate_database_ddl,
    generate_grant_ddl,
    generate_locking_view_ddl,
    is_locking_view,
    parse_from_clause,
    parse_module_token,
    parse_table_columns,
    render_column_list,
    rewrite_tables_to_views,
    run,
    split_object_filename,
    write_if_different,
)


# ===========================================================================
# Sample DDL — used across multiple tests
# ===========================================================================

MORTGAGE_TBL = """\
CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Mortgage
    ,FALLBACK
    ,NO BEFORE JOURNAL
    ,NO AFTER JOURNAL
(
     Mortgage_Id     INTEGER NOT NULL
    ,Applicant_Name  VARCHAR(200) NOT NULL
    ,Property_Id     INTEGER
    ,Loan_Amount     DECIMAL(15,2)
    ,Application_Date DATE
)
PRIMARY INDEX (Mortgage_Id)
;
"""

PROPERTY_TBL = """\
CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Property
    ,FALLBACK
(
     Property_Id      INTEGER NOT NULL
    ,Property_Address VARCHAR(500)
    ,Property_Value   DECIMAL(15,2)
    ,Application_Date DATE
)
PRIMARY INDEX (Property_Id)
;
"""

# Edge-case table with constraints, nested type parens, quoted identifier.
COMPLEX_TBL = """\
-- comment with fake column
/* block comment with FROM x.y */
CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Complex
    ,FALLBACK
(
     id              INTEGER NOT NULL
    ,"quoted col"    VARCHAR(50)
    ,price           DECIMAL(15,2) NOT NULL
    ,note            VARCHAR(100) CHARACTER SET LATIN
    ,CONSTRAINT pk_complex PRIMARY KEY (id)
)
PRIMARY INDEX (id)
;
"""

ENRICHED_VIEW = """\
-- AI-Native Data Product: Domain Module
-- Mortgage_Enhanced: joins mortgage with property data
CREATE VIEW {{DOM_DATABASE_V}}.Mortgage_Enhanced AS
LOCKING ROW FOR ACCESS
SELECT *
FROM {{DOM_DATABASE_T}}.Mortgage m
INNER JOIN {{DOM_DATABASE_T}}.Property p
    ON m.Property_Id = p.Property_Id
;
"""

CURRENT_VIEW = """\
-- AI-Native Data Product: Domain Module
CREATE VIEW {{DOM_DATABASE_V}}.Mortgage_Current AS
LOCKING ROW FOR ACCESS
SELECT m.*
FROM {{DOM_DATABASE_T}}.Mortgage m
WHERE m.Application_Date >= DATE '2024-01-01'
;
"""

CROSS_MODULE_VIEW = """\
-- SLA_Check: SEM view referencing Domain
CREATE VIEW {{SEM_DATABASE_V}}.SLA_Check AS
LOCKING ROW FOR ACCESS
SELECT m.Mortgage_Id, m.Loan_Amount
FROM {{DOM_DATABASE_V}}.Mortgage m
;
"""


# ===========================================================================
# Section 1 — Token & filename helpers
# ===========================================================================


class TestTokenHelpers:
    """parse_module_token / companion_token / split_object_filename."""

    def test_parse_t_token(self):
        assert parse_module_token("{{DOM_DATABASE_T}}") == ("DOM", "T")

    def test_parse_v_token(self):
        assert parse_module_token("{{SEM_DATABASE_V}}") == ("SEM", "V")

    def test_parse_compound_module(self):
        assert parse_module_token("{{DATA_OBS_DATABASE_T}}") == (
            "DATA_OBS",
            "T",
        )

    def test_parse_non_database_t_token(self):
        assert parse_module_token("{{DB_DOMAIN_T}}") == ("DB_DOMAIN", "T")

    def test_parse_invalid_returns_none(self):
        assert parse_module_token("{{DOM_DATABASE}}") is None
        assert parse_module_token("DOM_DATABASE_T") is None
        assert parse_module_token("{{lower_DATABASE_T}}") is None

    def test_companion_t_to_v(self):
        assert companion_token("{{DOM_DATABASE_T}}") == "{{DOM_DATABASE_V}}"

    def test_companion_v_to_t(self):
        assert companion_token("{{SEM_DATABASE_V}}") == "{{SEM_DATABASE_T}}"

    def test_companion_non_database_token(self):
        assert companion_token("{{DB_DOMAIN_T}}") == "{{DB_DOMAIN_V}}"

    def test_companion_invalid_returns_none(self):
        assert companion_token("{{NOT_A_TOKEN}}") is None

    def test_split_filename_basic(self):
        result = split_object_filename("{{DOM_DATABASE_T}}.Mortgage.tbl")
        assert result == ("{{DOM_DATABASE_T}}", "Mortgage", ".tbl")

    def test_split_filename_with_underscore_object(self):
        result = split_object_filename("{{DOM_DATABASE_V}}.Mortgage_Current.viw")
        assert result == (
            "{{DOM_DATABASE_V}}",
            "Mortgage_Current",
            ".viw",
        )

    def test_split_filename_no_token_returns_none(self):
        assert split_object_filename("Mortgage.tbl") is None

    def test_split_filename_no_extension_returns_none(self):
        assert split_object_filename("{{DOM_DATABASE_T}}.Mortgage") is None


# ===========================================================================
# Section 2 — Column extraction
# ===========================================================================


class TestColumnParser:
    """parse_table_columns — the careful, depth-counting parser."""

    def test_basic_table(self):
        cols = parse_table_columns(MORTGAGE_TBL)
        assert cols == [
            "Mortgage_Id",
            "Applicant_Name",
            "Property_Id",
            "Loan_Amount",
            "Application_Date",
        ]

    def test_decimal_with_internal_comma(self):
        # DECIMAL(15,2) — the comma is inside parens, so the column
        # block must NOT split there.
        ddl = (
            "CREATE TABLE D.X (\n"
            "     id INTEGER\n"
            "    ,price DECIMAL(15,2)\n"
            "    ,name VARCHAR(100)\n"
            ") PRIMARY INDEX (id);\n"
        )
        cols = parse_table_columns(ddl)
        assert cols == ["id", "price", "name"]

    def test_constraint_skipped(self):
        cols = parse_table_columns(COMPLEX_TBL)
        # CONSTRAINT line must NOT appear as a column.
        assert "CONSTRAINT" not in [c.upper() for c in cols]
        assert "pk_complex" not in cols
        assert cols[0] == "id"
        # Quoted identifier should appear unquoted.
        assert "quoted col" in cols
        assert "price" in cols
        assert "note" in cols

    def test_comments_ignored(self):
        # The table has a comment that mentions "fake column" — it
        # must not appear in the parsed columns.
        cols = parse_table_columns(COMPLEX_TBL)
        assert "fake" not in cols

    def test_no_column_block_returns_empty(self):
        ddl = "CREATE TABLE D.X PRIMARY INDEX (id);"
        assert parse_table_columns(ddl) == []

    def test_multi_word_type(self):
        ddl = (
            "CREATE TABLE D.X (\n"
            "     id INTEGER NOT NULL\n"
            "    ,name VARCHAR(100) CHARACTER SET LATIN\n"
            "    ,is_flag BYTEINT\n"
            ") PRIMARY INDEX (id);\n"
        )
        assert parse_table_columns(ddl) == ["id", "name", "is_flag"]


# ===========================================================================
# Section 3 — Locking view emission
# ===========================================================================


class TestLockingViewGeneration:
    """generate_locking_view_ddl + render_column_list + is_locking_view."""

    def test_render_simple_columns(self):
        rendered = render_column_list(["a", "b", "c"])
        assert rendered == ("      a\n    , b\n    , c")

    def test_render_with_qualifier(self):
        rendered = render_column_list(["a", "b"], qualifier="m")
        assert rendered == ("      m.a\n    , m.b")

    def test_render_empty(self):
        assert render_column_list([]) == ""

    def test_locking_view_basic_shape(self):
        spec = TableSpec(
            file_path=Path("dummy.tbl"),
            database_token="{{DOM_DATABASE_T}}",
            module="DOM",
            object_name="Mortgage",
            columns=["Mortgage_Id", "Applicant_Name", "Loan_Amount"],
        )
        ddl = generate_locking_view_ddl(spec)
        # Marker for validator exemption
        assert "-- LOCKING VIEW" in ddl
        # Header column list
        assert "CREATE VIEW {{DOM_DATABASE_V}}.Mortgage" in ddl
        # Both header and SELECT include columns
        assert ddl.count("Mortgage_Id") >= 2
        assert ddl.count("Applicant_Name") >= 2
        # LOCKING ROW FOR ACCESS present
        assert "LOCKING ROW FOR ACCESS" in ddl
        # Reads from _T
        assert "FROM {{DOM_DATABASE_T}}.Mortgage" in ddl
        # Terminates correctly
        assert ddl.rstrip().endswith(";")

    def test_locking_view_is_detected_by_marker(self):
        spec = TableSpec(
            file_path=Path("dummy.tbl"),
            database_token="{{DOM_DATABASE_T}}",
            module="DOM",
            object_name="Mortgage",
            columns=["Mortgage_Id"],
        )
        ddl = generate_locking_view_ddl(spec)
        assert is_locking_view(ddl) is True

    def test_business_view_not_detected_as_locking(self):
        assert is_locking_view(ENRICHED_VIEW) is False


# ===========================================================================
# Section 4 — FROM/JOIN parsing
# ===========================================================================


class TestFromClauseParser:
    """parse_from_clause."""

    def test_simple_from_with_alias(self):
        sql = "SELECT * FROM {{DOM_DATABASE_T}}.Mortgage m"
        refs = parse_from_clause(sql)
        assert len(refs) == 1
        assert refs[0].database_token == "{{DOM_DATABASE_T}}"
        assert refs[0].object_name == "Mortgage"
        assert refs[0].alias == "m"

    def test_from_with_as_alias(self):
        sql = "SELECT * FROM {{DOM_DATABASE_T}}.Mortgage AS m"
        refs = parse_from_clause(sql)
        assert refs[0].alias == "m"

    def test_from_no_alias(self):
        sql = "SELECT * FROM {{DOM_DATABASE_T}}.Mortgage"
        refs = parse_from_clause(sql)
        assert refs[0].alias == ""

    def test_inner_join(self):
        sql = (
            "SELECT * "
            "FROM {{DOM_DATABASE_T}}.Mortgage m "
            "INNER JOIN {{DOM_DATABASE_T}}.Property p "
            "ON m.Property_Id = p.Property_Id"
        )
        refs = parse_from_clause(sql)
        assert len(refs) == 2
        assert refs[1].alias == "p"

    def test_left_outer_join(self):
        sql = (
            "SELECT * FROM {{DOM_DATABASE_T}}.Mortgage m "
            "LEFT OUTER JOIN {{DOM_DATABASE_T}}.Property p "
            "ON m.Property_Id = p.Property_Id"
        )
        refs = parse_from_clause(sql)
        assert len(refs) == 2
        assert refs[1].object_name == "Property"

    def test_alias_stoplist_rejects_keyword(self):
        # Without alias but with ON immediately following — the regex
        # could over-capture ON as an alias. Stoplist must reject.
        sql = (
            "FROM {{DOM_DATABASE_T}}.Mortgage "
            "INNER JOIN {{DOM_DATABASE_T}}.Property "
            "ON Mortgage.Property_Id = Property.Property_Id"
        )
        refs = parse_from_clause(sql)
        # Both refs should have empty aliases (no real ones in source).
        for ref in refs:
            assert ref.alias.upper() not in {"ON", "WHERE", "INNER", "JOIN"}


# ===========================================================================
# Section 5 — SELECT * expansion
# ===========================================================================


@pytest.fixture
def two_table_index():
    """Column index covering Mortgage and Property tables."""
    return {
        ("{{DOM_DATABASE_T}}", "Mortgage"): [
            "Mortgage_Id",
            "Applicant_Name",
            "Property_Id",
            "Loan_Amount",
            "Application_Date",
        ],
        ("{{DOM_DATABASE_V}}", "Mortgage"): [
            "Mortgage_Id",
            "Applicant_Name",
            "Property_Id",
            "Loan_Amount",
            "Application_Date",
        ],
        ("{{DOM_DATABASE_T}}", "Property"): [
            "Property_Id",
            "Property_Address",
            "Property_Value",
            "Application_Date",
        ],
        ("{{DOM_DATABASE_V}}", "Property"): [
            "Property_Id",
            "Property_Address",
            "Property_Value",
            "Application_Date",
        ],
    }


class TestTeradataAliasBlocklist:
    """Regression tests — Teradata syntax shortcuts must be rejected as aliases.

    Teradata treats certain two- and three-letter identifiers as command
    shortcuts (e.g. CT = CREATE TABLE, CS = CASESPECIFIC).  Using them as
    table aliases causes Error 3706 ("expected something between ',' and the
    'CT' keyword").  The ``_ALIAS_STOPLIST`` must include all known shortcuts
    so that ``_alias_or_blank`` strips them and ``parse_from_clause`` records
    the source without an alias, preventing the generator from producing
    invalid SQL.
    """

    _SHORTCUTS = {
        "CT": "CREATE TABLE",
        "BT": "BEGIN TRANSACTION",
        "ET": "END TRANSACTION",
        "DEL": "DELETE",
        "INS": "INSERT",
        "SEL": "SELECT",
        "UPD": "UPDATE",
        "CV": "CREATE VIEW",
        "CM": "CREATE MACRO",
        "CP": "CREATE PROCEDURE",
        "CS": "CASESPECIFIC",
    }

    def test_all_shortcuts_in_stoplist(self):
        """Every known Teradata syntax shortcut must be in _ALIAS_STOPLIST."""
        from td_release_packager.view_layer_generator import _ALIAS_STOPLIST

        missing = [s for s in self._SHORTCUTS if s not in _ALIAS_STOPLIST]
        assert not missing, (
            f"Teradata syntax shortcuts missing from _ALIAS_STOPLIST: {missing}. "
            f"These would be accepted as table aliases, causing Teradata Error 3706."
        )

    def test_ct_alias_treated_as_no_alias(self):
        """CT used as an alias must be stripped — it is CREATE TABLE in Teradata."""
        sql = (
            "SELECT ct.topic "
            "FROM {{DB_V}}.Call_Topic_Current ct "
            "LEFT JOIN {{DB_V}}.Call_Current c ON c.call_id = ct.call_id"
        )
        result = parse_from_clause(sql)
        # CT should have been stripped; the source is recorded with an empty alias.
        ct_entries = [
            spec for spec in result if spec.object_name == "Call_Topic_Current"
        ]
        assert ct_entries, "Call_Topic_Current not found in parsed FROM clause"
        assert ct_entries[0].alias == "", (
            f"Expected alias '' for Call_Topic_Current (CT is a reserved shortcut), "
            f"got {ct_entries[0].alias!r}"
        )

    def test_cs_alias_treated_as_no_alias(self):
        """CS used as an alias must be stripped — it is CASESPECIFIC in Teradata."""
        sql = "SELECT cs.conversation_summary FROM {{DB_V}}.Call_Summary_Current cs"
        result = parse_from_clause(sql)
        cs_entries = [
            spec for spec in result if spec.object_name == "Call_Summary_Current"
        ]
        assert cs_entries, "Call_Summary_Current not found in parsed FROM clause"
        assert cs_entries[0].alias == "", (
            f"Expected alias '' for Call_Summary_Current (CS is a reserved shortcut), "
            f"got {cs_entries[0].alias!r}"
        )


class TestSelectStarExpansion:
    """expand_select_star."""

    def test_alias_star_single_table(self, two_table_index):
        warnings = []
        result = expand_select_star(
            CURRENT_VIEW, two_table_index, warnings, "Mortgage_Current.viw"
        )
        assert "*" not in result.split("FROM")[0]
        assert "m.Mortgage_Id" in result
        assert "m.Applicant_Name" in result
        assert "m.Loan_Amount" in result
        # No collision warnings expected for single-table expansion.
        assert not warnings

    def test_unqualified_star_two_tables_with_collision(self, two_table_index):
        warnings = []
        result = expand_select_star(
            ENRICHED_VIEW, two_table_index, warnings, "Mortgage_Enhanced.viw"
        )
        # SELECT * gone
        assert "SELECT *" not in result
        # First-table columns appear bare
        assert "m.Mortgage_Id" in result
        # Application_Date collides — second occurrence must be aliased.
        assert "Application_Date AS p_Application_Date" in result
        # Property_Id collides too — second occurrence aliased.
        assert "Property_Id AS p_Property_Id" in result
        # Two collisions => two warnings
        collision_warnings = [w for w in warnings if "collision" in w]
        assert len(collision_warnings) == 2

    def test_unresolvable_source_leaves_view_unchanged(self):
        warnings = []
        # Empty index — nothing resolves.
        result = expand_select_star(
            ENRICHED_VIEW, {}, warnings, "Mortgage_Enhanced.viw"
        )
        # Original SELECT * preserved
        assert "SELECT *" in result
        assert any("could not resolve" in w for w in warnings)

    def test_no_select_star_unchanged(self, two_table_index):
        sql = (
            "CREATE VIEW {{DOM_DATABASE_V}}.X AS\n"
            "SELECT m.Mortgage_Id FROM {{DOM_DATABASE_T}}.Mortgage m\n;"
        )
        warnings = []
        assert expand_select_star(sql, two_table_index, warnings, "x.viw") == sql

    def test_alias_star_unknown_alias(self, two_table_index):
        sql = (
            "CREATE VIEW {{DOM_DATABASE_V}}.Y AS\n"
            "SELECT z.* FROM {{DOM_DATABASE_T}}.Mortgage m;\n"
        )
        warnings = []
        result = expand_select_star(sql, two_table_index, warnings, "y.viw")
        assert "z.*" in result  # unchanged
        assert any("z" in w for w in warnings)


# ===========================================================================
# Section 6 — _T to _V reference rewrite
# ===========================================================================


class TestTablesToViewsRewrite:
    """rewrite_tables_to_views."""

    def test_simple_rewrite(self):
        sql = "FROM {{DOM_DATABASE_T}}.Mortgage m"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        assert new == "FROM {{DOM_DATABASE_V}}.Mortgage m"
        assert count == 1

    def test_non_database_token_rewrite(self):
        sql = "FROM {{DB_DOMAIN_T}}.Call_H c"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        assert new == "FROM {{DB_DOMAIN_V}}.Call_H c"
        assert count == 1

    def test_multiple_modules(self):
        sql = "FROM {{DOM_DATABASE_T}}.A INNER JOIN {{SEM_DATABASE_T}}.B ON A.id = B.id"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        assert "{{DOM_DATABASE_V}}" in new
        assert "{{SEM_DATABASE_V}}" in new
        assert "{{DOM_DATABASE_T}}" not in new
        assert "{{SEM_DATABASE_T}}" not in new
        assert count == 2

    def test_v_token_untouched(self):
        sql = "FROM {{DOM_DATABASE_V}}.A"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        assert new == sql
        assert count == 0

    def test_comment_reference_not_rewritten(self):
        sql = "-- previously was {{DOM_DATABASE_T}}.X\nFROM {{DOM_DATABASE_T}}.Y"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        assert "-- previously was {{DOM_DATABASE_T}}.X" in new
        assert "FROM {{DOM_DATABASE_V}}.Y" in new
        assert count == 1

    def test_block_comment_reference_not_rewritten(self):
        sql = "/* legacy: {{DOM_DATABASE_T}}.A */\nFROM {{DOM_DATABASE_T}}.B"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        assert "{{DOM_DATABASE_T}}.A" in new  # still in comment
        assert "FROM {{DOM_DATABASE_V}}.B" in new
        assert count == 1

    def test_string_literal_reference_not_rewritten(self):
        sql = "SELECT 'source: {{DOM_DATABASE_T}}.X' AS src FROM {{DOM_DATABASE_T}}.Y"
        new, count = rewrite_tables_to_views(sql, [], "x.viw")
        # The literal must keep its original token.
        assert "'source: {{DOM_DATABASE_T}}.X'" in new
        # The real FROM clause is rewritten.
        assert "FROM {{DOM_DATABASE_V}}.Y" in new
        assert count == 1


# ===========================================================================
# Section 7 — Database & grants emission
# ===========================================================================


class TestDatabaseAndGrants:
    """generate_database_ddl and generate_grant_ddl."""

    def test_database_ddl_basic(self):
        ddl = generate_database_ddl("{{DOM_DATABASE_V}}")
        assert "CREATE DATABASE {{DOM_DATABASE_V}}" in ddl
        assert "PERMANENT" in ddl
        assert "SPOOL" in ddl
        assert ddl.rstrip().endswith(";")

    def test_grant_ddl_single_grantor(self):
        ddl = generate_grant_ddl(
            "{{DOM_DATABASE_V}}",
            ["{{DOM_DATABASE_T}}"],
        )
        assert (
            "GRANT SELECT ON {{DOM_DATABASE_T}} TO {{DOM_DATABASE_V}} "
            "WITH GRANT OPTION;" in ddl
        )

    def test_grant_ddl_multiple_grantors_sorted(self):
        ddl = generate_grant_ddl(
            "{{SEM_DATABASE_V}}",
            ["{{SEM_DATABASE_T}}", "{{DOM_DATABASE_V}}"],
        )
        # Output must be deterministic — alphabetical grantor order.
        lines = [ln for ln in ddl.splitlines() if ln.startswith("GRANT")]
        assert lines == [
            "GRANT SELECT ON {{DOM_DATABASE_V}} TO {{SEM_DATABASE_V}} "
            "WITH GRANT OPTION;",
            "GRANT SELECT ON {{SEM_DATABASE_T}} TO {{SEM_DATABASE_V}} "
            "WITH GRANT OPTION;",
        ]

    def test_grant_ddl_dedupes(self):
        ddl = generate_grant_ddl(
            "{{X_DATABASE_V}}",
            ["{{Y_DATABASE_T}}", "{{Y_DATABASE_T}}"],
        )
        grant_lines = [ln for ln in ddl.splitlines() if ln.startswith("GRANT")]
        assert len(grant_lines) == 1


# ===========================================================================
# Section 8 — Idempotent file write
# ===========================================================================


class TestWriteIfDifferent:
    """write_if_different."""

    def test_writes_new_file(self, tmp_path):
        target = tmp_path / "sub" / "new.txt"
        result = write_if_different(target, "hello\n", dry_run=False)
        assert result is True
        assert target.read_text() == "hello\n"

    def test_skips_when_unchanged(self, tmp_path):
        target = tmp_path / "x.txt"
        target.write_text("same\n")
        result = write_if_different(target, "same\n", dry_run=False)
        assert result is False

    def test_overwrites_when_different(self, tmp_path):
        target = tmp_path / "x.txt"
        target.write_text("old\n")
        result = write_if_different(target, "new\n", dry_run=False)
        assert result is True
        assert target.read_text() == "new\n"

    def test_dry_run_does_not_write(self, tmp_path):
        target = tmp_path / "x.txt"
        result = write_if_different(target, "hello\n", dry_run=True)
        assert result is True
        assert not target.exists()


# ===========================================================================
# Section 9 — End-to-end
# ===========================================================================


def _make_ships_project(root: Path) -> None:
    """Create a minimal SHIPS project structure under *root*."""
    for sub in [
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/pre-requisites/databases",
        "payload/database/DCL/inter_db",
    ]:
        (root / sub).mkdir(parents=True, exist_ok=True)


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def ships_project(tmp_path):
    """A SHIPS project with two modules, two tables, and three views."""
    root = tmp_path / "Project"
    _make_ships_project(root)

    # Domain tables
    _write(
        root,
        "payload/database/DDL/tables/{{DOM_DATABASE_T}}.Mortgage.tbl",
        MORTGAGE_TBL,
    )
    _write(
        root,
        "payload/database/DDL/tables/{{DOM_DATABASE_T}}.Property.tbl",
        PROPERTY_TBL,
    )

    # Domain business views — both need rewriting.
    _write(
        root,
        "payload/database/DDL/views/{{DOM_DATABASE_V}}.Mortgage_Current.viw",
        CURRENT_VIEW,
    )
    _write(
        root,
        "payload/database/DDL/views/{{DOM_DATABASE_V}}.Mortgage_Enhanced.viw",
        ENRICHED_VIEW,
    )

    # SEM cross-module view (no _T refs to rewrite — already on _V).
    _write(
        root,
        "payload/database/DDL/views/{{SEM_DATABASE_V}}.SLA_Check.viw",
        CROSS_MODULE_VIEW,
    )

    return root


class TestEndToEnd:
    """run() exercising discovery, generation, rewrite and grants."""

    def test_full_pipeline_produces_expected_outputs(self, ships_project):
        result = run(ships_project, requested_modules=None, dry_run=False)

        # No fatal errors
        assert result.errors == []

        # 2 locking views (Mortgage, Property) created
        assert result.locking_views_written == 2

        # 2 business views rewritten in DOM
        # Note: SEM business view has no SELECT * and no _T refs, so
        # it should be rewritten=False (unchanged after expand+rewrite).
        assert result.business_views_rewritten >= 2

        # Locking views exist on disk and are detected as such.
        for name in ("Mortgage", "Property"):
            lv = (
                ships_project
                / "payload/database/DDL/views"
                / f"{{{{DOM_DATABASE_V}}}}.{name}.viw"
            )
            assert lv.exists()
            assert is_locking_view(lv.read_text())

        # Business view rewrites: SELECT * gone, references on _V.
        enhanced = (
            ships_project
            / "payload/database/DDL/views"
            / "{{DOM_DATABASE_V}}.Mortgage_Enhanced.viw"
        ).read_text()
        assert "SELECT *" not in enhanced
        assert "{{DOM_DATABASE_T}}" not in enhanced
        assert "{{DOM_DATABASE_V}}.Mortgage" in enhanced

    def test_full_pipeline_accepts_generic_t_v_tokens(self, tmp_path):
        root = tmp_path / "Project"
        _make_ships_project(root)
        _write(
            root,
            "payload/database/DDL/tables/{{DB_DOMAIN_T}}.Agent_H.tbl",
            MORTGAGE_TBL.replace("{{DOM_DATABASE_T}}", "{{DB_DOMAIN_T}}").replace(
                "Mortgage", "Agent_H"
            ),
        )
        _write(
            root,
            "payload/database/DDL/views/{{DB_DOMAIN_V}}.Agent_Current.viw",
            (
                "CREATE VIEW {{DB_DOMAIN_V}}.Agent_Current AS\n"
                "SELECT *\n"
                "FROM {{DB_DOMAIN_T}}.Agent_H a\n;"
            ),
        )

        result = run(root, requested_modules=None, dry_run=False)

        assert result.errors == []
        locking_view = (
            root / "payload/database/DDL/views" / "{{DB_DOMAIN_V}}.Agent_H.viw"
        )
        assert locking_view.exists()
        assert "CREATE VIEW {{DB_DOMAIN_V}}.Agent_H" in locking_view.read_text()

        business_view = (
            root / "payload/database/DDL/views" / "{{DB_DOMAIN_V}}.Agent_Current.viw"
        ).read_text()
        assert "SELECT *" not in business_view
        assert "{{DB_DOMAIN_T}}" not in business_view
        assert "FROM {{DB_DOMAIN_V}}.Agent_H a" in business_view

        grant = (
            root / "payload/database/DCL/inter_db" / "{{DB_DOMAIN_V}}.grt"
        ).read_text()
        assert "GRANT SELECT ON {{DB_DOMAIN_T}} TO {{DB_DOMAIN_V}}" in grant

    def test_database_files_generated(self, ships_project):
        run(ships_project, requested_modules=None, dry_run=False)
        db_path = (
            ships_project
            / "payload/database/pre-requisites/databases"
            / "{{DOM_DATABASE_V}}.db"
        )
        assert db_path.exists()
        assert "CREATE DATABASE {{DOM_DATABASE_V}}" in db_path.read_text()

    def test_same_module_grant_generated(self, ships_project):
        run(ships_project, requested_modules=None, dry_run=False)
        grant_path = (
            ships_project / "payload/database/DCL/inter_db" / "{{DOM_DATABASE_V}}.grt"
        )
        assert grant_path.exists()
        text = grant_path.read_text()
        # Same-module DOM_T -> DOM_V grant
        assert "GRANT SELECT ON {{DOM_DATABASE_T}} TO {{DOM_DATABASE_V}}" in text

    def test_cross_module_grant_generated(self, ships_project):
        run(ships_project, requested_modules=None, dry_run=False)
        # SEM_V reads from DOM_V — needs a cross-module grant.
        grant_path = (
            ships_project / "payload/database/DCL/inter_db" / "{{SEM_DATABASE_V}}.grt"
        )
        assert grant_path.exists()
        text = grant_path.read_text()
        assert "GRANT SELECT ON {{DOM_DATABASE_V}} TO {{SEM_DATABASE_V}}" in text

    def test_rerun_is_idempotent(self, ships_project):
        # First run writes everything.
        first = run(ships_project, requested_modules=None, dry_run=False)
        # Second run should find everything unchanged.
        second = run(ships_project, requested_modules=None, dry_run=False)
        assert second.locking_views_written == 0
        assert second.locking_views_unchanged == first.locking_views_written
        assert second.business_views_rewritten == 0
        assert second.databases_written == 0
        assert second.grants_written == 0

    def test_module_filter(self, ships_project):
        # Process only DOM — SEM grants must NOT be touched.
        run(
            ships_project,
            requested_modules={"DOM"},
            dry_run=False,
        )
        sem_grant = (
            ships_project / "payload/database/DCL/inter_db" / "{{SEM_DATABASE_V}}.grt"
        )
        assert not sem_grant.exists()

    def test_dry_run_writes_nothing(self, ships_project):
        result = run(ships_project, requested_modules=None, dry_run=True)
        # Counters report what WOULD have been written.
        assert result.locking_views_written >= 1
        # But files do not exist.
        lv = (
            ships_project
            / "payload/database/DDL/views"
            / "{{DOM_DATABASE_V}}.Mortgage.viw"
        )
        assert not lv.exists()


# ---------------------------------------------------------------
# CLI prog plumbing — guards the misleading-help-text fix
# ---------------------------------------------------------------


class TestArgParserProg:
    """The shim and `python -m` entry both override argparse's prog."""

    def test_default_prog_when_unspecified(self):
        # No prog → argparse picks one from sys.argv[0] / module name.
        # Just guard that something non-empty is chosen.
        parser = _build_arg_parser()
        assert parser.prog

    def test_custom_prog_propagates_to_usage_text(self):
        parser = _build_arg_parser(
            prog="python -m td_release_packager.view_layer_generator"
        )
        assert parser.prog == "python -m td_release_packager.view_layer_generator"
        assert (
            "python -m td_release_packager.view_layer_generator"
            in parser.format_usage()
        )

    def test_shim_prog_propagates_to_usage_text(self):
        parser = _build_arg_parser(prog="python tools/generate_view_layer.py")
        assert "python tools/generate_view_layer.py" in parser.format_usage()


# ===========================================================================
# Regression — DEFAULT keyword must not appear as a column name
# ===========================================================================


class TestDefaultKeywordNotEmittedAsColumn:
    """Regression tests for Teradata Error 3707.

    When a column has a DEFAULT clause whose literal value contains a
    comma (e.g. ``DEFAULT TIMESTAMP '9999-12-31 23:59:59.999999+00:00'``),
    the ``_split_top_level`` splitter treats the continuation line as a
    separate entry.  ``parse_table_columns`` must recognise that ``DEFAULT``
    is a SQL keyword continuation clause, not a column name, and skip it.

    Without the fix, the generated view contained a bare ``DEFAULT`` entry
    in both its column list and SELECT clause, which Teradata 20 rejects
    with Error 3707: "expected something like '(' between the 'DEFAULT'
    keyword and ','".
    """

    _TEMPORAL_TABLE = (
        "CREATE MULTISET TABLE {{DB_DOMAIN_T}}.Agent_H (\n"
        "    agent_id        INTEGER NOT NULL\n"
        "   ,agent_name      VARCHAR(200) NOT NULL\n"
        "   ,valid_from_dts  TIMESTAMP(6) WITH TIME ZONE NOT NULL\n"
        "   ,valid_to_dts    TIMESTAMP(6) WITH TIME ZONE NOT NULL\n"
        "                    DEFAULT TIMESTAMP '9999-12-31 23:59:59.999999+00:00'\n"
        "   ,is_current      BYTEINT NOT NULL DEFAULT 1\n"
        "   ,is_deleted      BYTEINT NOT NULL DEFAULT 0\n"
        ") PRIMARY INDEX (agent_id);\n"
    )

    def test_default_keyword_not_in_columns(self):
        """parse_table_columns must not return 'DEFAULT' as a column name."""
        cols = parse_table_columns(self._TEMPORAL_TABLE)
        assert "DEFAULT" not in [c.upper() for c in cols], (
            "parse_table_columns emitted 'DEFAULT' as a column name — "
            "the DEFAULT continuation clause was mistakenly split into a "
            "separate column entry."
        )

    def test_correct_columns_returned(self):
        """All real column names are returned and nothing spurious is added."""
        cols = parse_table_columns(self._TEMPORAL_TABLE)
        assert cols == [
            "agent_id",
            "agent_name",
            "valid_from_dts",
            "valid_to_dts",
            "is_current",
            "is_deleted",
        ]

    def test_generated_view_has_no_default_column(self, tmp_path):
        """The generated locking view DDL must not contain a bare DEFAULT column."""
        from pathlib import Path as _Path

        tbl_path = tmp_path / "{{DB_DOMAIN_T}}.Agent_H.tbl"
        tbl_path.write_text(self._TEMPORAL_TABLE, encoding="utf-8")

        cols = parse_table_columns(self._TEMPORAL_TABLE)
        spec = TableSpec(
            file_path=tbl_path,
            database_token="{{DB_DOMAIN_T}}",
            module="DB_DOMAIN",
            object_name="Agent_H",
            columns=cols,
        )
        ddl = generate_locking_view_ddl(spec)

        # Neither the column-list section nor the SELECT list should
        # contain a bare DEFAULT identifier.
        import re as _re

        # Match ', DEFAULT' or '  DEFAULT' as a standalone column reference
        # (not part of a longer name like 'default_value').
        bare_default = _re.search(
            r"(?:,\s*|\bSELECT\s+)\bDEFAULT\b(?!\s*\w)",
            ddl,
            _re.IGNORECASE,
        )
        assert bare_default is None, (
            f"Generated view DDL contains a bare DEFAULT column reference:\n{ddl}"
        )

    def test_inline_default_integer_not_mistaken(self):
        """Inline DEFAULT without a comma (e.g. DEFAULT 1) on the same line
        as the column definition must not cause the column to be skipped.
        """
        cols = parse_table_columns(self._TEMPORAL_TABLE)
        # is_current and is_deleted both have inline DEFAULT on the same line
        assert "is_current" in cols
        assert "is_deleted" in cols


class TestSkippedRun:
    """``run`` reports ``skipped`` instead of erroring on non-applicable payloads.

    View-layer generation is an opt-in convention. When the payload
    uses literal database names (no ``{{*_T}}.<Name>.tbl`` shapes),
    the generator can't operate — but that is not a pipeline failure,
    it is a payload that simply doesn't need this stage.
    """

    def test_literal_named_payload_is_skipped_not_errored(self, tmp_path):
        """A .tbl whose name does not start with a paired token reports
        skipped, not error. Matches the BionicCC_17 case where the
        operator deployed with literal database names."""
        root = tmp_path / "Project"
        _make_ships_project(root)
        # Literal database name, NOT a {{TOKEN}} prefix.
        _write(
            root,
            "payload/database/DDL/tables/CallCentre_DOM_STD_T.Agent_H.tbl",
            "CREATE MULTISET TABLE CallCentre_DOM_STD_T.Agent_H "
            "(id INTEGER NOT NULL) PRIMARY INDEX (id);\n",
        )

        result = run(root, requested_modules=None, dry_run=False)

        assert result.errors == []
        assert result.skipped is True
        assert result.skip_reason is not None
        # Mentions the convention the user needs to follow
        assert "{{DB_DOMAIN_T}}" in result.skip_reason

    def test_empty_tables_dir_is_skipped_not_errored(self, tmp_path):
        """A project that ships no tables at all is also a no-op for the
        generator. Some packages are pure DML/grants/views — they don't
        need the view-layer pipeline either."""
        root = tmp_path / "Project"
        _make_ships_project(root)

        result = run(root, requested_modules=None, dry_run=False)

        assert result.errors == []
        assert result.skipped is True

    def test_paired_token_payload_is_not_skipped(self, ships_project):
        """A real paired-token payload still runs through to completion."""
        result = run(ships_project, requested_modules=None, dry_run=False)
        assert result.errors == []
        assert result.skipped is False
        assert result.skip_reason is None
