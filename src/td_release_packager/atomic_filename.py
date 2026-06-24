"""
atomic_filename — canonical filename derivation for atomic payload files.

The SHIPS invariant: every atomic file in ``payload/`` carries its
object's qualified identity in the payload's current tokenisation
state. Tokenise the identity → the filename carries the ``{{TOKEN}}``.
Detokenise the identity → the filename carries the literal. Filename
and the object identity inside the file are never permitted to diverge.

This module is the *single* function that names atomic payload files
across every pipeline phase (Harvest, Generate, Inspect, Package).
Routing every phase through one derivation eliminates the historical
drift where each phase rolled its own filename string.

The asymmetry that makes uniqueness possible: the database segment
of an object's qualifier may be a ``{{TOKEN}}`` expression
(``{{DB_PREFIX}}_DOM_STD_T`` or a whole-name ``{{DOM_STD_T}}``), but
the **object segment is always literal** — objects never carry
tokens under the SHIPS naming standard. The literal object name is
therefore the permanent uniqueness guarantor, even when the database
prefix is variable.

Defect 1 fix: callers that historically keyed on the qualifier alone
(or on the ``{{...}}`` head of the qualifier) collapsed *N* distinct
objects onto a single filename. The uniqueness key here is the full
``(qualifier_rendered, object_rendered)`` tuple — once the literal
object participates in the key, the collapse is structurally
impossible.

Layering:

    tokenised_name           parses identifiers → QualifiedName AST
        └── atomic_filename  renders QualifiedName → on-disk name
              └── callers    Harvest, Generate, Inspect, Package
"""

from __future__ import annotations

from typing import Mapping

from td_release_packager.tokenised_name import (
    QualifiedName,
    TokenisedNameError,
    parse_qualified_name,
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class FilenameDerivationError(ValueError):
    """Raised when an identity cannot be rendered to an atomic filename.

    The most common cause is a non-literal object segment (an
    identifier whose object half contains a ``{{TOKEN}}``). The
    SHIPS naming standard forbids tokens in the object segment; that
    is the invariant that lets ``(qualifier, object)`` serve as the
    uniqueness key.
    """


class DerivedFilenameClash(ValueError):
    """Raised at the write step when two distinct identities derive
    the same filename.

    A genuine clash is a bug — it means a payload would lose objects
    to silent overwrite. The error is intentionally hard: callers
    catch it and surface the colliding identities to the operator.
    """

    def __init__(self, filename: str, existing: str, incoming: str) -> None:
        super().__init__(
            f"Derived filename clash: {filename!r} already produced by "
            f"identity {existing!r}; refusing to overwrite with {incoming!r}. "
            "Distinct objects must never fold to one file."
        )
        self.filename = filename
        self.existing = existing
        self.incoming = incoming


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def derive_filename(identity: QualifiedName, ext: str) -> str:
    """Render the on-disk filename for an atomic payload file.

    The qualifier is preserved verbatim — token braces and literal
    text are emitted as-is. The object segment is required to be
    pure literal; raising on a token-bearing object is a strict
    invariant check, not a stylistic one.

    Args:
        identity: Parsed qualified name from
            ``tokenised_name.parse_qualified_name``.
        ext: Object-class extension including the leading dot
            (``.tbl``, ``.viw``, ``.dcl`` …). Source the canonical
            value from ``classifier.TYPE_TO_EXTENSION``.

    Returns:
        ``"<qualifier>.<object><ext>"`` for qualified identities, or
        ``"<object><ext>"`` for unqualified ones (DATABASE, USER,
        ROLE, etc.).

    Raises:
        FilenameDerivationError: if ``identity.object`` is not pure
            literal, or if ``ext`` is empty / missing its leading dot.
    """
    if not ext or not ext.startswith("."):
        raise FilenameDerivationError(f"extension must start with '.': {ext!r}")

    # The literal-object invariant applies to *qualified* identities.
    # System-scope objects (DATABASE, USER, ROLE — unqualified) are
    # the database, so their whole name may legitimately be a token
    # like ``{{BASE_NODE}}``. Uniqueness is preserved either way: a
    # qualified identity's uniqueness key is ``(qualifier, object)``,
    # an unqualified one's is just the object's rendered text.
    if identity.database is not None and not identity.object.is_pure_literal:
        raise FilenameDerivationError(
            f"qualified-object segment must be pure literal, got tokens "
            f"{identity.object.tokens!r} in {identity.object.render()!r}. "
            "The SHIPS naming standard forbids tokens in the object "
            "segment of a qualified name; uniqueness depends on it."
        )

    obj = identity.object.render()
    if identity.database is None:
        return f"{obj}{ext}"
    qualifier = identity.database.render()
    return f"{qualifier}.{obj}{ext}"


def derive_filename_from_text(qualified: str, ext: str) -> str:
    """Convenience wrapper: parse ``qualified`` then derive.

    Equivalent to ``derive_filename(parse_qualified_name(qualified), ext)``
    but with a single failure mode for callers that already hold the
    qualified name as a string (e.g. ``"{{DB}}.MyTable"``).

    Raises:
        FilenameDerivationError: on any parse or derivation failure.
    """
    try:
        identity = parse_qualified_name(qualified)
    except TokenisedNameError as exc:
        raise FilenameDerivationError(
            f"cannot parse qualified name {qualified!r}: {exc}"
        ) from exc
    return derive_filename(identity, ext)


# --------------------------------------------------------------------------
# Inverse — Package phase
# --------------------------------------------------------------------------


def detokenise_filename(name: str, env: Mapping[str, str]) -> str:
    """Detokenise an atomic payload filename using an env-config map.

    Parsing is robust because tokens never contain dots: splitting on
    the last two dots gives ``[qualifier, object, ext]`` regardless of
    underscores or braces inside the qualifier.

    Unqualified filenames (one dot — ``<object><ext>``) pass through
    with no token resolution; tokens only appear in the qualifier
    half under the SHIPS naming standard.

    Args:
        name: A payload filename, e.g. ``"{{DB_PREFIX}}_DOM_STD_T.Customer.tbl"``.
        env: Token-name → literal-value map (typically loaded from
            ``config/env/<ENV>.conf``).

    Returns:
        The detokenised filename, ``"DEV_03_DOM_STD_T.Customer.tbl"``.

    Raises:
        FilenameDerivationError: if a qualifier token is unresolved
            by ``env`` (missing key), or the filename has no extension.
    """
    if "." not in name:
        raise FilenameDerivationError(f"filename has no extension: {name!r}")

    parts = name.rsplit(".", 2)
    if len(parts) == 2:
        # Unqualified: ``<object>.<ext>``. Under the SHIPS naming
        # standard a tokenised qualifier produces a 3-part name
        # (``{{TOKEN}}.<object>.<ext>``), but a *system-scope* object
        # — CREATE DATABASE / USER / ROLE — IS the database, so its
        # whole name may be a token (``{{BASE_NODE}}.db``). Resolve
        # the object half so those unqualified tokenised names
        # detokenise rather than passing through verbatim.
        obj_text, ext = parts
        try:
            resolved_obj = parse_qualified_name(obj_text).object.resolve(
                env, strict=True
            )
        except TokenisedNameError as exc:
            raise FilenameDerivationError(
                f"cannot parse object {obj_text!r} in {name!r}: {exc}"
            ) from exc
        except KeyError as exc:
            raise FilenameDerivationError(
                f"unresolved token {exc.args[0]!r} in {name!r}"
            ) from exc
        return f"{resolved_obj}.{ext}"

    qualifier, obj, ext = parts
    try:
        resolved_qualifier = parse_qualified_name(qualifier).object.resolve(
            env, strict=True
        )
    except TokenisedNameError as exc:
        raise FilenameDerivationError(
            f"cannot parse qualifier {qualifier!r} in {name!r}: {exc}"
        ) from exc
    except KeyError as exc:
        raise FilenameDerivationError(
            f"unresolved token {exc.args[0]!r} in qualifier of {name!r}"
        ) from exc
    return f"{resolved_qualifier}.{obj}.{ext}"
