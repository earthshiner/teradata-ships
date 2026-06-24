"""Unit tests for ``td_release_packager.grant_merger``.

Exercises the canonical privilege-merge rule from the handover §7.1:
compatible privilege grants fold; GRANT/REVOKE never merge; role-
grants never merge with privilege-grants; emission order is
deterministic.
"""

from __future__ import annotations

from td_release_packager.grant_merger import (
    PrivilegeGrant,
    RoleGrant,
    emit_statement,
    merge_and_emit,
    merge_statements,
    parse_statement,
)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def test_parses_simple_select_grant() -> None:
    stmt = parse_statement("GRANT SELECT ON DB.T TO U;")
    assert isinstance(stmt, PrivilegeGrant)
    assert stmt.action == "GRANT"
    assert stmt.privileges == ("SELECT",)
    assert stmt.on_object == "DB.T"
    assert stmt.grantee == "U"
    assert stmt.with_grant_option is False


def test_parses_with_grant_option() -> None:
    stmt = parse_statement("GRANT SELECT ON DB.T TO U WITH GRANT OPTION;")
    assert isinstance(stmt, PrivilegeGrant)
    assert stmt.with_grant_option is True


def test_parses_tokenised_on_object() -> None:
    stmt = parse_statement("GRANT SELECT ON {{DOM_STD_T}} TO {{DOM_STD_V}};")
    assert isinstance(stmt, PrivilegeGrant)
    assert stmt.on_object == "{{DOM_STD_T}}"
    assert stmt.grantee == "{{DOM_STD_V}}"


def test_parses_revoke() -> None:
    stmt = parse_statement("REVOKE SELECT ON DB.T FROM U;")
    assert isinstance(stmt, PrivilegeGrant)
    assert stmt.action == "REVOKE"


def test_parses_role_grant_no_on_clause() -> None:
    stmt = parse_statement("GRANT my_role TO some_user;")
    assert isinstance(stmt, RoleGrant)
    assert stmt.role == "my_role"
    assert stmt.grantee == "some_user"


def test_unparseable_returns_none() -> None:
    assert parse_statement("HELP DATABASE foo") is None


# --------------------------------------------------------------------------
# Merge — core rule
# --------------------------------------------------------------------------


def test_merges_select_and_insert_on_same_target() -> None:
    inputs = [
        PrivilegeGrant("GRANT", ("SELECT",), "X", "Y", True),
        PrivilegeGrant("GRANT", ("INSERT",), "X", "Y", True),
    ]
    out = merge_statements(inputs)
    assert len(out) == 1
    merged = out[0]
    assert isinstance(merged, PrivilegeGrant)
    assert merged.privileges == ("INSERT", "SELECT")  # sorted


def test_does_not_merge_when_grantee_differs() -> None:
    inputs = [
        PrivilegeGrant("GRANT", ("SELECT",), "X", "Y", True),
        PrivilegeGrant("GRANT", ("SELECT",), "X", "Z", True),
    ]
    assert len(merge_statements(inputs)) == 2


def test_does_not_merge_when_on_object_differs() -> None:
    inputs = [
        PrivilegeGrant("GRANT", ("SELECT",), "X1", "Y", True),
        PrivilegeGrant("GRANT", ("SELECT",), "X2", "Y", True),
    ]
    assert len(merge_statements(inputs)) == 2


def test_does_not_merge_when_with_grant_option_differs() -> None:
    inputs = [
        PrivilegeGrant("GRANT", ("SELECT",), "X", "Y", True),
        PrivilegeGrant("GRANT", ("INSERT",), "X", "Y", False),
    ]
    assert len(merge_statements(inputs)) == 2


def test_grant_and_revoke_never_merge() -> None:
    inputs = [
        PrivilegeGrant("GRANT", ("SELECT",), "X", "Y", True),
        PrivilegeGrant("REVOKE", ("SELECT",), "X", "Y", True),
    ]
    assert len(merge_statements(inputs)) == 2


def test_role_grant_and_privilege_grant_never_merge() -> None:
    inputs = [
        PrivilegeGrant("GRANT", ("SELECT",), "X", "Y", True),
        RoleGrant("GRANT", "my_role", "Y", False),
    ]
    out = merge_statements(inputs)
    assert len(out) == 2
    assert any(isinstance(s, PrivilegeGrant) for s in out)
    assert any(isinstance(s, RoleGrant) for s in out)


def test_deterministic_order_across_runs() -> None:
    """Same multiset of statements → same output sequence, regardless
    of input order. Locks the determinism property."""
    a = PrivilegeGrant("GRANT", ("SELECT",), "B", "U", True)
    b = PrivilegeGrant("GRANT", ("SELECT",), "A", "U", True)
    c = PrivilegeGrant("REVOKE", ("INSERT",), "A", "U", False)
    out1 = merge_statements([a, b, c])
    out2 = merge_statements([c, a, b])
    out3 = merge_statements([b, c, a])
    assert out1 == out2 == out3


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def test_emit_grant_with_grant_option() -> None:
    stmt = PrivilegeGrant("GRANT", ("INSERT", "SELECT"), "{{X}}", "{{Y}}", True)
    assert emit_statement(stmt) == (
        "GRANT INSERT, SELECT ON {{X}} TO {{Y}} WITH GRANT OPTION;"
    )


def test_emit_revoke_uses_from() -> None:
    stmt = PrivilegeGrant("REVOKE", ("SELECT",), "DB.T", "U", False)
    assert emit_statement(stmt) == "REVOKE SELECT ON DB.T FROM U;"


def test_emit_role_grant() -> None:
    stmt = RoleGrant("GRANT", "my_role", "some_user", False)
    assert emit_statement(stmt) == "GRANT my_role TO some_user;"


# --------------------------------------------------------------------------
# End-to-end — merge_and_emit
# --------------------------------------------------------------------------


def test_e2e_merges_compatible_grants() -> None:
    body = (
        "GRANT SELECT ON {{DOM_STD_T}} TO {{DOM_STD_V}} WITH GRANT OPTION;\n"
        "GRANT INSERT ON {{DOM_STD_T}} TO {{DOM_STD_V}} WITH GRANT OPTION;\n"
    )
    emitted, unparsed = merge_and_emit(body)
    assert unparsed == []
    assert emitted == (
        "GRANT INSERT, SELECT ON {{DOM_STD_T}} TO {{DOM_STD_V}} WITH GRANT OPTION;\n"
    )


def test_e2e_keeps_grant_and_revoke_separate() -> None:
    body = "GRANT SELECT ON X TO Y WITH GRANT OPTION;\nREVOKE SELECT ON X FROM Y;\n"
    emitted, _ = merge_and_emit(body)
    assert "GRANT SELECT ON X TO Y WITH GRANT OPTION;" in emitted
    assert "REVOKE SELECT ON X FROM Y;" in emitted


def test_e2e_passes_unparseable_through_to_warnings() -> None:
    body = "GRANT SELECT ON X TO Y;\nNOT A REAL STATEMENT\n"
    emitted, unparsed = merge_and_emit(body)
    assert "NOT A REAL STATEMENT" in unparsed[0]
    assert "GRANT SELECT ON X TO Y;" in emitted
