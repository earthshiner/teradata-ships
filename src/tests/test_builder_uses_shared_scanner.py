"""
test_builder_uses_shared_scanner.py — PR2 follow-up.

Locks down the "one scanner" guarantee the handover's §PR2 contract
implies but the original PR2 only delivered for inspect: a payload
that passes inspect's coverage gate must not later fail package's
coverage gate.

Three checks:

  1. Per-env summary symmetry — calling the shared per-env helper
     directly produces the same ``undefined`` set as the cross-env
     entry point's same-env slice. (Regression guard for the refactor
     that extracted the helper.)
  2. Filename tokens are covered — a payload whose only reference to
     a token is in a filename still surfaces the token as undefined,
     matching inspect's behaviour. Before this PR, package's bespoke
     ``validate_tokens`` flow only saw content tokens.
  3. ``unreferenced`` filtering matches ``validate_tokens`` — the
     per-env summary respects the same reserved-property set so the
     legacy report's "WARNINGS — tokens defined but never referenced"
     section stays accurate.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.token_engine import (
    validate_payload_against_env,
    validate_payload_token_coverage,
)


# ---------------------------------------------------------------
# Helpers — shared with the PR2 scanner tests
# ---------------------------------------------------------------


def _scaffold_project(
    root: Path,
    payload_files: dict,
    env_configs: dict,
) -> Path:
    """Build a tiny SHIPS-shaped tree under ``root``."""
    payload = root / "payload" / "database"
    payload.mkdir(parents=True)
    for rel, body in payload_files.items():
        target = payload / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    env_dir = root / "config" / "env"
    env_dir.mkdir(parents=True)
    for name, body in env_configs.items():
        (env_dir / f"{name}.conf").write_text(body, encoding="utf-8")

    return root


# ---------------------------------------------------------------
# Symmetry: per-env helper vs cross-env loop
# ---------------------------------------------------------------


def test_per_env_helper_matches_cross_env_summary(tmp_path: Path) -> None:
    """validate_payload_against_env on one env must produce the same
    summary as validate_payload_token_coverage's same-env entry."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DDL/tables/{{DB_PREFIX}}_T.Customer.tbl": (
                "CREATE TABLE {{DB_PREFIX}}_T.Customer (id INTEGER);"
            ),
            "DCL/inter_db/{{ROLE_TOK}}.dcl": "GRANT SELECT ON X TO Y;\n",
        },
        env_configs={"DEV": "DB_PREFIX=dev\nOTHER_TOKEN=x\n"},
    )
    payload_dir = project / "payload" / "database"

    cross_env = validate_payload_token_coverage(str(payload_dir), str(project))
    direct = validate_payload_against_env(
        str(payload_dir),
        str(project),
        str(project / "config" / "env" / "DEV.conf"),
    )

    # Both should agree on every field — undefined, token_files,
    # filename_tokens, unreferenced.
    dev = cross_env["DEV"]
    assert direct["undefined"] == dev["undefined"]
    assert direct["token_files"] == dev["token_files"]
    assert direct["filename_tokens"] == dev["filename_tokens"]
    assert direct["unreferenced"] == dev["unreferenced"]


# ---------------------------------------------------------------
# Filename-token coverage closes the inspect/package gap
# ---------------------------------------------------------------


def test_filename_only_token_surfaced_to_package_gate(tmp_path: Path) -> None:
    """A payload whose only reference to a token is in a filename
    surfaces the token as undefined under the shared helper — which
    means the package-side gate would now reject it, matching
    inspect's behaviour. Before this PR, package only saw content
    tokens and would silently package this payload."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            # Filename carries {{DB_PREFIX}}; content does not.
            "DCL/inter_db/{{DB_PREFIX}}_BUS_V.dcl": (
                "GRANT SELECT ON SomeDb TO Other WITH GRANT OPTION;\n"
            ),
        },
        env_configs={"DEV": "OTHER_TOKEN=value\n"},
    )
    direct = validate_payload_against_env(
        str(project / "payload" / "database"),
        str(project),
        str(project / "config" / "env" / "DEV.conf"),
    )

    assert direct["undefined"] == ["DB_PREFIX"]
    assert "DB_PREFIX" in direct["filename_tokens"]
    # No content reference for this token — token_files entry is absent.
    assert "DB_PREFIX" not in direct["token_files"]


# ---------------------------------------------------------------
# Unreferenced filtering matches the historic validate_tokens set
# ---------------------------------------------------------------


def test_reserved_metadata_excluded_from_unreferenced(tmp_path: Path) -> None:
    """SHIPS_ENV / SHIPS_PROJECT / ENV_PREFIX / PERM_SPACE / SPOOL_SPACE /
    TEMP_SPACE / EXTERNAL_PARENTS — none should appear in
    ``unreferenced`` even when no payload file references them. Mirrors
    the reserved-property filter ``validate_tokens`` uses so the build
    report shape is preserved."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DDL/tables/Customer.tbl": (
                "CREATE TABLE {{DB_PREFIX}}_T.Customer (id INTEGER);"
            ),
        },
        env_configs={
            "DEV": (
                "DB_PREFIX=dev\n"
                "SHIPS_ENV=DEV\n"
                "SHIPS_PROJECT=CallCentre\n"
                "ENV_PREFIX=A_D01\n"
                "PERM_SPACE=1G\n"
                "SPOOL_SPACE=4G\n"
                "TEMP_SPACE=1G\n"
                "EXTERNAL_PARENTS=DATAPRODUCTS\n"
                "REAL_UNUSED_TOKEN=value\n"
            )
        },
    )
    direct = validate_payload_against_env(
        str(project / "payload" / "database"),
        str(project),
        str(project / "config" / "env" / "DEV.conf"),
    )

    assert direct["undefined"] == []
    # Only the genuinely unreferenced non-reserved token surfaces.
    assert direct["unreferenced"] == ["REAL_UNUSED_TOKEN"]


def test_unreferenced_counts_filename_references_as_used(tmp_path: Path) -> None:
    """A token referenced only via a filename is still ``used`` — it
    must not appear in ``unreferenced``. The legacy
    ``validate_tokens`` flow would have wrongly flagged such a token."""
    project = _scaffold_project(
        tmp_path / "project",
        payload_files={
            "DCL/inter_db/{{DB_PREFIX}}_BUS_V.dcl": (
                "GRANT SELECT ON SomeDb TO Other;\n"
            ),
        },
        env_configs={"DEV": "DB_PREFIX=dev\n"},
    )
    direct = validate_payload_against_env(
        str(project / "payload" / "database"),
        str(project),
        str(project / "config" / "env" / "DEV.conf"),
    )

    assert direct["undefined"] == []
    assert direct["unreferenced"] == []
