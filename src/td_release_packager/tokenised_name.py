"""
tokenised_name — shared parser for token-laced SQL object names.

Both the token-resolution collision audit and the dependency analyser need to
take an identifier like ``{{DB_PREFIX}}_SEM_STD_V`` or ``{{DB}}.{{OBJ}}`` and
decompose it into the literal fragments and ``{{TOKEN}}`` references that
compose it. Doing this in two places with hand-rolled regexes drifts; this
module is the single source of truth.

Token syntax supported:
    {{NAME}}            canonical token
    $NAME, ${NAME}      legacy placeholder (normalised to {{NAME}})
    &&NAME&&            legacy placeholder (normalised to {{NAME}})

Names may be:
    bare            ``MyView``
    quoted          ``"My View"``           (no token substitution inside)
    token-only      ``{{DB}}``
    token-prefix    ``{{PFX}}_SEM_STD_V``
    multi-token     ``{{ENV}}_{{SUFFIX}}``
    qualified       ``db.obj``              (either side may be tokenised)

The parser is intentionally permissive about *what* a SQL identifier is — it
does not enforce reserved-word or length rules; those belong to the deployer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Tuple, Union


# --------------------------------------------------------------------------
# Token-form recognition
# --------------------------------------------------------------------------

# Canonical token: {{NAME}}
_CANONICAL = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")

# Legacy forms — normalised to {{NAME}} on parse so downstream code only ever
# sees canonical TokenRef objects.
_LEGACY_DOLLAR_BRACED = re.compile(r"\$\{([A-Za-z_]\w*)\}")
_LEGACY_DOLLAR_BARE = re.compile(r"\$([A-Za-z_]\w*)")
_LEGACY_AMP = re.compile(r"&&([A-Za-z_]\w*)&&")


def _normalise_legacy(text: str) -> str:
    """Rewrite ``$NAME``, ``${NAME}``, ``&&NAME&&`` to ``{{NAME}}``.

    Order matters: ``${NAME}`` must be tried before ``$NAME`` so the braces
    are not partially consumed.
    """
    text = _LEGACY_AMP.sub(r"{{\1}}", text)
    text = _LEGACY_DOLLAR_BRACED.sub(r"{{\1}}", text)
    text = _LEGACY_DOLLAR_BARE.sub(r"{{\1}}", text)
    return text


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenRef:
    """A ``{{NAME}}`` reference inside an identifier."""

    name: str

    def __str__(self) -> str:
        return "{{" + self.name + "}}"


Fragment = Union[str, TokenRef]


@dataclass(frozen=True)
class NamePart:
    """One side of a possibly-qualified SQL name.

    A bare name like ``MyTbl`` becomes ``NamePart((“MyTbl”,))``.
    A token-prefixed name like ``{{PFX}}_V`` becomes
    ``NamePart((TokenRef("PFX"), "_V"))``.
    """

    fragments: Tuple[Fragment, ...]
    quoted: bool = False

    @property
    def tokens(self) -> Tuple[str, ...]:
        """Names of all ``{{TOKEN}}`` references in source order, with duplicates preserved."""
        return tuple(f.name for f in self.fragments if isinstance(f, TokenRef))

    @property
    def is_pure_literal(self) -> bool:
        """True when this part contains no token references."""
        return not any(isinstance(f, TokenRef) for f in self.fragments)

    @property
    def is_pure_token(self) -> bool:
        """True when this part is exactly one ``{{TOKEN}}`` and nothing else."""
        return len(self.fragments) == 1 and isinstance(self.fragments[0], TokenRef)

    def render(self) -> str:
        """Reassemble the original tokenised source string (sans quoting)."""
        return "".join(str(f) for f in self.fragments)

    def resolve(self, env: Mapping[str, str], *, strict: bool = True) -> str:
        """Substitute every TokenRef from ``env`` and concatenate.

        Args:
            env: token name -> value mapping.
            strict: when True, missing tokens raise KeyError. When False, the
                literal ``{{NAME}}`` is left in place so callers can detect
                under-resolution downstream.
        """
        out = []
        for frag in self.fragments:
            if isinstance(frag, TokenRef):
                if frag.name in env:
                    out.append(env[frag.name])
                elif strict:
                    raise KeyError(frag.name)
                else:
                    out.append(str(frag))
            else:
                out.append(frag)
        return "".join(out)

    def __str__(self) -> str:
        rendered = self.render()
        return f'"{rendered}"' if self.quoted else rendered


@dataclass(frozen=True)
class QualifiedName:
    """A SQL object name, possibly qualified by a database segment.

    ``database`` is ``None`` for unqualified names like ``MyTbl``.
    """

    database: NamePart | None
    object: NamePart

    @property
    def tokens(self) -> Tuple[str, ...]:
        """All tokens across both segments, in source order."""
        db_tokens = self.database.tokens if self.database is not None else ()
        return db_tokens + self.object.tokens

    @property
    def is_qualified(self) -> bool:
        return self.database is not None

    def render(self) -> str:
        if self.database is None:
            return str(self.object)
        return f"{self.database}.{self.object}"

    def resolve(self, env: Mapping[str, str], *, strict: bool = True) -> str:
        obj = self.object.resolve(env, strict=strict)
        if self.database is None:
            return obj
        db = self.database.resolve(env, strict=strict)
        return f"{db}.{obj}"

    def __str__(self) -> str:
        return self.render()


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class TokenisedNameError(ValueError):
    """Raised for syntactically malformed tokenised names."""


def _lex_fragments(text: str) -> Tuple[Fragment, ...]:
    """Split a string into interleaved literal and ``{{TOKEN}}`` fragments.

    Adjacent literals are merged. Empty literals are dropped.
    """
    out: list[Fragment] = []
    pos = 0
    for match in _CANONICAL.finditer(text):
        if match.start() > pos:
            out.append(text[pos : match.start()])
        out.append(TokenRef(match.group(1)))
        pos = match.end()
    if pos < len(text):
        out.append(text[pos:])
    # Drop empty literal heads/tails that arise from {{TOK}}_V having no
    # leading literal.
    return tuple(f for f in out if not (isinstance(f, str) and f == ""))


def _split_qualifier(text: str) -> Tuple[str | None, str]:
    """Split ``db.obj`` into ``(db, obj)``, honouring quoted identifiers.

    A dot inside ``"..."`` does not qualify. Returns ``(None, text)`` when
    there is no top-level dot.
    """
    depth_quote = False
    last_dot = -1
    for i, ch in enumerate(text):
        if ch == '"':
            depth_quote = not depth_quote
        elif ch == "." and not depth_quote:
            # Right-most top-level dot wins. SQL qualified names are
            # at most two-part here (db.obj); deeper qualifiers like
            # db.schema.obj aren't a Teradata pattern but if encountered,
            # everything left of the final dot is treated as the database
            # segment.
            last_dot = i
    if last_dot < 0:
        return None, text
    return text[:last_dot], text[last_dot + 1 :]


def _parse_part(text: str) -> NamePart:
    """Parse one side of a qualified name into a NamePart."""
    raw = text.strip()
    if not raw:
        raise TokenisedNameError("empty identifier part")
    quoted = False
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        quoted = True
        raw = raw[1:-1]
        # Inside quotes the canonical-token rule still applies (e.g. a
        # quoted identifier may itself be entirely a {{TOKEN}}), but legacy
        # forms ($NAME, &&...&&) are not normalised — quoting is a literal
        # request.
        fragments = _lex_fragments(raw)
    else:
        fragments = _lex_fragments(_normalise_legacy(raw))
    if not fragments:
        raise TokenisedNameError(f"identifier resolves to nothing: {text!r}")
    return NamePart(fragments=fragments, quoted=quoted)


def parse_qualified_name(text: str) -> QualifiedName:
    """Parse a one- or two-part tokenised SQL object name.

    Examples:
        >>> parse_qualified_name("MyTbl").object.is_pure_literal
        True
        >>> parse_qualified_name("{{DB}}.{{OBJ}}").tokens
        ('DB', 'OBJ')
        >>> parse_qualified_name("{{PFX}}_SEM_STD_V").object.tokens
        ('PFX',)

    Raises:
        TokenisedNameError: if the input is empty or malformed.
    """
    if text is None:
        raise TokenisedNameError("cannot parse None")
    stripped = text.strip()
    if not stripped:
        raise TokenisedNameError("cannot parse empty string")

    db_text, obj_text = _split_qualifier(stripped)
    obj_part = _parse_part(obj_text)
    db_part = _parse_part(db_text) if db_text is not None else None
    return QualifiedName(database=db_part, object=obj_part)


def extract_tokens(text: str) -> Tuple[str, ...]:
    """Return every ``{{TOKEN}}`` name referenced anywhere in ``text``.

    Convenience for callers that want a flat list and do not care about
    structure. Legacy forms are normalised first.
    """
    normalised = _normalise_legacy(text)
    return tuple(m.group(1) for m in _CANONICAL.finditer(normalised))


def iter_token_refs(text: str) -> Iterable[Tuple[int, str]]:
    """Yield ``(offset, token_name)`` for each canonical ``{{TOKEN}}`` in ``text``.

    Offsets are into the original string (legacy forms are not normalised
    here — use ``extract_tokens`` if you want legacy-aware extraction).
    """
    for m in _CANONICAL.finditer(text):
        yield m.start(), m.group(1)
