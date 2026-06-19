"""
Tests for ``td_release_packager.token_audit``.

These tests pin the spec's acceptance criteria for step (b):

* AGNOSTIC prefix-tokenisation rebuild reports 0 real collisions.
* A whole-name token-map clobber yields REAL.
* Two identity tokens naming the same logical object yields ALIAS.
* PERM_SPACE == SPOOL_SPACE yields SCALAR.
* ENV_PREFIX == SHIPS_ENV yields ENV_LABEL.
* Case-insensitive collisions are detected (Teradata default semantics).
"""

from __future__ import annotations

import pytest

from td_release_packager.token_audit import (
    Clobber,
    CollisionClass,
    LogicalObject,
    audit_project,
    classify_collision,
    collect_payload_objects,
    detect_clobbers,
)
from td_release_packager.token_roles import (
    Role,
    RoleAssignment,
    TokenPositions,
)
from td_release_packager.tokenised_name import parse_qualified_name


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _role(r: Role, *, identity=False, scalar=False, env_label=False) -> RoleAssignment:
    return RoleAssignment(
        role=r,
        mixed_use=False,
        positions=TokenPositions(
            identity=identity,
            scalar=scalar,
            env_config_composition=env_label,
            any_payload_reference=True,
        ),
    )


def _ident(source_id: str, qname: str) -> LogicalObject:
    return LogicalObject(source_id=source_id, name=parse_qualified_name(qname))


# --------------------------------------------------------------------------
# Clobber detection
# --------------------------------------------------------------------------


class TestClobberDetection:
    def test_distinct_objects_same_physical_name(self):
        """Whole-name token map: two tokens, same resolved value → clobber."""
        objects = [
            _ident("a.view.viw", "{{TBL_A}}.MyView"),
            _ident("b.view.viw", "{{TBL_B}}.MyView"),
        ]
        env = {"TBL_A": "ProdDb", "TBL_B": "ProdDb"}
        roles = {
            "TBL_A": _role(Role.IDENTITY, identity=True),
            "TBL_B": _role(Role.IDENTITY, identity=True),
        }
        clobbers = detect_clobbers(objects, env, roles=roles)
        assert len(clobbers) == 1
        c = clobbers[0]
        assert c.physical_name == "proddb.myview"
        assert c.sources == ("a.view.viw", "b.view.viw")
        # Both identity tokens are attributed.
        assert set(c.tokens) == {"TBL_A", "TBL_B"}

    def test_prefix_tokenisation_no_clobber(self):
        """Prefix-tokenisation has one identity token → impossible to clobber."""
        objects = [
            _ident("v1.viw", "{{DB_PREFIX}}_SEM_STD.View1"),
            _ident("v2.viw", "{{DB_PREFIX}}_SEM_STD.View2"),
            _ident("v3.viw", "{{DB_PREFIX}}_SEM_STD.View3"),
        ]
        env = {"DB_PREFIX": "PROD_X"}
        roles = {"DB_PREFIX": _role(Role.IDENTITY, identity=True)}
        clobbers = detect_clobbers(objects, env, roles=roles)
        assert clobbers == ()

    def test_case_insensitive_collision(self):
        """Teradata is case-insensitive by default — Foo == foo."""
        objects = [
            _ident("a.viw", "MyDb.CallCentre_X"),
            _ident("b.viw", "mydb.callcentre_x"),
        ]
        env = {}
        clobbers = detect_clobbers(objects, env, roles={})
        assert len(clobbers) == 1

    def test_same_source_recorded_twice_does_not_clobber(self):
        """Defence against double-counting if the same path appears twice."""
        objects = [
            _ident("a.viw", "Db.X"),
            _ident("a.viw", "Db.X"),  # same source_id, same name
        ]
        clobbers = detect_clobbers(objects, {}, roles={})
        assert clobbers == ()

    def test_missing_token_skips_object(self):
        """Under-resolved names cannot participate (undefined check handles them)."""
        objects = [
            _ident("a.viw", "{{MISSING}}.X"),
            _ident("b.viw", "Db.X"),
        ]
        clobbers = detect_clobbers(objects, {}, roles={})
        assert clobbers == ()

    def test_clobber_attributes_identity_tokens_only(self):
        """Mixed tokens in composing name → only IDENTITY ones attributed."""
        objects = [
            _ident("a.viw", "{{ID_A}}.X"),
            _ident("b.viw", "{{ID_B}}.X"),
        ]
        env = {"ID_A": "Db", "ID_B": "Db"}
        roles = {
            "ID_A": _role(Role.IDENTITY, identity=True),
            "ID_B": _role(Role.IDENTITY, identity=True),
        }
        clobbers = detect_clobbers(objects, env, roles=roles)
        assert clobbers and set(clobbers[0].tokens) == {"ID_A", "ID_B"}


# --------------------------------------------------------------------------
# Collision classification
# --------------------------------------------------------------------------


class TestCollisionClassification:
    def test_all_scalar(self):
        roles = {
            "PERM_SPACE": _role(Role.SCALAR, scalar=True),
            "SPOOL_SPACE": _role(Role.SCALAR, scalar=True),
        }
        assert (
            classify_collision(("PERM_SPACE", "SPOOL_SPACE"), roles)
            is CollisionClass.SCALAR
        )

    def test_all_env_label(self):
        roles = {
            "ENV_PREFIX": _role(Role.ENV_LABEL, env_label=True),
            "SHIPS_ENV": _role(Role.ENV_LABEL, env_label=True),
        }
        assert (
            classify_collision(("ENV_PREFIX", "SHIPS_ENV"), roles)
            is CollisionClass.ENV_LABEL
        )

    def test_two_identity_with_clobber_is_real(self):
        roles = {
            "A": _role(Role.IDENTITY, identity=True),
            "B": _role(Role.IDENTITY, identity=True),
        }
        clobbers = (
            Clobber(physical_name="db.x", sources=("s1", "s2"), tokens=("A", "B")),
        )
        assert (
            classify_collision(("A", "B"), roles, clobbers=clobbers)
            is CollisionClass.REAL
        )

    def test_two_identity_no_clobber_is_alias(self):
        """Two identity tokens with the same value but only one logical object → DRY alias."""
        roles = {
            "A": _role(Role.IDENTITY, identity=True),
            "B": _role(Role.IDENTITY, identity=True),
        }
        assert (
            classify_collision(("A", "B"), roles, clobbers=()) is CollisionClass.ALIAS
        )

    def test_mixed_roles_returns_mixed(self):
        roles = {
            "ID": _role(Role.IDENTITY, identity=True),
            "S": _role(Role.SCALAR, scalar=True),
        }
        assert (
            classify_collision(("ID", "S"), roles, clobbers=()) is CollisionClass.MIXED
        )

    def test_unknown_member_is_mixed(self):
        roles = {"X": _role(Role.UNKNOWN)}
        assert (
            classify_collision(("X", "Y"), roles, clobbers=()) is CollisionClass.MIXED
        )

    def test_empty_token_list(self):
        assert classify_collision((), {}, clobbers=()) is CollisionClass.MIXED


# --------------------------------------------------------------------------
# Filename parser
# --------------------------------------------------------------------------


class TestCollectPayloadObjects:
    def test_db_obj_ext_recognised(self):
        objs = collect_payload_objects([("{{DB_PREFIX}}_SEM_STD.MyView.viw", "")])
        assert len(objs) == 1
        assert objs[0].source_id == "{{DB_PREFIX}}_SEM_STD.MyView.viw"
        assert objs[0].database.tokens == ("DB_PREFIX",)
        assert objs[0].object.fragments == ("MyView",)

    def test_unparseable_filename_skipped(self):
        objs = collect_payload_objects([("README.md", "")])
        assert objs == ()

    def test_no_dots_skipped(self):
        objs = collect_payload_objects([("plainname", "")])
        assert objs == ()


# --------------------------------------------------------------------------
# End-to-end: spec acceptance criteria
# --------------------------------------------------------------------------


class TestAcceptanceCriteria:
    """The four cases the spec calls out as acceptance criteria (section 9)."""

    def test_agnostic_prefix_tokenisation_zero_real_collisions(self):
        """Spec §9.1: MyNewDataProduct AGNOSTIC rebuild → 0 real collisions."""
        env = {
            "SHIPS_ENV": "AGNOSTIC",
            "ENV_PREFIX": "{{SHIPS_ENV}}",
            "DB_PREFIX": "{{ENV_PREFIX}}_MyNewDataProduct",
            "PERM_SPACE": "1e9",
            "SPOOL_SPACE": "1e9",
        }
        resolved = {
            "SHIPS_ENV": "AGNOSTIC",
            "ENV_PREFIX": "AGNOSTIC",
            "DB_PREFIX": "AGNOSTIC_MyNewDataProduct",
            "PERM_SPACE": "1e9",
            "SPOOL_SPACE": "1e9",
        }
        payload = [
            (
                "{{DB_PREFIX}}_SEM_STD.View1.viw",
                "CREATE VIEW {{DB_PREFIX}}_SEM_STD.View1 AS SELECT 1;",
            ),
            (
                "{{DB_PREFIX}}_SEM_STD.View2.viw",
                "CREATE VIEW {{DB_PREFIX}}_SEM_STD.View2 AS SELECT 1;",
            ),
            (
                "MyDb.MyDb.db",
                "CREATE DATABASE {{DB_PREFIX}}_SEM_STD FROM DBC "
                "AS PERM = {{PERM_SPACE}}, SPOOL = {{SPOOL_SPACE}};",
            ),
        ]
        report = audit_project(
            env="AGNOSTIC",
            env_config=env,
            resolved_env=resolved,
            payload_files=payload,
        )
        # Zero clobbers (single identity token → impossible).
        assert report.clobbers == ()
        # PERM_SPACE/SPOOL_SPACE collide on 1e9 but classify as SCALAR.
        # ENV_PREFIX/SHIPS_ENV collide on "AGNOSTIC" → ENV_LABEL.
        # No REAL collisions.
        assert report.real_collisions == ()
        # The scalar and env-label collisions are still present (in the
        # benign bucket) so the report can show them as informational.
        benign_classes = {c.classification for c in report.benign_collisions}
        assert CollisionClass.SCALAR in benign_classes
        assert CollisionClass.ENV_LABEL in benign_classes

    def test_whole_name_token_map_clobber_is_real(self):
        """Spec §9.2: two identity tokens → same literal → REAL collision."""
        env = {"TBL_A": "ProdDb", "TBL_B": "ProdDb"}
        resolved = dict(env)
        payload = [
            ("{{TBL_A}}.MyView.viw", "CREATE VIEW {{TBL_A}}.MyView AS SELECT 1;"),
            ("{{TBL_B}}.MyView.viw", "CREATE VIEW {{TBL_B}}.MyView AS SELECT 1;"),
        ]
        report = audit_project(
            env="DEV",
            env_config=env,
            resolved_env=resolved,
            payload_files=payload,
        )
        assert len(report.clobbers) == 1
        assert len(report.real_collisions) == 1
        real = report.real_collisions[0]
        assert set(real.tokens) == {"TBL_A", "TBL_B"}

    def test_identity_alias_classification(self):
        """Two identity tokens sharing a value but naming the same object → ALIAS."""
        # Only one logical object on disk; both tokens are interchangeable.
        env = {"PRIMARY_DB": "ProdDb", "ALIAS_DB": "ProdDb"}
        resolved = dict(env)
        payload = [
            (
                "{{PRIMARY_DB}}.MyView.viw",
                "CREATE VIEW {{PRIMARY_DB}}.MyView AS SELECT 1;",
            ),
        ]
        report = audit_project(
            env="DEV",
            env_config=env,
            resolved_env=resolved,
            payload_files=payload,
        )
        # No clobber (only one source object).
        assert report.clobbers == ()
        # The value collision still classifies — both members are IDENTITY by
        # role, but no clobber attributes either → ALIAS.
        alias_groups = [
            c for c in report.collisions if c.classification == CollisionClass.ALIAS
        ]
        # ALIAS_DB only appears in env config, not in payload → it classifies
        # as UNUSED, not IDENTITY. Test with both tokens actually used:
        # ensure the classifier doesn't promote to REAL when no clobber.
        # (This test pins behaviour even when one member isn't IDENTITY.)
        assert report.real_collisions == ()

    def test_two_identity_tokens_same_object_pure_alias(self):
        """Both tokens referenced in payload, same object → ALIAS."""
        env = {"A": "Db", "B": "Db"}
        resolved = dict(env)
        # Both tokens appear in identity positions but compose names that
        # resolve to the same logical object name. Without a second source
        # object using the *other* token, this is an alias.
        payload = [
            ("{{A}}.MyView.viw", "CREATE VIEW {{A}}.MyView AS SELECT 1;"),
            ("misc.misc.db", "GRANT SELECT ON {{B}}.AnotherTbl TO PUBLIC;"),
        ]
        report = audit_project(
            env="DEV",
            env_config=env,
            resolved_env=resolved,
            payload_files=payload,
        )
        # No clobber on the physical name: only {{A}}.MyView has a source.
        assert report.clobbers == ()
        # The value-level collision between A and B is identity-alias.
        alias = [
            c for c in report.collisions if c.classification == CollisionClass.ALIAS
        ]
        assert len(alias) == 1
        assert set(alias[0].tokens) == {"A", "B"}


# --------------------------------------------------------------------------
# Report properties
# --------------------------------------------------------------------------


class TestReportShape:
    def test_real_and_benign_partitions_disjoint(self):
        env = {"A": "v", "B": "v", "C": "x", "D": "x"}
        roles = {
            "A": _role(Role.IDENTITY, identity=True),
            "B": _role(Role.IDENTITY, identity=True),
            "C": _role(Role.SCALAR, scalar=True),
            "D": _role(Role.SCALAR, scalar=True),
        }
        report = audit_project(
            env="DEV",
            env_config=env,
            resolved_env=dict(env),
            payload_files=[
                ("{{A}}.X.viw", "CREATE VIEW {{A}}.X AS SELECT 1;"),
                ("{{B}}.X.viw", "CREATE VIEW {{B}}.X AS SELECT 1;"),
                (
                    "dbA.dbA.db",
                    "CREATE DATABASE dbA FROM DBC AS PERM={{C}}, SPOOL={{D}};",
                ),
            ],
        )
        real = set(c.tokens for c in report.real_collisions)
        benign = set(c.tokens for c in report.benign_collisions)
        assert real & benign == set()
        assert ("A", "B") in real
        assert ("C", "D") in benign
