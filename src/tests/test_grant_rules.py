"""
Unit tests for the two grant-architecture rules in validate.py:

    public_grant_on_tables
        Flag GRANT ... TO PUBLIC on a tables database.

    review_unmapped_grants
        Flag GRANT targets that are neither tables nor views databases
        in the placement map (excluding well-known Teradata system
        databases).

Both rules live as ``_check_*`` functions inside ``validate.py``,
following the same pattern as ``_check_object_placement``. These
tests exercise them directly so failures point straight at the
rule implementation rather than at the dispatch loop.
"""

import os
import tempfile

import pytest

from td_release_packager.object_placement import ObjectPlacement
from td_release_packager.validate import (
    ValidationIssue,
    _check_public_grant_on_tables,
    _check_unmapped_grants,
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


@pytest.fixture
def mapped_placement():
    """Mapped strategy with both literal and tokenised entries —
    matches the user's actual production configuration shape."""
    return ObjectPlacement(
        {
            "strategy": "mapped",
            "locking_views": True,
            "database_map": [
                {
                    "tables_database": "{{OBS_DATABASE_T}}",
                    "views_database": "{{OBS_DATABASE_V}}",
                },
                {
                    "tables_database": "{{DOM_DATABASE_T}}",
                    "views_database": "{{DOM_DATABASE_V}}",
                },
                {"tables_database": "D01_MP_OBS_T", "views_database": "D01_MP_OBS_V"},
                {"tables_database": "D01_MP_DOM_T", "views_database": "D01_MP_DOM_V"},
            ],
        }
    )


@pytest.fixture
def separated_placement():
    """Separated (pattern-based) strategy with _T / _V suffix."""
    return ObjectPlacement(
        {
            "strategy": "separated",
            "locking_views": True,
            "database_pattern_tables": "{BASE}_T",
            "database_pattern_views": "{BASE}_V",
        }
    )


@pytest.fixture
def colocated_placement():
    """Colocated — no architectural distinction. Both rules skip."""
    return ObjectPlacement(
        {
            "strategy": "colocated",
            "locking_views": False,
        }
    )


def _grt(content: str) -> tuple:
    """Write content to a temp .grt file, return (rel_path, content, abs_path)."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".grt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write(content)
    tmp.close()
    abs_path = tmp.name
    rel_path = os.path.basename(abs_path)
    return rel_path, content, abs_path


def _other_ext(content: str, suffix: str) -> tuple:
    """Same as _grt but with a custom extension."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, mode="w", encoding="utf-8"
    )
    tmp.write(content)
    tmp.close()
    abs_path = tmp.name
    rel_path = os.path.basename(abs_path)
    return rel_path, content, abs_path


# ===================================================================
# public_grant_on_tables
# ===================================================================


class TestPublicGrantOnTables_Fires:
    """Positive cases — the rule MUST flag these."""

    def test_users_exact_case_token(self, mapped_placement):
        """The exact case from the bug report: tokenised tables db."""
        rel, content, abs_path = _grt(
            "GRANT SELECT ON {{OBS_DATABASE_T}} TO PUBLIC WITH GRANT OPTION;\n"
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1
        assert issues[0].rule == "public_grant_on_tables"
        assert issues[0].severity == "WARNING"
        assert isinstance(issues[0], ValidationIssue)
        assert "{{OBS_DATABASE_T}}" in issues[0].message

    def test_literal_tables_database(self, mapped_placement):
        rel, content, abs_path = _grt("GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n")
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1

    def test_object_level_grant(self, mapped_placement):
        """Object-level grants on tables databases also violate."""
        rel, content, abs_path = _grt(
            "GRANT SELECT ON D01_MP_OBS_T.SomeTable TO PUBLIC;\n"
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1

    def test_all_privileges(self, mapped_placement):
        rel, content, abs_path = _grt("GRANT ALL ON D01_MP_OBS_T TO PUBLIC;\n")
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1

    def test_multiple_privileges(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT, INSERT, UPDATE ON D01_MP_OBS_T TO PUBLIC;\n"
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1

    def test_public_among_multiple_grantees(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT ON D01_MP_OBS_T TO admin_user, PUBLIC, batch_role;\n"
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1

    def test_multi_line_grant(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT\n    ON D01_MP_OBS_T\n    TO PUBLIC\n    WITH GRANT OPTION;\n"
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1

    def test_multiple_grants_in_file(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n"
            "GRANT INSERT ON D01_MP_DOM_T TO PUBLIC;\n"
            "GRANT SELECT ON D01_MP_OBS_V TO PUBLIC;\n"  # views, OK
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 2

    def test_separated_strategy(self, separated_placement):
        rel, content, abs_path = _grt("GRANT SELECT ON ANALYTICS_T TO PUBLIC;\n")
        issues = _check_public_grant_on_tables(
            rel, content, abs_path, separated_placement
        )
        assert len(issues) == 1

    def test_line_number_reported(self, mapped_placement):
        rel, content, abs_path = _grt(
            "-- Header comment\n"
            "-- Another comment\n"
            "\n"
            "GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n"
        )
        issues = _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1
        assert issues[0].line == 4


class TestPublicGrantOnTables_DoesNotFire:
    """Negative cases — false-positive guards."""

    def test_views_database_grant(self, mapped_placement):
        rel, content, abs_path = _grt("GRANT SELECT ON D01_MP_OBS_V TO PUBLIC;\n")
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_grant_to_specific_role(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT ON D01_MP_OBS_T TO batch_processing_role;\n"
        )
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_revoke_from_public(self, mapped_placement):
        rel, content, abs_path = _grt("REVOKE SELECT ON D01_MP_OBS_T FROM PUBLIC;\n")
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_grantee_name_contains_public(self, mapped_placement):
        """Word boundary — 'PUBLIC_REPORTING_ROLE' is not PUBLIC."""
        rel, content, abs_path = _grt(
            "GRANT SELECT ON D01_MP_OBS_T TO PUBLIC_REPORTING_ROLE;\n"
        )
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_grant_in_line_comment(self, mapped_placement):
        rel, content, abs_path = _grt(
            "-- Example: GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n"
            "GRANT SELECT ON D01_MP_OBS_V TO PUBLIC;\n"
        )
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_grant_in_block_comment(self, mapped_placement):
        rel, content, abs_path = _grt(
            "/* GRANT SELECT ON D01_MP_OBS_T TO PUBLIC; */\n"
            "GRANT SELECT ON D01_MP_OBS_V TO PUBLIC;\n"
        )
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_grant_on_unknown_database(self, mapped_placement):
        """Database not in the map → can't classify → no fire."""
        rel, content, abs_path = _grt("GRANT SELECT ON SomeLegacyDB TO PUBLIC;\n")
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )


class TestPublicGrantOnTables_Configuration:
    def test_non_grt_file_skipped(self, mapped_placement):
        rel, content, abs_path = _other_ext(
            "GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n",
            ".sql",
        )
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, mapped_placement)
            == []
        )

    def test_colocated_skipped(self, colocated_placement):
        rel, content, abs_path = _grt("GRANT SELECT ON SomeDb TO PUBLIC;\n")
        assert (
            _check_public_grant_on_tables(rel, content, abs_path, colocated_placement)
            == []
        )

    def test_no_placement_skipped(self):
        """Without a placement engine, the rule is silently inactive."""
        rel, content, abs_path = _grt("GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n")
        assert _check_public_grant_on_tables(rel, content, abs_path, None) == []


# ===================================================================
# review_unmapped_grants
# ===================================================================


class TestReviewUnmappedGrants_Fires:
    """Positive cases — the rule MUST flag these."""

    def test_unknown_database(self, mapped_placement):
        """Database not in the map → flag."""
        rel, content, abs_path = _grt("GRANT SELECT ON SomeLegacyDB TO admin_user;\n")
        issues = _check_unmapped_grants(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1
        assert issues[0].rule == "review_unmapped_grants"
        assert issues[0].severity == "WARNING"
        assert "SomeLegacyDB" in issues[0].message

    def test_object_level_grant_unmapped(self, mapped_placement):
        """db.object form — only the db is checked."""
        rel, content, abs_path = _grt(
            "GRANT SELECT ON UnknownDb.SomeTable TO admin_user;\n"
        )
        issues = _check_unmapped_grants(rel, content, abs_path, mapped_placement)
        assert len(issues) == 1
        assert "UnknownDb" in issues[0].message

    def test_multiple_unmapped_grants(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT ON DbOne TO user1;\n"
            "GRANT INSERT ON DbTwo TO user2;\n"
            "GRANT SELECT ON D01_MP_OBS_V TO user3;\n"  # mapped, OK
            "GRANT SELECT ON DbThree TO user4;\n"
        )
        issues = _check_unmapped_grants(rel, content, abs_path, mapped_placement)
        assert len(issues) == 3

    def test_separated_strategy_off_pattern(self, separated_placement):
        """In separated mode, a name not matching the pattern fires."""
        rel, content, abs_path = _grt("GRANT SELECT ON SomeRandomDb TO user;\n")
        issues = _check_unmapped_grants(rel, content, abs_path, separated_placement)
        assert len(issues) == 1


class TestReviewUnmappedGrants_DoesNotFire:
    """Negative cases — false-positive guards."""

    def test_known_tables_database(self, mapped_placement):
        rel, content, abs_path = _grt("GRANT SELECT ON D01_MP_OBS_T TO admin_user;\n")
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_known_views_database(self, mapped_placement):
        rel, content, abs_path = _grt("GRANT SELECT ON D01_MP_OBS_V TO PUBLIC;\n")
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_tokenised_tables_database(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT ON {{OBS_DATABASE_T}} TO admin_user;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_tokenised_views_database(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT SELECT ON {{OBS_DATABASE_V}} TO admin_user;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_dbc_system_database(self, mapped_placement):
        """DBC is the canonical Teradata system catalog — auto-allowed."""
        rel, content, abs_path = _grt(
            "GRANT EXECUTE PROCEDURE ON DBC.SysExecSP TO admin_user;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_dbc_lowercase(self, mapped_placement):
        """System db comparison is case-insensitive."""
        rel, content, abs_path = _grt(
            "GRANT EXECUTE PROCEDURE ON dbc.SysExecSP TO admin_user;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_syslib_system_database(self, mapped_placement):
        rel, content, abs_path = _grt(
            "GRANT EXECUTE FUNCTION ON SYSLIB.something TO PUBLIC;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_tdstats_system_database(self, mapped_placement):
        """TDStats is a Teradata system db — used for grant reconciliation."""
        rel, content, abs_path = _grt(
            "GRANT SELECT ON TDStats.AllStatsV TO some_role;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_grant_logon_on_all(self, mapped_placement):
        """GRANT LOGON ON ALL — 'ALL' is not a database, allowlisted."""
        rel, content, abs_path = _grt("GRANT LOGON ON ALL TO some_user;\n")
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_grant_in_comment_ignored(self, mapped_placement):
        rel, content, abs_path = _grt(
            "-- GRANT SELECT ON UnknownDb TO user;\n"
            "GRANT SELECT ON D01_MP_OBS_V TO user;\n"
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []


class TestReviewUnmappedGrants_Configuration:
    def test_non_grt_file_skipped(self, mapped_placement):
        rel, content, abs_path = _other_ext(
            "GRANT SELECT ON UnknownDb TO user;\n",
            ".sql",
        )
        assert _check_unmapped_grants(rel, content, abs_path, mapped_placement) == []

    def test_colocated_skipped(self, colocated_placement):
        """Colocated has no map → rule is meaningless."""
        rel, content, abs_path = _grt("GRANT SELECT ON SomeRandomDb TO user;\n")
        assert _check_unmapped_grants(rel, content, abs_path, colocated_placement) == []

    def test_no_placement_skipped(self):
        rel, content, abs_path = _grt("GRANT SELECT ON UnknownDb TO user;\n")
        assert _check_unmapped_grants(rel, content, abs_path, None) == []


# ===================================================================
# Both rules co-existing — neither produces duplicate issues
# ===================================================================


class TestRuleInteraction:
    """When both rules apply to the same line, they produce
    independent issues — they don't shadow or duplicate each other."""

    def test_public_on_unknown_db_fires_unmapped_only(self, mapped_placement):
        """An unknown db with PUBLIC: only review_unmapped_grants
        fires (public_grant_on_tables needs a known tables db)."""
        rel, content, abs_path = _grt("GRANT SELECT ON UnknownDb TO PUBLIC;\n")
        public_issues = _check_public_grant_on_tables(
            rel, content, abs_path, mapped_placement
        )
        unmapped_issues = _check_unmapped_grants(
            rel, content, abs_path, mapped_placement
        )
        assert public_issues == []
        assert len(unmapped_issues) == 1

    def test_public_on_tables_fires_public_only(self, mapped_placement):
        """A known tables db with PUBLIC: only public_grant_on_tables
        fires (review_unmapped_grants doesn't fire on mapped dbs)."""
        rel, content, abs_path = _grt("GRANT SELECT ON D01_MP_OBS_T TO PUBLIC;\n")
        public_issues = _check_public_grant_on_tables(
            rel, content, abs_path, mapped_placement
        )
        unmapped_issues = _check_unmapped_grants(
            rel, content, abs_path, mapped_placement
        )
        assert len(public_issues) == 1
        assert unmapped_issues == []
