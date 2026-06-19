"""
Tests for the MCP author + validate tools backing expected_collisions.yaml.

The tools follow the same propose -> apply_diff flow as
ships_author_inspect_config / ships_author_token_map: every action returns
an envelope with diff + expected_hash, never writes to disk itself.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def project(tmp_path):
    (tmp_path / "config").mkdir()
    return tmp_path


# ---------------------------------------------------------------------
# ships_validate_expected_collisions
# ---------------------------------------------------------------------


class TestValidate:
    def test_missing_file_is_valid_empty(self, project):
        from ships_mcp import ships_validate_expected_collisions

        r = ships_validate_expected_collisions(str(project))
        assert r["success"] is True
        assert r["exists"] is False
        assert r["valid"] is True
        assert r["errors"] == []

    def test_well_formed_file_is_valid(self, project):
        from ships_mcp import ships_validate_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected:\n  - tokens: [A, B]\n    reason: ok\n",
            encoding="utf-8",
        )
        r = ships_validate_expected_collisions(str(project))
        assert r["success"] is True
        assert r["exists"] is True
        assert r["valid"] is True

    def test_malformed_file_surfaces_error(self, project):
        from ships_mcp import ships_validate_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected: not_a_list\n", encoding="utf-8"
        )
        r = ships_validate_expected_collisions(str(project))
        assert r["success"] is True
        assert r["valid"] is False
        assert r["errors"]


# ---------------------------------------------------------------------
# ships_author_expected_collisions — propose envelope
# ---------------------------------------------------------------------


class TestAuthorCreate:
    def test_create_with_entries(self, project):
        from ships_mcp import ships_author_expected_collisions

        r = ships_author_expected_collisions(
            str(project),
            "create",
            entries=[{"tokens": ["A", "B"], "reason": "ok"}],
        )
        assert r["success"] is True
        assert r["unchanged"] is False
        assert r["validation"]["valid"] is True
        assert "expected:" in r["proposed_content"]
        assert "tokens: [A, B]" in r["proposed_content"]
        # The proposal does NOT write to disk.
        assert not (project / "config" / "expected_collisions.yaml").exists()

    def test_create_empty_is_still_a_proposal(self, project):
        from ships_mcp import ships_author_expected_collisions

        r = ships_author_expected_collisions(str(project), "create")
        assert r["success"] is True
        assert "expected: []" in r["proposed_content"]

    def test_create_fails_when_file_exists(self, project):
        from ships_mcp import ships_author_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected: []\n", encoding="utf-8"
        )
        r = ships_author_expected_collisions(str(project), "create")
        assert r["success"] is False
        assert "already exists" in r["error"]


class TestAuthorAdd:
    def test_add_appends_entry(self, project):
        from ships_mcp import ships_author_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected:\n  - tokens: [A, B]\n    reason: first\n",
            encoding="utf-8",
        )
        r = ships_author_expected_collisions(
            str(project),
            "add",
            entries=[{"tokens": ["C", "D"], "reason": "second"}],
        )
        assert r["success"] is True
        # Both entries appear in proposed_content.
        assert "tokens: [A, B]" in r["proposed_content"]
        assert "tokens: [C, D]" in r["proposed_content"]

    def test_add_rejects_duplicate_token_set(self, project):
        from ships_mcp import ships_author_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected:\n  - tokens: [A, B]\n    reason: first\n",
            encoding="utf-8",
        )
        # Same tokens in different order — still rejected.
        r = ships_author_expected_collisions(
            str(project),
            "add",
            entries=[{"tokens": ["B", "A"], "reason": "dup"}],
        )
        assert r["success"] is False
        assert "already exists" in r["error"]

    def test_add_requires_two_tokens(self, project):
        from ships_mcp import ships_author_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected: []\n", encoding="utf-8"
        )
        r = ships_author_expected_collisions(
            str(project),
            "add",
            entries=[{"tokens": ["LONE"], "reason": "x"}],
        )
        assert r["success"] is False


class TestAuthorRemove:
    def test_remove_existing_entry(self, project):
        from ships_mcp import ships_author_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected:\n"
            "  - tokens: [A, B]\n    reason: keep\n"
            "  - tokens: [C, D]\n    reason: drop\n",
            encoding="utf-8",
        )
        r = ships_author_expected_collisions(
            str(project),
            "remove",
            remove_tokens=[["C", "D"]],
        )
        assert r["success"] is True
        assert "tokens: [A, B]" in r["proposed_content"]
        assert "tokens: [C, D]" not in r["proposed_content"]

    def test_remove_unknown_entry_is_noop(self, project):
        """Removing a token set that wasn't present is not an error.

        The proposal envelope's ``unchanged`` flag tells the caller nothing
        actually changed; this keeps the tool idempotent.
        """
        from ships_mcp import ships_author_expected_collisions

        (project / "config" / "expected_collisions.yaml").write_text(
            "expected:\n  - tokens: [A, B]\n    reason: keep\n",
            encoding="utf-8",
        )
        r = ships_author_expected_collisions(
            str(project),
            "remove",
            remove_tokens=[["X", "Y"]],
        )
        assert r["success"] is True


# ---------------------------------------------------------------------
# Round-trip: every author tool output must validate
# ---------------------------------------------------------------------


def test_author_output_validates(project):
    """Whatever the author tool proposes must pass the validator.

    Pins the invariant that propose + apply leaves the file in a
    state ships_validate_expected_collisions will accept.
    """
    from ships_mcp import (
        ships_author_expected_collisions,
        ships_validate_expected_collisions,
    )

    r = ships_author_expected_collisions(
        str(project),
        "create",
        entries=[
            {"tokens": ["PERM_SPACE", "SPOOL_SPACE"], "reason": "Scalar pair"},
            {"tokens": ["ENV_PREFIX", "SHIPS_ENV"], "reason": "Env labels"},
        ],
    )
    assert r["success"]
    # Write what the proposal said and validate it.
    target = project / "config" / "expected_collisions.yaml"
    target.write_text(r["proposed_content"], encoding="utf-8")
    v = ships_validate_expected_collisions(str(project))
    assert v["valid"] is True, v.get("errors")
