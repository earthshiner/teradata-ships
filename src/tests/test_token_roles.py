"""
Tests for ``td_release_packager.token_roles``.

The classifier feeds the collision audit: getting the role precedence wrong
turns a real clobber into a benign warning or vice-versa. These tests pin
each axis (identity / scalar / env-label / unused / unknown / mixed) and
the precedence rules between them.
"""

from __future__ import annotations

import pytest

from td_release_packager.token_roles import (
    Role,
    TokenPositions,
    classify,
    classify_token_roles,
    scan_env_config_composition,
    scan_sql_text,
)


# ---------------------------------------------------------------------
# Identity detection — SQL anchors
# ---------------------------------------------------------------------


class TestIdentityAnchors:
    def test_create_database(self):
        pos = scan_sql_text("CREATE DATABASE {{DB}} FROM DBC AS PERMANENT = 1e9;")
        assert pos["DB"].identity

    def test_create_database_token_parent(self):
        pos = scan_sql_text(
            "CREATE DATABASE {{CHILD}} FROM {{PARENT}} AS PERMANENT = 0;"
        )
        assert pos["CHILD"].identity
        assert pos["PARENT"].identity

    def test_create_user(self):
        pos = scan_sql_text("CREATE USER {{U}} FROM DBC AS PERMANENT = 0;")
        assert pos["U"].identity

    def test_create_role(self):
        pos = scan_sql_text("CREATE ROLE {{R}};")
        assert pos["R"].identity

    def test_create_view_qualified(self):
        pos = scan_sql_text("CREATE VIEW {{DB}}.{{V}} AS SELECT * FROM {{DB}}.SomeTbl;")
        # Both qualifier sides count as identity.
        assert pos["DB"].identity
        assert pos["V"].identity

    def test_create_view_prefix_tokenised(self):
        # Common SHIPS pattern.
        pos = scan_sql_text("CREATE VIEW {{PFX}}_SEM_STD.{{PFX}}_View AS SELECT 1;")
        assert pos["PFX"].identity

    def test_create_table_with_attributes(self):
        pos = scan_sql_text(
            "CREATE TABLE {{DB}}.MyTbl, NO FALLBACK (x INT) PRIMARY INDEX (x);"
        )
        assert pos["DB"].identity

    def test_grant_on_target(self):
        pos = scan_sql_text("GRANT SELECT ON {{DB}}.MyTbl TO PUBLIC;")
        assert pos["DB"].identity

    def test_grant_to_grantee(self):
        pos = scan_sql_text("GRANT SELECT ON MyDb.MyTbl TO {{ROLE}};")
        assert pos["ROLE"].identity

    def test_filename_db_segment_convention(self):
        # SHIPS filename pattern: <db>.<object>.<ext>
        pos = scan_sql_text(
            "SELECT 1;",  # body has no tokens at all
            filename="{{DB_PREFIX}}_SEM_STD.MyView.viw",
        )
        assert pos["DB_PREFIX"].identity


# ---------------------------------------------------------------------
# Scalar detection
# ---------------------------------------------------------------------


class TestScalarAnchors:
    def test_perm_assignment(self):
        pos = scan_sql_text("CREATE DATABASE A FROM DBC AS PERM = {{PSPACE}};")
        assert pos["PSPACE"].scalar
        assert not pos["PSPACE"].identity

    def test_spool_assignment(self):
        pos = scan_sql_text("CREATE USER U FROM DBC AS SPOOL = {{SPOOL_SPACE}};")
        assert pos["SPOOL_SPACE"].scalar

    def test_perm_and_spool_distinct_tokens(self):
        pos = scan_sql_text(
            "CREATE DATABASE A FROM DBC AS PERM = {{P}}, SPOOL = {{S}};"
        )
        assert pos["P"].scalar
        assert pos["S"].scalar
        assert not pos["P"].identity
        assert not pos["S"].identity

    def test_password_assignment(self):
        pos = scan_sql_text("CREATE USER U FROM DBC AS PERM = 0, PASSWORD = {{PWD}};")
        assert pos["PWD"].scalar


# ---------------------------------------------------------------------
# Mixed use (identity + scalar = modelling smell)
# ---------------------------------------------------------------------


class TestMixedUse:
    def test_identity_wins_over_scalar(self):
        # Same token used in two positions across two statements.
        sql = (
            "CREATE DATABASE {{T}} FROM DBC AS PERM = 0;\n"
            "CREATE TABLE X.MyTbl, NO FALLBACK (c INT) PRIMARY INDEX (c);\n"
            "GRANT SELECT ON X.MyTbl TO ROLE_X;\n"
            "MODIFY DATABASE A AS PERM = {{T}};\n"
        )
        pos = scan_sql_text(sql)
        assert pos["T"].identity
        assert pos["T"].scalar
        # Classification result confirms precedence.
        roles = classify(
            defined_tokens={"T"},
            payload_positions=pos,
            env_config_composition={},
        )
        assert roles["T"].role is Role.IDENTITY
        assert roles["T"].mixed_use is True


# ---------------------------------------------------------------------
# Env-label composition
# ---------------------------------------------------------------------


class TestEnvLabelDetection:
    def test_token_referenced_only_in_env_rhs(self):
        env = {
            "SHIPS_ENV": "DEV",
            "ENV_PREFIX": "A_D01",
            "DB_PREFIX": "{{ENV_PREFIX}}_{{SHIPS_ENV}}",
        }
        composition = scan_env_config_composition(env)
        assert composition.get("ENV_PREFIX") is True
        assert composition.get("SHIPS_ENV") is True
        # DB_PREFIX is itself defined but is not on any RHS.
        assert composition.get("DB_PREFIX") is None

    def test_env_label_role_assignment(self):
        env = {
            "SHIPS_ENV": "DEV",
            "ENV_PREFIX": "A_D01",
            "DB_PREFIX": "{{ENV_PREFIX}}_{{SHIPS_ENV}}",
        }
        # Payload references DB_PREFIX only — env labels never reach payload.
        payload = {
            "DB_PREFIX": TokenPositions(identity=True, any_payload_reference=True),
        }
        roles = classify(
            defined_tokens=env.keys(),
            payload_positions=payload,
            env_config_composition=scan_env_config_composition(env),
        )
        assert roles["DB_PREFIX"].role is Role.IDENTITY
        assert roles["ENV_PREFIX"].role is Role.ENV_LABEL
        assert roles["SHIPS_ENV"].role is Role.ENV_LABEL


# ---------------------------------------------------------------------
# Unused detection
# ---------------------------------------------------------------------


class TestUnused:
    def test_defined_but_never_referenced(self):
        env = {"GHOST": "abc", "USED": "x"}
        payload = {"USED": TokenPositions(identity=True, any_payload_reference=True)}
        roles = classify(
            defined_tokens=env.keys(),
            payload_positions=payload,
            env_config_composition=scan_env_config_composition(env),
        )
        assert roles["GHOST"].role is Role.UNUSED
        assert roles["USED"].role is Role.IDENTITY


# ---------------------------------------------------------------------
# Unknown / fallthrough
# ---------------------------------------------------------------------


class TestUnknown:
    def test_payload_reference_no_recognised_anchor(self):
        # Token appears in a SQL comment-free body but in no anchor we know.
        # Realistically rare; should land as UNKNOWN for triage rather than
        # silently promoted to anything else.
        roles = classify(
            defined_tokens={"X"},
            payload_positions={
                "X": TokenPositions(any_payload_reference=True),
            },
            env_config_composition={},
        )
        assert roles["X"].role is Role.UNKNOWN


# ---------------------------------------------------------------------
# Spec section 2a — the AGNOSTIC scenario
# ---------------------------------------------------------------------


class TestAgnosticScenario:
    """The motivating example from the spec.

    AGNOSTIC env config has SHIPS_ENV=AGNOSTIC, ENV_PREFIX=AGNOSTIC, plus
    interchangeable scalar pair PERM_SPACE=1e9, SPOOL_SPACE=1e9, plus a
    DB_PREFIX identity token. The audit must classify these roles correctly
    so the collision pass downgrades the env-label and scalar collisions and
    reports 0 real collisions.
    """

    def test_classification_matches_spec(self):
        # Mirrors a real AGNOSTIC env config: SHIPS_ENV composes ENV_PREFIX
        # which in turn composes DB_PREFIX. Each level is a label, not an
        # object name.
        env = {
            "SHIPS_ENV": "AGNOSTIC",
            "ENV_PREFIX": "{{SHIPS_ENV}}",
            "DB_PREFIX": "{{ENV_PREFIX}}_MyNewDataProduct",
            "PERM_SPACE": "1e9",
            "SPOOL_SPACE": "1e9",
        }
        payload = (
            (
                "{{DB_PREFIX}}_SEM_STD.MyView.viw",
                "CREATE VIEW {{DB_PREFIX}}_SEM_STD.{{DB_PREFIX}}_View AS SELECT 1;",
            ),
            (
                "MyDb.db",
                "CREATE DATABASE {{DB_PREFIX}}_SEM_STD FROM DBC "
                "AS PERM = {{PERM_SPACE}}, SPOOL = {{SPOOL_SPACE}};",
            ),
        )
        roles = classify_token_roles(
            payload_dir=None,
            env_config=env,
            extra_payloads=payload,
        )
        assert roles["DB_PREFIX"].role is Role.IDENTITY
        assert roles["ENV_PREFIX"].role is Role.ENV_LABEL
        assert roles["SHIPS_ENV"].role is Role.ENV_LABEL
        assert roles["PERM_SPACE"].role is Role.SCALAR
        assert roles["SPOOL_SPACE"].role is Role.SCALAR
        # No mixed-use smells.
        assert not any(r.mixed_use for r in roles.values())


# ---------------------------------------------------------------------
# Higher-level walker
# ---------------------------------------------------------------------


class TestPayloadWalker:
    def test_walks_real_directory(self, tmp_path):
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "{{DB}}.MyView.viw").write_text(
            "CREATE VIEW {{DB}}.MyView AS SELECT 1;", encoding="utf-8"
        )
        (payload / "irrelevant.txt").write_text("ignore me", encoding="utf-8")
        roles = classify_token_roles(
            payload_dir=payload,
            env_config={"DB": "MyDb"},
        )
        assert roles["DB"].role is Role.IDENTITY

    def test_skips_unknown_extensions(self, tmp_path):
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "random.md").write_text(
            "CREATE DATABASE {{NOPE}} FROM DBC AS PERM = 0;", encoding="utf-8"
        )
        roles = classify_token_roles(
            payload_dir=payload,
            env_config={"NOPE": "x"},
        )
        assert roles["NOPE"].role is Role.UNUSED


# ---------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------


def test_role_assignment_to_dict_json_safe():
    """RoleAssignment must round-trip through JSON for ships.token_resolution.json."""
    import json

    roles = classify(
        defined_tokens={"X"},
        payload_positions={"X": TokenPositions(identity=True)},
        env_config_composition={},
    )
    payload = json.dumps({k: v.to_dict() for k, v in roles.items()})
    parsed = json.loads(payload)
    assert parsed["X"]["role"] == "IDENTITY"
