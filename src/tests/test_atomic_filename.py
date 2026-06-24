"""Unit tests for ``td_release_packager.atomic_filename``.

Covers the canonical ``derive_filename`` contract and its inverse
``detokenise_filename``. The thing every test ultimately checks is
the SHIPS invariant: filename and parsed identity stay locked
together across tokenisation states.
"""

from __future__ import annotations

import pytest

from td_release_packager.atomic_filename import (
    DerivedFilenameClash,
    FilenameDerivationError,
    derive_filename,
    derive_filename_from_text,
    detokenise_filename,
)
from td_release_packager.tokenised_name import parse_qualified_name


# --------------------------------------------------------------------------
# derive_filename — happy paths
# --------------------------------------------------------------------------


def test_literal_db_and_object() -> None:
    name = derive_filename(
        parse_qualified_name("CallCentre_DOM_STD_T.Customer"), ".tbl"
    )
    assert name == "CallCentre_DOM_STD_T.Customer.tbl"


def test_token_prefix_db() -> None:
    name = derive_filename(
        parse_qualified_name("{{DB_PREFIX}}_DOM_STD_T.Customer"), ".tbl"
    )
    assert name == "{{DB_PREFIX}}_DOM_STD_T.Customer.tbl"


def test_whole_name_db_token() -> None:
    name = derive_filename(parse_qualified_name("{{DOM_STD_T}}.Customer"), ".tbl")
    assert name == "{{DOM_STD_T}}.Customer.tbl"


def test_multi_token_db() -> None:
    name = derive_filename(parse_qualified_name("{{ENV}}_{{SUFFIX}}.Customer"), ".tbl")
    assert name == "{{ENV}}_{{SUFFIX}}.Customer.tbl"


def test_unqualified_object() -> None:
    # System-scope objects (DATABASE, ROLE, USER) have no DB qualifier.
    name = derive_filename(parse_qualified_name("DOM_STD_T"), ".db")
    assert name == "DOM_STD_T.db"


def test_dcl_extension() -> None:
    name = derive_filename(parse_qualified_name("{{DOM_STD_T}}.Customer"), ".dcl")
    assert name == "{{DOM_STD_T}}.Customer.dcl"


# --------------------------------------------------------------------------
# derive_filename — uniqueness key (the Defect 1 fix)
# --------------------------------------------------------------------------


def test_n_objects_in_one_tokenised_db_produce_n_distinct_filenames() -> None:
    """N objects sharing one tokenised database → N distinct filenames.

    This is the structural property that prevents the historical
    collapse where all N objects hashed onto one filename.
    """
    objects = ["Customer", "Account", "Transaction", "Branch", "Product"]
    names = {
        derive_filename(
            parse_qualified_name(f"{{{{DB_PREFIX}}}}_DOM_STD_T.{obj}"), ".tbl"
        )
        for obj in objects
    }
    assert len(names) == len(objects)


# --------------------------------------------------------------------------
# derive_filename — invariant failures
# --------------------------------------------------------------------------


def test_token_in_qualified_object_segment_raises() -> None:
    with pytest.raises(
        FilenameDerivationError, match="qualified-object segment must be pure literal"
    ):
        derive_filename(parse_qualified_name("{{DB}}.{{OBJ}}"), ".tbl")


def test_unqualified_whole_name_token_allowed() -> None:
    """System-scope DATABASE/USER/ROLE may legitimately be a token —
    the unqualified name IS the database, so the literal-object
    invariant does not apply."""
    name = derive_filename(parse_qualified_name("{{BASE_NODE}}"), ".db")
    assert name == "{{BASE_NODE}}.db"


def test_extension_without_dot_raises() -> None:
    with pytest.raises(FilenameDerivationError, match="must start with '.'"):
        derive_filename(parse_qualified_name("DB.Customer"), "tbl")


def test_empty_extension_raises() -> None:
    with pytest.raises(FilenameDerivationError):
        derive_filename(parse_qualified_name("DB.Customer"), "")


# --------------------------------------------------------------------------
# derive_filename_from_text — convenience
# --------------------------------------------------------------------------


def test_from_text_round_trips() -> None:
    name = derive_filename_from_text("{{DB_PREFIX}}_DOM_STD_T.Customer", ".tbl")
    assert name == "{{DB_PREFIX}}_DOM_STD_T.Customer.tbl"


def test_from_text_bad_parse_raises() -> None:
    with pytest.raises(FilenameDerivationError, match="cannot parse"):
        derive_filename_from_text("", ".tbl")


# --------------------------------------------------------------------------
# detokenise_filename — Package inverse
# --------------------------------------------------------------------------


def test_detok_substitutes_db_token() -> None:
    out = detokenise_filename(
        "{{DB_PREFIX}}_DOM_STD_T.Customer.tbl", {"DB_PREFIX": "DEV_03"}
    )
    assert out == "DEV_03_DOM_STD_T.Customer.tbl"


def test_detok_whole_name_token() -> None:
    out = detokenise_filename(
        "{{DOM_STD_T}}.Customer.tbl", {"DOM_STD_T": "PRD_DOM_STD_T"}
    )
    assert out == "PRD_DOM_STD_T.Customer.tbl"


def test_detok_literal_passes_through() -> None:
    out = detokenise_filename("CallCentre_DOM_STD_T.Customer.tbl", {})
    assert out == "CallCentre_DOM_STD_T.Customer.tbl"


def test_detok_unqualified_literal_passes_through() -> None:
    out = detokenise_filename("DOM_STD_T.db", {"DB_PREFIX": "DEV_03"})
    assert out == "DOM_STD_T.db"


def test_detok_unqualified_tokenised_resolves() -> None:
    """System-scope objects (CREATE DATABASE / USER / ROLE) carry the
    whole name as a token. The unqualified single-dot form must still
    resolve under the env map — passing through verbatim would leave
    a tokenised release filename, defeating Package's job."""
    out = detokenise_filename("{{BASE_NODE}}.db", {"BASE_NODE": "DEV_BASE"})
    assert out == "DEV_BASE.db"


def test_detok_unqualified_token_unresolved_raises() -> None:
    with pytest.raises(FilenameDerivationError, match="unresolved token"):
        detokenise_filename("{{BASE_NODE}}.db", {})


def test_detok_dcl_extension() -> None:
    out = detokenise_filename(
        "{{DOM_STD_T}}.Customer.dcl", {"DOM_STD_T": "PRD_DOM_STD_T"}
    )
    assert out == "PRD_DOM_STD_T.Customer.dcl"


def test_detok_unresolved_token_raises() -> None:
    with pytest.raises(FilenameDerivationError, match="unresolved token"):
        detokenise_filename("{{DB_PREFIX}}_DOM_STD_T.Customer.tbl", {})


def test_detok_no_extension_raises() -> None:
    with pytest.raises(FilenameDerivationError, match="no extension"):
        detokenise_filename("noextension", {})


# --------------------------------------------------------------------------
# Round-trip — name and body share one substitution surface
# --------------------------------------------------------------------------


def test_derive_then_detok_round_trip() -> None:
    """Whatever derive_filename produces, detokenise_filename undoes
    using the same env map. The single-substitution invariant."""
    env = {"DB_PREFIX": "DEV_03"}
    payload_name = derive_filename(
        parse_qualified_name("{{DB_PREFIX}}_DOM_STD_T.Customer"), ".tbl"
    )
    release_name = detokenise_filename(payload_name, env)
    assert release_name == "DEV_03_DOM_STD_T.Customer.tbl"


# --------------------------------------------------------------------------
# DerivedFilenameClash — collision guard wiring
# --------------------------------------------------------------------------


def test_clash_carries_both_identities() -> None:
    exc = DerivedFilenameClash(
        filename="{{DB}}_T.X.tbl",
        existing="{{DB}}_T.X (from a.sql)",
        incoming="{{DB}}_T.X (from b.sql)",
    )
    msg = str(exc)
    assert "{{DB}}_T.X.tbl" in msg
    assert "a.sql" in msg
    assert "b.sql" in msg
