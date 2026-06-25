#!/usr/bin/env python3
"""
test_infer_grants_prefix_token.py — Issue #390

Regression tests for grant inference under prefix-tokenisation, where
database identifiers are *compound* — a ``{{TOKEN}}`` followed by a literal
suffix, e.g. ``{{DB_PREFIX}}_SEM_STD_V``. The existing suite only covers
the per-database model (single-token identifiers like ``{{DOM_DATABASE_V}}``),
which is why the user-reported bug slipped through:

    Step 0b reported {{DB_PREFIX_SEM_STD_V}} as an undefined token
    in payload/database/DCL/inter_db/{{DB_PREFIX_SEM_STD_V}}.dcl

i.e. the interior ``}}_`` boundary collapsed and the entire compound name
got wrapped as one token name.

The bug is somewhere in the chain
  parser -> statement-owner extraction -> grant inference -> filename build.
These tests pin each layer so the failing layer is obvious.
"""

import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from td_release_packager.infer_grants import (
    analyse_file,
    consolidate_grants,
    grantee_filename,
)
from td_release_packager.sql_reference_extractor import default_extractor


PREFIX_VIEW_SQL = textwrap.dedent(
    """\
    CREATE VIEW {{DB_PREFIX}}_SEM_STD_V.MyView AS
    SELECT a.*
      FROM {{DB_PREFIX}}_DOM_STD_T.SomeTable a;
    """
)
"""A view in prefix-tokenised form: owner DB is ``{{DB_PREFIX}}_SEM_STD_V``,
SELECT source is ``{{DB_PREFIX}}_DOM_STD_T``."""


# ---------------------------------------------------------------------------
# Layer 1: the statement-owner extractor must preserve the compound form.
# ---------------------------------------------------------------------------
class TestStatementOwnerPrefixToken:
    def test_extract_compound_grantee_view(self):
        owner = default_extractor().extract_statement_owner(PREFIX_VIEW_SQL)
        assert owner is not None, "extractor must find the CREATE VIEW"
        assert owner.database == "{{DB_PREFIX}}_SEM_STD_V", (
            "compound tokenised database must round-trip verbatim — "
            f"got {owner.database!r}"
        )
        assert owner.object_name == "MyView"
        assert owner.object_type == "VIEW"

    def test_extract_compound_grantee_table(self):
        sql = "CREATE TABLE {{DB_PREFIX}}_DOM_STD_T.Booking (id INT);"
        owner = default_extractor().extract_statement_owner(sql)
        assert owner is not None
        assert owner.database == "{{DB_PREFIX}}_DOM_STD_T", f"got {owner.database!r}"

    def test_extract_per_database_grantee_still_works(self):
        """Regression guard: the per-database (single-token) shape must
        continue to work unchanged — this is what existing tests cover."""
        sql = "CREATE VIEW {{DOM_STD_V}}.MyView AS SELECT 1 AS x;"
        owner = default_extractor().extract_statement_owner(sql)
        assert owner is not None
        assert owner.database == "{{DOM_STD_V}}"


# ---------------------------------------------------------------------------
# Layer 2: analyse_file (the inference) must key consolidated grants by the
# compound name, not the collapsed form.
# ---------------------------------------------------------------------------
class TestAnalyseFilePrefixToken:
    def _write(self, sql: str, suffix: str = ".viw") -> Path:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="test_grant_prefix_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(sql)
        return Path(path)

    def test_analyse_view_with_compound_grantee_and_grantor(self):
        path = self._write(PREFIX_VIEW_SQL)
        try:
            # analyse_file returns a single dict per file: {grantee, grants, ...}
            # where grants is {grantor: set_of_privileges}.
            result = analyse_file(path)
            assert result is not None, "must infer at least one grant for the view"
            assert result["grantee"] == "{{DB_PREFIX}}_SEM_STD_V", (
                f"grantee must preserve compound form — got {result['grantee']!r}"
            )
            assert "{{DB_PREFIX}}_DOM_STD_T" in result["grants"], (
                "grantor key must preserve compound form — "
                f"got grants={result['grants']!r}"
            )
        finally:
            path.unlink(missing_ok=True)

    def test_consolidate_keeps_compound_keys(self):
        path = self._write(PREFIX_VIEW_SQL)
        try:
            # consolidate_grants expects a list of analyse_file dicts.
            consolidated = consolidate_grants([analyse_file(path)])
            assert "{{DB_PREFIX}}_SEM_STD_V" in consolidated, (
                "consolidated must key by the compound grantee — "
                f"got keys {list(consolidated.keys())}"
            )
            # Negative guard for the actual bug pattern:
            assert "{{DB_PREFIX_SEM_STD_V}}" not in consolidated, (
                "the malformed collapsed form MUST NOT appear as a key"
            )
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Layer 3: grantee_filename must produce the compound-correct .dcl name.
# (This is trivially f"{grantee}.dcl"; included as a documentation pin.)
# ---------------------------------------------------------------------------
class TestGranteeFilenamePrefixToken:
    def test_filename_for_compound_grantee(self):
        assert (
            grantee_filename("{{DB_PREFIX}}_SEM_STD_V") == "{{DB_PREFIX}}_SEM_STD_V.dcl"
        )

    def test_filename_negative_guard(self):
        """Document the exact bug shape we are guarding against."""
        # If anything upstream ever passes the collapsed form, the file
        # would be named {{DB_PREFIX_SEM_STD_V}}.dcl — the user-visible bug.
        # This test exists to make the bug shape impossible to mis-grep.
        bad = "{{DB_PREFIX_SEM_STD_V}}"
        good = "{{DB_PREFIX}}_SEM_STD_V"
        assert good != bad, "the two shapes must be distinct under string equality"
        assert grantee_filename(good).startswith("{{DB_PREFIX}}_")
