"""
test_token_suffix_regex_family.py — Regression tests for issue #454.

Every regex in the codebase that matches a ``{{TOKEN}}`` shape inside a
database/object identifier needs the trailing ``\\w*`` after ``}}`` so
``{{DB_PREFIX}}_DOM_STD_T`` parses as one identifier — not as the bare
``{{DB_PREFIX}}`` token with a stray suffix that the surrounding regex
either truncates or refuses to match.

#448 fixed one site (the analyser). This module locks in the same
property across every other site that handles tokenised identifiers,
so a future regex edit can't silently re-introduce the truncation in
one corner of the codebase while leaving the others correct.
"""

from __future__ import annotations

import re


TOKEN_NAME = "{{DB_PREFIX}}_DOM_STD_T"
TOKEN_QNAME = "{{DB_PREFIX}}_DOM_STD_T.customer"
TOKEN_DB_FROM_PARENT = "{{DB_PREFIX}}_Root"


class TestEnvironmentPrereqsCreateParentRe:
    """``_CREATE_PARENT_RE`` must capture both child and parent in full."""

    def test_tokenised_child_and_parent_capture_in_full(self):
        from td_release_packager.environment_prereqs import _CREATE_PARENT_RE

        sql = (
            "CREATE DATABASE {{DB_PREFIX}}_Domain FROM {{DB_PREFIX}}_Root AS PERM = 0;"
        )
        m = _CREATE_PARENT_RE.search(sql)
        assert m is not None, "tokenised CREATE DATABASE … FROM … must match"
        assert m.group(2) == "{{DB_PREFIX}}_Domain"
        assert m.group(3) == "{{DB_PREFIX}}_Root"


class TestRootParentCreatePrereqHeaderRe:
    """``CREATE_PREREQ_HEADER_RE`` covers the whole tokenised header."""

    def test_tokenised_database_header_captured_in_full(self):
        from td_release_packager.root_parent import CREATE_PREREQ_HEADER_RE

        m = CREATE_PREREQ_HEADER_RE.search(
            "CREATE DATABASE {{DB_PREFIX}}_Domain AS PERM = 0;"
        )
        assert m is not None
        assert m.group(0).endswith("{{DB_PREFIX}}_Domain")


class TestValidateViewMacroDefNameRe:
    """View/macro header regex must capture the full tokenised dbpart."""

    def test_tokenised_view_header_captures_full_dbpart(self):
        from td_release_packager.validate import _VIEW_MACRO_DEF_NAME_RE

        m = _VIEW_MACRO_DEF_NAME_RE.search(
            "REPLACE VIEW {{DB_PREFIX}}_DOM_ACL_V.customer_current AS"
        )
        assert m is not None, "tokenised VIEW header must match"
        assert m.group("dbpart") == "{{DB_PREFIX}}_DOM_ACL_V"
        assert m.group("objpart") == "customer_current"


class TestValidateDbQualifiedRefRe:
    """``_DB_QUALIFIED_REF_RE`` finds tokenised db.obj refs in DDL bodies."""

    def test_tokenised_reference_in_from_clause_captured_in_full(self):
        from td_release_packager.validate import _DB_QUALIFIED_REF_RE

        m = _DB_QUALIFIED_REF_RE.search("FROM {{DB_PREFIX}}_DOM_STD_T.customer c")
        assert m is not None
        assert m.group(0) == "{{DB_PREFIX}}_DOM_STD_T.customer"


class TestValidateVclIdentFrag:
    """``_VCL_IDENT_FRAG`` is composed into other regexes — probe directly."""

    def test_fragment_composed_into_qualified_match(self):
        from td_release_packager.validate import _VCL_IDENT_FRAG

        regex = re.compile(rf"{_VCL_IDENT_FRAG}\s*\.\s*{_VCL_IDENT_FRAG}")
        m = regex.search("FROM {{DB_PREFIX}}_DOM_STD_T.customer")
        assert m is not None
        assert m.group(0) == "{{DB_PREFIX}}_DOM_STD_T.customer"


class TestValidatePrereqIdentFrag:
    """``_PREREQ_IDENT_FRAG`` matches the full tokenised database name."""

    def test_fragment_captures_full_tokenised_name(self):
        from td_release_packager.validate import _PREREQ_IDENT_FRAG

        regex = re.compile(
            rf"CREATE\s+DATABASE\s+({_PREREQ_IDENT_FRAG})",
            re.IGNORECASE,
        )
        m = regex.search("CREATE DATABASE {{DB_PREFIX}}_Domain AS PERM = 0;")
        assert m is not None
        assert m.group(1) == "{{DB_PREFIX}}_Domain"


class TestValidateGrantIdent:
    """Tokenised GRANT targets and grantees must match in full."""

    def test_tokenised_grant_target_and_grantee_capture(self):
        from td_release_packager.validate import _GRANT_IDENT

        regex = re.compile(
            rf"ON\s+({_GRANT_IDENT}\.{_GRANT_IDENT})\s+TO\s+({_GRANT_IDENT})",
            re.IGNORECASE,
        )
        m = regex.search(
            "GRANT SELECT ON {{DB_PREFIX}}_DOM_STD_V.foo TO {{ROLE}}_Read;"
        )
        assert m is not None, "tokenised GRANT must match"
        assert m.group(1) == "{{DB_PREFIX}}_DOM_STD_V.foo"
        assert m.group(2) == "{{ROLE}}_Read"


class TestValidatePlacementQualifiedRefPattern:
    """View-placement validator finds tokenised db.obj refs."""

    def test_tokenised_reference_captured_in_full(self):
        from td_release_packager.validate_placement import _QUALIFIED_REF_PATTERN

        m = _QUALIFIED_REF_PATTERN.search(
            "SELECT * FROM {{DB_PREFIX}}_DOM_STD_T.customer"
        )
        assert m is not None
        assert m.group(0) == "{{DB_PREFIX}}_DOM_STD_T.customer"


class TestTdReleasePackagerBuilderQname:
    """Builder's qualified-name regex keeps tokenised names whole."""

    def test_tokenised_qname_match(self):
        from td_release_packager.builder import _QUALIFIED_NAME_RE

        regex = re.compile(_QUALIFIED_NAME_RE, re.IGNORECASE)
        m = regex.search(TOKEN_QNAME)
        assert m is not None
        assert m.group(0) == TOKEN_QNAME


class TestTokenRolesQname:
    """Role-discovery regex anchors must keep tokenised names whole."""

    def test_tokenised_qname_match(self):
        from td_release_packager.token_roles import _QNAME_RX

        regex = re.compile(_QNAME_RX, re.IGNORECASE)
        m = regex.search(TOKEN_QNAME)
        assert m is not None
        assert m.group(0) == TOKEN_QNAME


class TestDeployerBuilderQname:
    """Deployer (embedded) builder keeps tokenised names whole."""

    def test_tokenised_qname_match(self):
        from database_package_deployer.builder import _QUALIFIED_NAME_RE

        regex = re.compile(_QUALIFIED_NAME_RE, re.IGNORECASE)
        m = regex.search(TOKEN_QNAME)
        assert m is not None
        assert m.group(0) == TOKEN_QNAME


class TestDeployerStatementParserNp:
    """Statement-parser name fragment keeps tokenised names whole."""

    def test_tokenised_qname_match(self):
        from database_package_deployer.statement_parser import _NP

        regex = re.compile(rf"{_NP}\s*\.\s*{_NP}", re.IGNORECASE)
        m = regex.search(TOKEN_QNAME)
        assert m is not None
        assert m.group(0) == TOKEN_QNAME


class TestDeployerPrivilegeCheckDatabaseStatement:
    """``DATABASE {{TOKEN}}_suffix;`` must match the privilege-check regex."""

    def test_tokenised_database_statement_captures_in_full(self):
        from database_package_deployer.privilege_check import (
            _DATABASE_STATEMENT_RE,
        )

        m = _DATABASE_STATEMENT_RE.search("DATABASE {{DB_PREFIX}}_Domain;")
        assert m is not None, "tokenised DATABASE statement must match"
        assert m.group(1) == "{{DB_PREFIX}}_Domain"


class TestMigrateViewReferencesQualifiedRef:
    """View-reference migration tool keeps tokenised db.obj refs whole."""

    def test_tokenised_reference_captured_in_full(self):
        import sys
        from pathlib import Path

        tools_dir = Path(__file__).resolve().parents[2] / "tools"
        added = False
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
            added = True
        try:
            import migrate_view_references as mvr
        finally:
            if added:
                sys.path.remove(str(tools_dir))

        m = mvr._QUALIFIED_REF_PATTERN.search(
            "SELECT * FROM {{DB_PREFIX}}_DOM_STD_T.customer"
        )
        assert m is not None
        assert m.group(0) == "{{DB_PREFIX}}_DOM_STD_T.customer"
