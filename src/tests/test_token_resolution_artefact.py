"""
Tests for ``td_release_packager.token_resolution_artefact``.

The artefact is the bridge between the audit and everything downstream — the
sixth trust signal, the package report, and any agent inspecting a built
package. These tests pin its JSON shape, since that shape is the contract.
"""

from __future__ import annotations

import json

import pytest

from td_release_packager.expected_collisions import (
    AllowlistEntry,
    RejectedEntry,
    parse_allowlist,
)
from td_release_packager.token_audit import (
    Clobber,
    CollisionClass,
    CollisionGroup,
    ResolutionReport,
)
from td_release_packager.token_resolution_artefact import (
    ARTEFACT_FILENAME,
    ARTEFACT_REF,
    ARTEFACT_SCHEMA_VERSION,
    EnvAuditResult,
    audit_envs_with_allowlist,
    compute_artefact,
    load_artefact,
    write_artefact,
)
from td_release_packager.token_roles import (
    Role,
    RoleAssignment,
    TokenPositions,
)


# ---------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------


def _role(r: Role) -> RoleAssignment:
    return RoleAssignment(role=r, mixed_use=False, positions=TokenPositions())


class TestArtefactShape:
    def test_minimal_document(self):
        doc = compute_artefact([], generated_at="2026-06-19T00:00:00+00:00")
        assert doc["schema_version"] == ARTEFACT_SCHEMA_VERSION
        assert doc["generated_at"] == "2026-06-19T00:00:00+00:00"
        assert doc["environments"] == []
        assert doc["generated_by"].endswith("token_resolution_artefact")

    def test_serialises_clobbers_and_collisions(self):
        report = ResolutionReport(
            env="DEV",
            clobbers=(
                Clobber(
                    physical_name="db.x",
                    sources=("a.viw", "b.viw"),
                    tokens=("A", "B"),
                ),
            ),
            collisions=(
                CollisionGroup(
                    value="1e9",
                    tokens=("PERM_SPACE", "SPOOL_SPACE"),
                    classification=CollisionClass.SCALAR,
                ),
            ),
            roles={"A": _role(Role.IDENTITY), "PERM_SPACE": _role(Role.SCALAR)},
            defined_count=4,
            undefined=("MISSING",),
            unused=("GHOST",),
            empty=("BLANK",),
        )
        doc = compute_artefact(
            [EnvAuditResult(report=report)],
            generated_at="2026-06-19T00:00:00+00:00",
        )
        env = doc["environments"][0]
        assert env["env"] == "DEV"
        assert env["defined"] == 4
        assert env["undefined"] == ["MISSING"]
        assert env["unused"] == ["GHOST"]
        assert env["empty"] == ["BLANK"]
        assert env["roles"] == {"A": "IDENTITY", "PERM_SPACE": "SCALAR"}
        assert env["clobbers"] == [
            {
                "physical_name": "db.x",
                "sources": ["a.viw", "b.viw"],
                "tokens": ["A", "B"],
            }
        ]
        assert env["collisions"] == [
            {
                "value": "1e9",
                "tokens": ["PERM_SPACE", "SPOOL_SPACE"],
                "class": "scalar",
            }
        ]
        assert env["rejected_allowlist"] == []

    def test_serialises_rejected_allowlist(self):
        entry = AllowlistEntry(tokens=("A", "B"), reason="masked attempt")
        rejected = (
            RejectedEntry(
                entry=entry,
                real_collision_value="db.x",
                real_collision_tokens=("A", "B"),
            ),
        )
        report = ResolutionReport(env="DEV")
        doc = compute_artefact(
            [EnvAuditResult(report=report, rejected=rejected)],
            generated_at="2026-06-19T00:00:00+00:00",
        )
        env = doc["environments"][0]
        assert env["rejected_allowlist"] == [
            {
                "tokens": ["A", "B"],
                "value": "db.x",
                "reason": "masked attempt",
            }
        ]


# ---------------------------------------------------------------------
# Disk round-trip
# ---------------------------------------------------------------------


class TestPersistence:
    def test_write_and_load_roundtrip(self, tmp_path):
        report = ResolutionReport(env="DEV", defined_count=2)
        doc = compute_artefact(
            [EnvAuditResult(report=report)],
            generated_at="2026-06-19T00:00:00+00:00",
        )
        path = write_artefact(str(tmp_path), doc)
        # The conventional filename and location.
        assert path.endswith(ARTEFACT_FILENAME)
        assert (tmp_path / "context" / ARTEFACT_FILENAME).exists()

        # Round-trip preserves the document.
        reloaded = load_artefact(str(tmp_path))
        assert reloaded == doc

    def test_load_returns_none_when_absent(self, tmp_path):
        assert load_artefact(str(tmp_path)) is None

    def test_artefact_ref_matches_convention(self):
        assert ARTEFACT_REF == "context/" + ARTEFACT_FILENAME


# ---------------------------------------------------------------------
# audit_envs_with_allowlist convenience
# ---------------------------------------------------------------------


class TestAuditEnvsConvenience:
    def test_runs_per_env(self):
        envs = {
            "DEV": {"DB_PREFIX": "DEV_X"},
            "PRD": {"DB_PREFIX": "PRD_X"},
        }
        results = audit_envs_with_allowlist(
            env_configs=envs,
            resolved_envs=envs,
            payload_files=[
                (
                    "{{DB_PREFIX}}.MyView.viw",
                    "CREATE VIEW {{DB_PREFIX}}.MyView AS SELECT 1;",
                ),
            ],
            referenced_tokens={"DB_PREFIX"},
        )
        assert [r.report.env for r in results] == ["DEV", "PRD"]
        # Each env should classify DB_PREFIX as IDENTITY.
        for r in results:
            assert r.report.roles["DB_PREFIX"].role is Role.IDENTITY

    def test_allowlist_applied_to_each_env(self):
        envs = {
            "DEV": {"PERM_SPACE": "1e9", "SPOOL_SPACE": "1e9"},
        }
        a = parse_allowlist(
            "expected:\n  - tokens: [PERM_SPACE, SPOOL_SPACE]\n    reason: scalar\n"
        )
        results = audit_envs_with_allowlist(
            env_configs=envs,
            resolved_envs=envs,
            payload_files=[
                (
                    "x.x.tbl",
                    "CREATE TABLE x.x (Id INT) AS PERM={{PERM_SPACE}}, "
                    "SPOOL={{SPOOL_SPACE}};",
                )
            ],
            allowlist=a,
            referenced_tokens={"PERM_SPACE", "SPOOL_SPACE"},
        )
        assert len(results) == 1
        # The scalar pair is downgraded.
        classes = {c.classification for c in results[0].report.collisions}
        assert CollisionClass.ALLOWLISTED in classes
        assert CollisionClass.SCALAR not in classes
