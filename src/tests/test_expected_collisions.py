"""
Tests for ``td_release_packager.expected_collisions``.

These tests pin the safety-critical behaviour of the operator allow-list:

* benign collisions can be downgraded;
* a REAL clobber CANNOT be suppressed — the safety invariant emits a
  ``RejectedEntry`` instead and leaves the original ERROR in place;
* malformed YAML raises a structured parse error.
"""

from __future__ import annotations

import pytest

from td_release_packager.expected_collisions import (
    Allowlist,
    AllowlistEntry,
    AllowlistParseError,
    apply_allowlist,
    apply_to_report,
    load_allowlist,
    parse_allowlist,
)
from td_release_packager.token_audit import (
    Clobber,
    CollisionClass,
    CollisionGroup,
    ResolutionReport,
)


# ---------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------


class TestParser:
    def test_minimal_valid(self):
        doc = """
        expected:
          - tokens: [PERM_SPACE, SPOOL_SPACE]
            reason: "Interchangeable scalars."
        """
        a = parse_allowlist(doc)
        assert len(a.entries) == 1
        e = a.entries[0]
        # Tokens stored sorted for structural stability.
        assert e.tokens == ("PERM_SPACE", "SPOOL_SPACE")
        assert e.reason == "Interchangeable scalars."

    def test_empty_file_yields_empty_allowlist(self):
        a = parse_allowlist("")
        assert a.entries == ()

    def test_null_expected_key(self):
        a = parse_allowlist("expected: null\n")
        assert a.entries == ()

    def test_multiple_entries(self):
        doc = """
        expected:
          - tokens: [A, B]
            reason: "first"
          - tokens: [C, D]
            reason: "second"
        """
        a = parse_allowlist(doc)
        assert len(a.entries) == 2
        assert a.entries[0].reason == "first"
        assert a.entries[1].reason == "second"

    def test_top_level_not_a_mapping(self):
        with pytest.raises(AllowlistParseError, match="must be a mapping"):
            parse_allowlist("- just a list")

    def test_expected_must_be_list(self):
        with pytest.raises(AllowlistParseError, match="must be a list"):
            parse_allowlist("expected: not a list\n")

    def test_entry_must_be_mapping(self):
        with pytest.raises(AllowlistParseError, match="entry #1 is not a mapping"):
            parse_allowlist("expected:\n  - just a string\n")

    def test_missing_tokens(self):
        doc = "expected:\n  - reason: nope\n"
        with pytest.raises(
            AllowlistParseError, match="'tokens' must be a non-empty list"
        ):
            parse_allowlist(doc)

    def test_single_token_rejected(self):
        # A collision is by definition >=2 tokens.
        doc = "expected:\n  - tokens: [ONLY]\n    reason: weird\n"
        with pytest.raises(AllowlistParseError, match="at least two tokens"):
            parse_allowlist(doc)

    def test_non_string_token(self):
        doc = "expected:\n  - tokens: [A, 1]\n    reason: x\n"
        with pytest.raises(
            AllowlistParseError, match="every token must be a non-empty string"
        ):
            parse_allowlist(doc)

    def test_duplicate_entries_rejected(self):
        doc = """
        expected:
          - tokens: [A, B]
            reason: one
          - tokens: [B, A]
            reason: dup
        """
        with pytest.raises(AllowlistParseError, match="duplicated"):
            parse_allowlist(doc)

    def test_invalid_yaml(self):
        with pytest.raises(AllowlistParseError, match="invalid YAML"):
            parse_allowlist("expected: [\n")


# ---------------------------------------------------------------------
# Disk loader
# ---------------------------------------------------------------------


class TestLoader:
    def test_missing_file_returns_empty(self, tmp_path):
        a = load_allowlist(str(tmp_path / "does_not_exist.yaml"))
        assert a.entries == ()
        assert a.is_empty

    def test_loads_disk_file(self, tmp_path):
        path = tmp_path / "expected_collisions.yaml"
        path.write_text(
            "expected:\n  - tokens: [A, B]\n    reason: ok\n",
            encoding="utf-8",
        )
        a = load_allowlist(str(path))
        assert len(a.entries) == 1
        assert a.source_path == str(path)


# ---------------------------------------------------------------------
# Entry semantics
# ---------------------------------------------------------------------


class TestEntryCovers:
    def test_exact_match(self):
        e = AllowlistEntry(tokens=("A", "B"), reason="")
        assert e.covers(("A", "B"))

    def test_subset_match(self):
        # A collision involving a strict subset of the entry's token set is
        # still covered — the entry says "any combination of these tokens
        # colliding is fine".
        e = AllowlistEntry(tokens=("A", "B", "C"), reason="")
        assert e.covers(("A", "B"))
        assert e.covers(("B", "C"))

    def test_disjoint_does_not_match(self):
        e = AllowlistEntry(tokens=("A", "B"), reason="")
        assert not e.covers(("A", "C"))

    def test_superset_does_not_match(self):
        # An entry covering [A, B] should NOT silently swallow a collision
        # including unrelated token Z.
        e = AllowlistEntry(tokens=("A", "B"), reason="")
        assert not e.covers(("A", "B", "Z"))


# ---------------------------------------------------------------------
# Application — the safety invariant
# ---------------------------------------------------------------------


class TestApplyAllowlist:
    def test_scalar_pair_downgraded(self):
        groups = (
            CollisionGroup(
                value="1e9",
                tokens=("PERM_SPACE", "SPOOL_SPACE"),
                classification=CollisionClass.SCALAR,
            ),
        )
        a = parse_allowlist(
            "expected:\n  - tokens: [PERM_SPACE, SPOOL_SPACE]\n    reason: x\n"
        )
        updated, rejected = apply_allowlist(groups, a)
        assert rejected == ()
        assert updated[0].classification is CollisionClass.ALLOWLISTED

    def test_env_label_pair_downgraded(self):
        groups = (
            CollisionGroup(
                value="AGNOSTIC",
                tokens=("ENV_PREFIX", "SHIPS_ENV"),
                classification=CollisionClass.ENV_LABEL,
            ),
        )
        a = parse_allowlist(
            "expected:\n  - tokens: [ENV_PREFIX, SHIPS_ENV]\n    reason: agnostic\n"
        )
        updated, rejected = apply_allowlist(groups, a)
        assert updated[0].classification is CollisionClass.ALLOWLISTED
        assert rejected == ()

    def test_alias_pair_downgraded(self):
        groups = (
            CollisionGroup(
                value="ProdDb",
                tokens=("PRIMARY", "ALIAS"),
                classification=CollisionClass.ALIAS,
            ),
        )
        a = parse_allowlist(
            "expected:\n  - tokens: [PRIMARY, ALIAS]\n    reason: legacy\n"
        )
        updated, rejected = apply_allowlist(groups, a)
        assert updated[0].classification is CollisionClass.ALLOWLISTED
        assert rejected == ()

    def test_real_clobber_cannot_be_suppressed(self):
        """Safety invariant: a REAL clobber must remain ERROR even when the
        operator names its tokens in the allow-list. A RejectedEntry is
        emitted and the original ERROR is preserved."""
        groups = (
            CollisionGroup(
                value="ProdDb",
                tokens=("TBL_A", "TBL_B"),
                classification=CollisionClass.REAL,
            ),
        )
        a = parse_allowlist(
            "expected:\n  - tokens: [TBL_A, TBL_B]\n    reason: trying to mask\n"
        )
        updated, rejected = apply_allowlist(groups, a)

        # Original REAL class preserved.
        assert updated[0].classification is CollisionClass.REAL
        # The rejection is surfaced separately.
        assert len(rejected) == 1
        r = rejected[0]
        assert r.entry.tokens == ("TBL_A", "TBL_B")
        assert r.real_collision_value == "ProdDb"
        assert r.real_collision_tokens == ("TBL_A", "TBL_B")
        assert r.entry.reason == "trying to mask"

    def test_unmatched_collision_unchanged(self):
        groups = (
            CollisionGroup(
                value="x",
                tokens=("A", "B"),
                classification=CollisionClass.SCALAR,
            ),
        )
        a = parse_allowlist("expected:\n  - tokens: [C, D]\n    reason: irrelevant\n")
        updated, rejected = apply_allowlist(groups, a)
        assert updated[0].classification is CollisionClass.SCALAR
        assert rejected == ()

    def test_empty_allowlist_passes_through(self):
        groups = (
            CollisionGroup(
                value="x",
                tokens=("A", "B"),
                classification=CollisionClass.SCALAR,
            ),
        )
        updated, rejected = apply_allowlist(groups, Allowlist(entries=()))
        assert updated == groups
        assert rejected == ()


class TestApplyToReport:
    def test_report_is_not_mutated(self):
        original = ResolutionReport(
            env="DEV",
            clobbers=(),
            collisions=(
                CollisionGroup(
                    value="v",
                    tokens=("A", "B"),
                    classification=CollisionClass.SCALAR,
                ),
            ),
        )
        a = parse_allowlist("expected:\n  - tokens: [A, B]\n    reason: ok\n")
        updated, rejected = apply_to_report(original, a)

        # New report carries the downgrade.
        assert updated.collisions[0].classification is CollisionClass.ALLOWLISTED
        # Original is unchanged.
        assert original.collisions[0].classification is CollisionClass.SCALAR

    def test_report_preserves_clobbers_and_metadata(self):
        original = ResolutionReport(
            env="DEV",
            clobbers=(
                Clobber(
                    physical_name="db.x",
                    sources=("s1", "s2"),
                    tokens=("A", "B"),
                ),
            ),
            collisions=(
                CollisionGroup(
                    value="v",
                    tokens=("A", "B"),
                    classification=CollisionClass.REAL,
                ),
            ),
            defined_count=5,
            undefined=("X",),
            unused=("Y",),
            empty=("Z",),
        )
        a = parse_allowlist("expected:\n  - tokens: [A, B]\n    reason: oops\n")
        updated, rejected = apply_to_report(original, a)

        # Clobber list preserved.
        assert updated.clobbers == original.clobbers
        # Defined/undefined/unused/empty preserved.
        assert updated.defined_count == 5
        assert updated.undefined == ("X",)
        # Rejection surfaced.
        assert len(rejected) == 1
        # Collision still REAL.
        assert updated.collisions[0].classification is CollisionClass.REAL
