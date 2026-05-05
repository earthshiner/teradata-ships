"""
test_orchestrator_issue_codes.py — Tests for the central issue-code
registry (orchestrator/issue_codes.py).

The registry is the source of truth for what codes ``StageRecorder.
add_issue`` may use. These tests are intentionally tight — they
guard against drift, not behaviour. If a new code is added, this
file is the place that fails first.
"""

from __future__ import annotations


from td_release_packager.orchestrator import issue_codes


class TestRegistryShape:
    def test_registry_is_a_dict_of_str_to_str(self):
        assert isinstance(issue_codes.ISSUE_CODES, dict)
        for code, desc in issue_codes.ISSUE_CODES.items():
            assert isinstance(code, str)
            assert isinstance(desc, str)
            assert code, "empty code in registry"
            assert desc, "empty description for code"

    def test_codes_are_uppercase_snake_case(self):
        """Convention: SCREAMING_SNAKE_CASE."""
        import re

        pattern = re.compile(r"^[A-Z][A-Z0-9_]+$")
        for code in issue_codes.ISSUE_CODES:
            assert pattern.match(code), (
                f"Code {code!r} violates SCREAMING_SNAKE_CASE convention"
            )

    def test_constants_match_registry_entries(self):
        """Every module-level constant must appear in ISSUE_CODES.

        Catches drift where someone defines TOKEN_FOO at module
        level but forgets to add the description to ISSUE_CODES.
        """
        module_constants = {
            name: value
            for name, value in vars(issue_codes).items()
            if name.isupper()
            and not name.startswith("_")
            and isinstance(value, str)
            and name != "ISSUE_CODES"
        }
        for name, value in module_constants.items():
            assert value in issue_codes.ISSUE_CODES, (
                f"Constant {name}={value!r} not present in ISSUE_CODES — "
                f"add a description or remove the constant."
            )


class TestDescribe:
    def test_known_code_returns_registry_description(self):
        desc = issue_codes.describe(issue_codes.TOKEN_UNDEFINED)
        assert "no corresponding entry" in desc.lower() or "undefined" in desc.lower()

    def test_unknown_code_returns_fallback_string(self):
        """describe() never raises — missing description is a doc gap,
        not a runtime fault."""
        result = issue_codes.describe("DOES_NOT_EXIST")
        assert result == "(unregistered code)"


class TestIsRegistered:
    def test_registered_codes_return_true(self):
        assert issue_codes.is_registered(issue_codes.TOKEN_UNDEFINED) is True
        assert issue_codes.is_registered(issue_codes.TOKEN_UNUSED) is True
        assert issue_codes.is_registered(issue_codes.PROPERTIES_NOT_FOUND) is True

    def test_unregistered_code_returns_false(self):
        assert issue_codes.is_registered("MADE_UP_CODE") is False

    def test_empty_string_returns_false(self):
        assert issue_codes.is_registered("") is False


class TestPackageReexports:
    """The registry constants are re-exported from the orchestrator
    package so callers don't need a deep import."""

    def test_constants_importable_from_package(self):
        from td_release_packager.orchestrator import (
            ISSUE_CODES,
            PROPERTIES_NOT_FOUND,
            TOKEN_UNDEFINED,
            TOKEN_UNUSED,
            describe,
            is_registered,
        )

        assert TOKEN_UNDEFINED == "TOKEN_UNDEFINED"
        assert TOKEN_UNUSED == "TOKEN_UNUSED"
        assert PROPERTIES_NOT_FOUND == "PROPERTIES_NOT_FOUND"
        assert TOKEN_UNDEFINED in ISSUE_CODES
        assert callable(describe)
        assert callable(is_registered)
