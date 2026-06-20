"""
test_external_parents_env_config.py — PR5a of the deterministic-deploy
programme.

Locks down the env-config-declared external-parents path
(HANDOVER-ships-deterministic-deploy.md §PR5, option (b)):

  * EXTERNAL_PARENTS=A,B in the env config is parsed into a normalised
    set of identifier names.
  * The set is plumbed through to
    ``analyse_environment_parent_requirements`` so the build's
    environment-prereqs gate stops flagging legitimate external
    parents (e.g. CallCentre under DATAPRODUCTS) as needing a DBA
    amendment.
  * EXTERNAL_PARENTS is exempt from the unreferenced-token warning
    (it's metadata, not a substituted token).
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.environment_prereqs import (
    _DEFAULT_KNOWN_EXTERNAL_PARENTS,
    analyse_environment_parent_requirements,
    parse_external_parents_from_env,
)
from td_release_packager.token_engine import validate_tokens


# ---------------------------------------------------------------
# parse_external_parents_from_env
# ---------------------------------------------------------------


def test_parse_absent_returns_empty_set() -> None:
    """No EXTERNAL_PARENTS declaration → empty set. Caller is then
    expected to fall back to ``_DEFAULT_KNOWN_EXTERNAL_PARENTS`` —
    behaviour-preserving for any env config that pre-dates PR5a."""
    assert parse_external_parents_from_env({"OTHER": "value"}) == set()


def test_parse_empty_returns_empty_set() -> None:
    """``EXTERNAL_PARENTS=`` (empty value) is treated as no declaration."""
    assert parse_external_parents_from_env({"EXTERNAL_PARENTS": ""}) == set()
    assert parse_external_parents_from_env({"EXTERNAL_PARENTS": "   "}) == set()


def test_parse_single_value() -> None:
    """One parent, no commas — returns a single-element upper-cased set."""
    assert parse_external_parents_from_env({"EXTERNAL_PARENTS": "DataProducts"}) == {
        "DATAPRODUCTS"
    }


def test_parse_comma_separated_multiple() -> None:
    """Comma-separated list normalises to upper-case and trims whitespace."""
    parsed = parse_external_parents_from_env(
        {"EXTERNAL_PARENTS": "DataProducts, SysDba ,FINANCE_BASE"}
    )
    assert parsed == {"DATAPRODUCTS", "SYSDBA", "FINANCE_BASE"}


def test_parse_ignores_blank_entries() -> None:
    """Stray empty entries between commas are dropped."""
    parsed = parse_external_parents_from_env(
        {"EXTERNAL_PARENTS": "DataProducts,,SysDba,"}
    )
    assert parsed == {"DATAPRODUCTS", "SYSDBA"}


# ---------------------------------------------------------------
# analyse_environment_parent_requirements — declared parent excluded
# ---------------------------------------------------------------


def _make_prereqs_package(tmp_path: Path, child: str, parent: str) -> Path:
    """Build the minimal post-split prereqs package layout the analyser
    walks (``payload/01_pre_requisites/databases/<name>.db``)."""
    pkg = tmp_path / "pkg"
    dbs = pkg / "payload" / "01_pre_requisites" / "databases"
    dbs.mkdir(parents=True)
    (dbs / f"{child}.db").write_text(
        f"CREATE DATABASE {child} FROM {parent} AS PERM=1000000;\n",
        encoding="utf-8",
    )
    return pkg


def test_declared_external_parent_not_flagged(tmp_path: Path) -> None:
    """When the env config declares a parent, the analyser treats it as
    pre-existing and emits no environment-prereq requirement."""
    pkg = _make_prereqs_package(tmp_path, child="CallCentre", parent="DATAPRODUCTS")

    declared = parse_external_parents_from_env({"EXTERNAL_PARENTS": "DataProducts"})
    # Mirror the build-time union with the default (DBC) baseline.
    effective = set(_DEFAULT_KNOWN_EXTERNAL_PARENTS) | declared

    requirements = analyse_environment_parent_requirements(
        str(pkg), known_external_parents=effective
    )
    assert requirements == [], (
        "DATAPRODUCTS was declared as an external parent — the build "
        "should not generate an environment-prereqs requirement."
    )


def test_undeclared_external_parent_still_flagged(tmp_path: Path) -> None:
    """The exemption is targeted. An undeclared external parent still
    triggers the environment-prereq requirement — same behaviour as
    before PR5a for the safety-net case where the operator hasn't
    yet declared the parent."""
    pkg = _make_prereqs_package(tmp_path, child="CallCentre", parent="DATAPRODUCTS")

    declared = parse_external_parents_from_env({"OTHER": "ignored"})
    effective = set(_DEFAULT_KNOWN_EXTERNAL_PARENTS) | declared if declared else None

    requirements = analyse_environment_parent_requirements(
        str(pkg), known_external_parents=effective
    )
    assert len(requirements) == 1
    assert requirements[0].parent_name == "DATAPRODUCTS"


def test_dbc_remains_exempt_when_external_parents_declared(
    tmp_path: Path,
) -> None:
    """Declaring EXTERNAL_PARENTS must not lose the implicit DBC
    exemption — DBC is the universal platform root. Regression guard
    for the build-side union with ``_DEFAULT_KNOWN_EXTERNAL_PARENTS``."""
    pkg = _make_prereqs_package(tmp_path, child="MyDb", parent="DBC")

    declared = parse_external_parents_from_env({"EXTERNAL_PARENTS": "DATAPRODUCTS"})
    effective = set(_DEFAULT_KNOWN_EXTERNAL_PARENTS) | declared

    requirements = analyse_environment_parent_requirements(
        str(pkg), known_external_parents=effective
    )
    assert requirements == []


# ---------------------------------------------------------------
# EXTERNAL_PARENTS exempt from "unreferenced" warning
# ---------------------------------------------------------------


def test_external_parents_not_warned_as_unreferenced() -> None:
    """EXTERNAL_PARENTS is metadata (build-time gate input), not a
    DDL-substituted token, so the unreferenced-token warning must
    not fire for it. Mirrors the same exemption added for
    PERM_SPACE / SPOOL_SPACE in PR6."""
    token_values = {
        "EXTERNAL_PARENTS": "DATAPRODUCTS",
        "DB_PREFIX": "CallCentre",
    }
    token_usage = {
        "payload/database/DDL/tables/Customer.tbl": {"DB_PREFIX"},
    }

    _errors, warnings = validate_tokens(token_values, token_usage)
    joined = "\n".join(warnings)
    assert "{{EXTERNAL_PARENTS}}" not in joined, (
        "EXTERNAL_PARENTS is metadata, not a substituted token — "
        "must be excluded from the unreferenced-token warning."
    )
