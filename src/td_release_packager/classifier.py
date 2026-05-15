"""
classifier.py — Rich content-based classification of DDL/SQL files.

Replaces the flat ``_classify_ddl`` regex pass in ``ingest.py`` with
a structured pipeline that returns a ``ClassificationResult`` —
type plus confidence, evidence, external-reference list, and
warnings about filename/content mismatches.

Three things this module does that the old classifier didn't:

  1. **External-reference extraction** for C UDFs and Java
     procedures. ``CREATE FUNCTION ... LANGUAGE C ... EXTERNAL NAME
     'CS!alias!path/foo.c!CH!alias!path/foo.h'`` populates
     ``related_files`` with the .c/.h paths so the deployer can
     bundle them in the right order. ``CREATE PROCEDURE ...
     LANGUAGE JAVA ... EXTERNAL NAME 'jar_alias:com.x.Foo.bar'``
     records the JAR alias for cross-referencing against the
     ``CALL SQLJ.INSTALL_JAR(..., 'jar_alias', ...)`` script that
     installs it.

  2. **Filename-vs-content mismatch warnings.** A file named
     ``foo.tbl`` whose content is ``CREATE PROCEDURE ...`` is still
     classified as PROCEDURE (content always wins) but a HIGH-
     priority warning is attached so the user can see SHIPS made
     a judgement call and rename the source file if appropriate.

  3. **Sub-types** that distinguish dialect:
       ``FUNCTION_C``     vs ``FUNCTION_SQL``
       ``PROCEDURE_JAVA`` / ``PROCEDURE_CPP`` vs ``PROCEDURE_SPL``
     The base type ("FUNCTION", "PROCEDURE") is preserved through
     ``base_type()`` for callers that don't care about dialect.
     Extensions and subdirectories are unchanged from the base.

What this module does NOT do (deferred from v1):

  - Multi-statement file handling (one type per file for now)
  - Heuristic recovery for unclassifiable files
  - Cross-file dependency resolution (analyze stage handles that)

Single-source-of-truth note: ``TYPE_TO_EXTENSION`` and
``TYPE_TO_SUBDIR`` live here. ``ingest.py`` imports them. A
follow-up should also move ``validate.py``'s mirror copy to use
this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------
# Type taxonomy
# ---------------------------------------------------------------


#: Canonical SHIPS object/operation types. Sub-types like
#: ``FUNCTION_C`` are mapped back to their base via SUBTYPE_TO_BASE.
BASE_TYPES: Set[str] = {
    "TABLE",
    "JOIN_INDEX",
    "HASH_INDEX",
    "INDEX",
    "VIEW",
    "MACRO",
    "PROCEDURE",
    "FUNCTION",
    "TRIGGER",
    "DATABASE",
    "USER",
    "GRANT",
    "REVOKE",
    "COMMENT",
    "STATISTICS",
    "JAR",
    "SCRIPT_TABLE_OPERATOR",
    "MAP",
    "ROLE",
    "PROFILE",
    "AUTHORIZATION",
    "FOREIGN_SERVER",
    "C_SOURCE",
    "C_HEADER",
    "DML",
    "FOREIGN_KEY",
}


#: Sub-types add dialect specificity. Each maps back to a base type.
SUBTYPE_TO_BASE: Dict[str, str] = {
    "FUNCTION_C": "FUNCTION",
    "FUNCTION_SQL": "FUNCTION",
    "PROCEDURE_JAVA": "PROCEDURE",
    "PROCEDURE_CPP": "PROCEDURE",
    "PROCEDURE_SPL": "PROCEDURE",
}


def base_type(t: Optional[str]) -> Optional[str]:
    """Map a sub-type back to its base. Pass-through for plain types."""
    if t is None:
        return None
    return SUBTYPE_TO_BASE.get(t, t)


#: Output extension per type. Sub-types share the base extension —
#: dialect is reflected in the classification, not the filename.
TYPE_TO_EXTENSION: Dict[str, str] = {
    "TABLE": ".tbl",
    "JOIN_INDEX": ".jix",
    "HASH_INDEX": ".idx",
    "INDEX": ".idx",
    "VIEW": ".viw",
    "MACRO": ".mcr",
    "PROCEDURE": ".spl",
    "PROCEDURE_SPL": ".spl",
    "PROCEDURE_JAVA": ".spl",
    "PROCEDURE_CPP": ".spl",
    "FUNCTION": ".fnc",
    "FUNCTION_SQL": ".fnc",
    "FUNCTION_C": ".fnc",
    "TRIGGER": ".trg",
    "DATABASE": ".db",
    "USER": ".usr",
    "GRANT": ".dcl",
    "REVOKE": ".dcl",
    "COMMENT": ".cmt",
    "STATISTICS": ".stt",
    # SQLJ install script — see ingest commit history for the
    # rationale for .sjr (not .jar).
    "JAR": ".sjr",
    "SCRIPT_TABLE_OPERATOR": ".sto",
    "MAP": ".map",
    "ROLE": ".rol",
    "PROFILE": ".prf",
    "AUTHORIZATION": ".auth",
    "FOREIGN_SERVER": ".fsvr",
    "C_SOURCE": ".c",
    "C_HEADER": ".h",
    "DML": ".dml",
    "FOREIGN_KEY": ".fk",
}


#: Subdirectory under ``payload/database/`` per type.
TYPE_TO_SUBDIR: Dict[str, str] = {
    "TABLE": "DDL/tables",
    "JOIN_INDEX": "DDL/join_indexes",
    "HASH_INDEX": "DDL/join_indexes",
    "INDEX": "DDL/join_indexes",
    "VIEW": "DDL/views",
    "MACRO": "DDL/macros",
    "PROCEDURE": "DDL/procedures",
    "PROCEDURE_SPL": "DDL/procedures",
    "PROCEDURE_JAVA": "DDL/procedures",
    "PROCEDURE_CPP": "DDL/procedures",
    "FUNCTION": "DDL/functions",
    "FUNCTION_SQL": "DDL/functions",
    "FUNCTION_C": "DDL/functions",
    "TRIGGER": "DDL/triggers",
    "DATABASE": "pre-requisites/databases",
    "USER": "pre-requisites/users",
    "GRANT": "DCL/inter_db",
    "REVOKE": "DCL/inter_db",
    "COMMENT": "DDL/comments",
    "STATISTICS": "DDL/statistics",
    "JAR": "DDL/jar_install",
    "SCRIPT_TABLE_OPERATOR": "DDL/script_table_operators",
    "MAP": "system/maps",
    "ROLE": "system/roles",
    "PROFILE": "system/profiles",
    "AUTHORIZATION": "system/authorizations",
    "FOREIGN_SERVER": "system/foreign_servers",
    "C_SOURCE": "DDL/functions",
    "C_HEADER": "DDL/functions",
    "DML": "DML",
    "FOREIGN_KEY": "DDL/alters",
}


# ---------------------------------------------------------------
# Filename → expected types
# ---------------------------------------------------------------


#: Filename extensions that signal which types are expected. None
#: means a generic extension (.sql/.ddl/.dml) where any type is
#: legitimate. A file whose detected type isn't in the expected
#: set produces a filename-mismatch warning.
EXTENSION_TO_EXPECTED: Dict[str, Optional[Set[str]]] = {
    ".tbl": {"TABLE"},
    ".viw": {"VIEW"},
    ".mcr": {"MACRO"},
    ".spl": {"PROCEDURE", "PROCEDURE_SPL", "PROCEDURE_JAVA", "PROCEDURE_CPP"},
    ".fnc": {"FUNCTION", "FUNCTION_SQL", "FUNCTION_C"},
    ".trg": {"TRIGGER"},
    ".jix": {"JOIN_INDEX"},
    ".idx": {"INDEX", "HASH_INDEX"},
    ".db": {"DATABASE"},
    ".usr": {"USER"},
    ".sjr": {"JAR"},
    ".jar": {"JAR"},  # legacy alias
    ".cmt": {"COMMENT"},
    ".stt": {"STATISTICS"},
    ".sto": {"SCRIPT_TABLE_OPERATOR"},
    ".dcl": {"GRANT", "REVOKE"},
    ".map": {"MAP"},
    ".rol": {"ROLE"},
    ".prf": {"PROFILE"},
    ".auth": {"AUTHORIZATION"},
    ".fsvr": {"FOREIGN_SERVER"},
    # DML scripts — INSERT/UPDATE/DELETE/MERGE.
    ".dml": {"DML"},
    # Foreign key ALTER scripts — ALTER TABLE ... ADD FOREIGN KEY.
    ".fk": {"FOREIGN_KEY"},
    # Generic — any type acceptable
    ".sql": None,
    ".ddl": None,
    # BTEQ-style extensions. Legacy Teradata codebases sometimes
    # name pure-SQL scripts ``.bteq`` / ``.btq`` even when there
    # are no actual BTEQ commands in the body. Treat them as
    # generic so a CREATE TABLE in foo.bteq classifies as TABLE
    # without firing a filename-mismatch warning.
    ".bteq": None,
    ".btq": None,
}


# ---------------------------------------------------------------
# Pattern table
# ---------------------------------------------------------------


#: Compiled patterns paired with their detected type. Specific
#: patterns must come first — the first match wins.
#:
#: All verbs are anchored to the **start of a SQL statement**
#: via ``^\s*`` plus ``re.MULTILINE``. Anchoring exists to prevent
#: a verb appearing inside another statement (e.g. ``GRANT CREATE
#: PROCEDURE ON db TO user``) being mistaken for the file's primary
#: object. Without the anchor, that file classifies as PROCEDURE
#: (the substring ``CREATE PROCEDURE`` matches mid-line) instead
#: of GRANT — and lands in ``DDL/procedures/<name>.spl``, miles from
#: where the operator expects to find a permissions script.
#:
#: Multi-line bodies (CREATE PROCEDURE ... BEGIN ... END;) still
#: match because the ``^\s*CREATE PROCEDURE`` anchor only requires
#: line-leading position for the OPENING verb — the body that
#: follows can span any number of lines.
_STMT_FLAGS = re.IGNORECASE | re.MULTILINE
_CLASSIFY_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Indexes (most specific first)
    (re.compile(r"^\s*CREATE\s+JOIN\s+INDEX\b", _STMT_FLAGS), "JOIN_INDEX"),
    (re.compile(r"^\s*CREATE\s+HASH\s+INDEX\b", _STMT_FLAGS), "HASH_INDEX"),
    (re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", _STMT_FLAGS), "INDEX"),
    # SCRIPT_TABLE_OPERATOR (uses FUNCTION syntax but with TABLE OPERATOR).
    # DOTALL is needed for the ``.*?`` to span the FUNCTION header onto
    # the line containing TABLE OPERATOR; the leading ^\s* still anchors
    # the opening CREATE/REPLACE FUNCTION to a line start.
    (
        re.compile(
            r"^\s*(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b"
            r".*?TABLE\s+OPERATOR",
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        ),
        "SCRIPT_TABLE_OPERATOR",
    ),
    # FUNCTION sub-types — LANGUAGE C is detected by a follow-up
    # check inside `classify` (we need to know that FUNCTION
    # matched first). The pattern below catches both.
    (
        re.compile(
            r"^\s*(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b",
            _STMT_FLAGS,
        ),
        "FUNCTION",
    ),
    # PROCEDURE sub-types — sub-typing via post-process LANGUAGE check
    (re.compile(r"^\s*(?:CREATE|REPLACE)\s+PROCEDURE\b", _STMT_FLAGS), "PROCEDURE"),
    # Standard DDL objects
    (
        re.compile(
            r"^\s*(?:CREATE|REPLACE)\s+(?:MULTISET|SET)?\s*"
            r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
            r"(?:TRACE\s+)?TABLE\b",
            _STMT_FLAGS,
        ),
        "TABLE",
    ),
    (re.compile(r"^\s*(?:CREATE|REPLACE)\s+VIEW\b", _STMT_FLAGS), "VIEW"),
    (re.compile(r"^\s*(?:CREATE\s+|REPLACE\s+)MACRO\b", _STMT_FLAGS), "MACRO"),
    (re.compile(r"^\s*(?:CREATE|REPLACE)\s+TRIGGER\b", _STMT_FLAGS), "TRIGGER"),
    # Pre-requisites
    (re.compile(r"^\s*CREATE\s+DATABASE\b", _STMT_FLAGS), "DATABASE"),
    (re.compile(r"^\s*CREATE\s+USER\b", _STMT_FLAGS), "USER"),
    # System-scoped objects
    (re.compile(r"^\s*CREATE\s+MAP\b", _STMT_FLAGS), "MAP"),
    (re.compile(r"^\s*CREATE\s+PROFILE\b", _STMT_FLAGS), "PROFILE"),
    (re.compile(r"^\s*CREATE\s+ROLE\b", _STMT_FLAGS), "ROLE"),
    (re.compile(r"^\s*CREATE\s+AUTHORIZATION\b", _STMT_FLAGS), "AUTHORIZATION"),
    (re.compile(r"^\s*CREATE\s+FOREIGN\s+SERVER\b", _STMT_FLAGS), "FOREIGN_SERVER"),
    # JAR install scripts
    (
        re.compile(
            r"^\s*CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(",
            _STMT_FLAGS,
        ),
        "JAR",
    ),
    # Metadata + statistics. ``UPDATE STATISTICS`` is a Teradata
    # synonym for ``COLLECT STATISTICS`` — both refresh table stats.
    # Match it here BEFORE the generic UPDATE → DML pattern below so
    # stats-collection scripts don't get misclassified as DML.
    (re.compile(r"^\s*COMMENT\s+ON\b", _STMT_FLAGS), "COMMENT"),
    (
        re.compile(r"^\s*(?:COLLECT|UPDATE)\s+STATISTICS\b", _STMT_FLAGS),
        "STATISTICS",
    ),
    # DCL (least specific of the DDL family). Anchoring matters MORE
    # here than for the CREATE/REPLACE patterns — a real procedure
    # body can legally contain a string literal like
    # ``EXECUTE IMMEDIATE 'GRANT ...'``, which is stripped by the
    # caller, but if any GRANT survived in the body we'd still avoid
    # mis-classifying because the GRANT verb wouldn't be at line-start.
    (re.compile(r"^\s*GRANT\b", _STMT_FLAGS), "GRANT"),
    (re.compile(r"^\s*REVOKE\b", _STMT_FLAGS), "REVOKE"),
    # Foreign key constraint — ALTER TABLE ... ADD FOREIGN KEY.
    # Must appear before the generic DML patterns below so that an
    # ALTER TABLE ADD FOREIGN KEY script is not mis-classified as DML
    # if it happens to contain an embedded UPDATE or INSERT keyword.
    (
        re.compile(
            r"^\s*ALTER\s+TABLE\b.*?\bADD\s+FOREIGN\s+KEY\b",
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        ),
        "FOREIGN_KEY",
    ),
    # DML — comes LAST so any DDL with embedded DML (e.g. a procedure
    # body containing INSERT/UPDATE) classifies as the DDL type via
    # an earlier pattern. A pure DML script (registration data, seed
    # loads) reaches these patterns and classifies as DML.
    #
    # ``DELETE FROM`` only — bare ``DELETE`` would also match Teradata's
    # destructive ``DELETE DATABASE foo ALL`` (a teardown command, not a
    # deployment artefact). Requiring ``FROM`` keeps that out.
    #
    # ``UPDATE`` is permissive (no ``SET`` requirement) because Teradata's
    # ``UPDATE t FROM other o SET ...`` reorders the SET clause; the line-
    # start anchor + earlier UPDATE STATISTICS rule are enough discrimination.
    (re.compile(r"^\s*INSERT\s+INTO\b", _STMT_FLAGS), "DML"),
    (re.compile(r"^\s*UPDATE\b", _STMT_FLAGS), "DML"),
    (re.compile(r"^\s*DELETE\s+FROM\b", _STMT_FLAGS), "DML"),
    (re.compile(r"^\s*MERGE\s+INTO\b", _STMT_FLAGS), "DML"),
]


# ---------------------------------------------------------------
# Sub-typing helpers
# ---------------------------------------------------------------


_LANGUAGE_C_RE = re.compile(r"\bLANGUAGE\s+C\b", re.I)
_LANGUAGE_CPP_RE = re.compile(r"\bLANGUAGE\s+(?:CPP|C\+\+)\b", re.I)
_LANGUAGE_JAVA_RE = re.compile(r"\bLANGUAGE\s+JAVA\b", re.I)
_LANGUAGE_SQL_RE = re.compile(r"\bLANGUAGE\s+SQL\b", re.I)


def _refine_function_subtype(content: str) -> str:
    """Decide between FUNCTION_C and FUNCTION_SQL.

    LANGUAGE C explicit → FUNCTION_C.
    LANGUAGE SQL explicit OR no LANGUAGE clause → FUNCTION_SQL
    (Teradata defaults to SQL when the clause is absent).
    """
    if _LANGUAGE_C_RE.search(content):
        return "FUNCTION_C"
    return "FUNCTION_SQL"


def _refine_procedure_subtype(content: str) -> str:
    """Decide the Teradata procedure dialect.

    LANGUAGE JAVA explicit → PROCEDURE_JAVA.
    LANGUAGE CPP / C++ explicit → PROCEDURE_CPP.
    Anything else → PROCEDURE_SPL (Teradata's default for procedures
    when no LANGUAGE clause is present).
    """
    if _LANGUAGE_JAVA_RE.search(content):
        return "PROCEDURE_JAVA"
    if _LANGUAGE_CPP_RE.search(content):
        return "PROCEDURE_CPP"
    return "PROCEDURE_SPL"


# ---------------------------------------------------------------
# External-reference extraction
# ---------------------------------------------------------------


#: Match an EXTERNAL NAME clause and capture its body. The body is
#: a delimiter-separated string that we tokenise separately —
#: format varies across Teradata releases and this stays permissive.
_EXTERNAL_NAME_RE = re.compile(
    r"EXTERNAL\s+NAME\s+'([^']+)'",
    re.I | re.DOTALL,
)


#: C/Java external-name token forms we care about. Teradata accepts
#: more (CO!, CI!, F! etc.) but for v1 we only extract sources +
#: headers. Unrecognised tokens become a warning.
_EXTERNAL_TOKEN_RE = re.compile(
    r"(C[SHIO])\s*!\s*([^!]+?)\s*(?:!\s*([^!]+?))?(?=\s*(?:!|$))",
    re.I,
)


def extract_c_externals(external_body: str) -> List[str]:
    """Parse a Teradata C-UDF EXTERNAL NAME body.

    Recognised tokens:
        CS!alias!path        - C source file
        CH!alias!path        - C header file
        CI!alias!path        - C include (alternative to CH)
        CO!alias!path        - C object file
        CS!path              - short form (no alias)

    Returns the list of *paths* (the alias is metadata only). Order
    is preserved so the deployer can bundle in declared order.
    """
    paths: List[str] = []
    # Split on '!' but track every consecutive triple.
    parts = [p.strip() for p in external_body.split("!") if p.strip()]
    i = 0
    while i < len(parts):
        token = parts[i].upper()
        if token in ("CS", "CH", "CI", "CO"):
            # Two valid forms:
            #   <TOK>!alias!path     (3 tokens consumed)
            #   <TOK>!path           (2 tokens consumed)
            # We assume long-form first; if alias position looks
            # like a path (contains . or /) treat as short form.
            if i + 2 < len(parts):
                alias_or_path = parts[i + 1]
                next_path = parts[i + 2]
                # Heuristic: short form has the file in alias slot
                if "/" in alias_or_path or alias_or_path.endswith(
                    (".c", ".h", ".o", ".cpp")
                ):
                    paths.append(alias_or_path)
                    i += 2
                else:
                    paths.append(next_path)
                    i += 3
            elif i + 1 < len(parts):
                paths.append(parts[i + 1])
                i += 2
            else:
                # Lone token without a path — caller can warn
                i += 1
        else:
            i += 1
    return paths


def extract_jar_alias(external_body: str) -> Optional[str]:
    """Parse a Java-procedure EXTERNAL NAME body.

    Format: ``jar_alias:com.x.Foo.bar``
    Returns the alias before the first colon, or None if the body
    doesn't contain one.
    """
    if ":" not in external_body:
        return None
    return external_body.split(":", 1)[0].strip()


#: Match the path portion of CALL SQLJ.INSTALL_JAR/REPLACE_JAR.
#: Captures the full quoted argument so we can extract the path
#: from inside the ``CJ!path`` form.
_SQLJ_INSTALL_RE = re.compile(
    r"CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(\s*"
    r"'([^']+)'",
    re.I,
)


def extract_sqlj_jar_paths(content: str) -> List[str]:
    """Extract ``'CJ!path'`` filesystem paths from SQLJ install
    scripts.

    For each ``CALL SQLJ.INSTALL_JAR('CJ!path/to/X.jar', ...)``
    statement, returns the path portion (everything after the
    leading ``CJ!``). Multiple INSTALL_JAR calls in one file all
    contribute. Order is preserved.
    """
    paths: List[str] = []
    for m in _SQLJ_INSTALL_RE.finditer(content):
        arg = m.group(1)
        # The CJ!-prefixed form is the documented one. Anything
        # else (e.g. a server-side path with no prefix) is left
        # alone — caller decides whether to warn.
        if arg.upper().startswith("CJ!"):
            paths.append(arg[3:])
        else:
            paths.append(arg)
    return paths


def extract_externals(content: str, type_hint: str) -> List[str]:
    """Extract external references for FUNCTION_C / PROCEDURE_CPP / PROCEDURE_JAVA / JAR.

    Returns:
        For FUNCTION_C / PROCEDURE_CPP: list of .c/.cpp/.h paths from EXTERNAL NAME.
        For PROCEDURE_JAVA: single-element list with the JAR alias
                            from EXTERNAL NAME.
        For JAR (SQLJ install script): list of binary paths from
                            CALL SQLJ.INSTALL_JAR's CJ!path argument.
        Empty list otherwise.
    """
    if type_hint == "JAR":
        return extract_sqlj_jar_paths(content)

    m = _EXTERNAL_NAME_RE.search(content)
    if m is None:
        return []

    body = m.group(1)
    if type_hint in ("FUNCTION_C", "PROCEDURE_CPP"):
        return extract_c_externals(body)
    if type_hint == "PROCEDURE_JAVA":
        alias = extract_jar_alias(body)
        return [alias] if alias else []
    return []


# ---------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------


@dataclass
class ClassificationResult:
    """Output of ``classify()``.

    Attributes:
        type:           Detected type (or sub-type). None if no
                        pattern matched.
        confidence:     "HIGH" / "MEDIUM" / "LOW".
        evidence:       Short strings describing what triggered
                        the classification (e.g. "matched pattern
                        CREATE FUNCTION at line 3").
        related_files:  External references for C UDFs (paths) or
                        Java procedures (JAR alias).
        warnings:       Diagnostic messages — filename mismatches,
                        unrecognised externals, etc.
    """

    type: Optional[str] = None
    confidence: str = "LOW"
    evidence: List[str] = field(default_factory=list)
    related_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def base_type(self) -> Optional[str]:
        """Map a sub-type to its base. Pass-through for plain types."""
        return base_type(self.type)


# ---------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------


def classify(path: str, content: str) -> ClassificationResult:
    """
    Classify a SQL/DDL file by content.

    Args:
        path:    File path. Used for filename hints; pass empty
                 string if no path is meaningful.
        content: File contents as text.

    Returns:
        A ``ClassificationResult`` recording the detected type, the
        confidence in that detection, evidence, external references,
        and any warnings (e.g. filename mismatch).
    """
    result = ClassificationResult()

    # Stage 1: regex match for the base type
    base = None
    for pattern, type_ in _CLASSIFY_PATTERNS:
        m = pattern.search(content)
        if m is not None:
            base = type_
            line_num = content.count("\n", 0, m.start()) + 1
            snippet = content[m.start() : m.start() + 60].replace("\n", " ")
            result.evidence.append(
                f"matched pattern for {type_} at line {line_num}: {snippet!r}"
            )
            break

    if base is None:
        # No pattern matched — leave result.type as None
        return result

    # Stage 2: refine FUNCTION/PROCEDURE into their dialect sub-types
    if base == "FUNCTION":
        result.type = _refine_function_subtype(content)
        if result.type == "FUNCTION_C":
            result.evidence.append("LANGUAGE C → FUNCTION_C")
        else:
            result.evidence.append("no LANGUAGE C → FUNCTION_SQL")
    elif base == "PROCEDURE":
        result.type = _refine_procedure_subtype(content)
        if result.type == "PROCEDURE_JAVA":
            result.evidence.append("LANGUAGE JAVA → PROCEDURE_JAVA")
        elif result.type == "PROCEDURE_CPP":
            result.evidence.append("LANGUAGE CPP → PROCEDURE_CPP")
        else:
            result.evidence.append("no LANGUAGE JAVA/CPP → PROCEDURE_SPL")
    else:
        result.type = base

    # Stage 3: extract external references where relevant
    if result.type in ("FUNCTION_C", "PROCEDURE_CPP", "PROCEDURE_JAVA", "JAR"):
        result.related_files = extract_externals(content, result.type)
        if result.type == "FUNCTION_C" and not result.related_files:
            result.warnings.append(
                "FUNCTION_C detected but no .c/.h files referenced "
                "in EXTERNAL NAME clause — verify the dependency."
            )
        if result.type == "PROCEDURE_CPP" and not result.related_files:
            result.warnings.append(
                "PROCEDURE_CPP detected but no .c/.cpp/.h files referenced "
                "in EXTERNAL NAME clause — verify the dependency."
            )
        if result.type == "PROCEDURE_JAVA" and not result.related_files:
            result.warnings.append(
                "PROCEDURE_JAVA detected but no JAR alias resolved "
                "from EXTERNAL NAME clause."
            )
        if result.type == "JAR" and not result.related_files:
            result.warnings.append(
                "JAR install script detected but no CJ!path "
                "binary references resolved — the deployer won't "
                "have any JAR file to install."
            )

    # Stage 4: filename consistency check
    if path:
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()
        expected = EXTENSION_TO_EXPECTED.get(ext)
        if expected is not None and result.type not in expected:
            # Filename suggests one type, content matches another.
            # Content always wins (filename is unreliable in legacy
            # codebases) but flag it so the user can rename.
            result.warnings.append(
                f"Filename mismatch: '{filename}' (extension '{ext}' "
                f"suggests {sorted(expected)}) but content matches "
                f"{result.type}. Content wins; consider renaming the "
                f"source file."
            )

    # Stage 5: confidence labelling
    result.confidence = _score_confidence(
        type_=result.type,
        evidence=result.evidence,
        warnings=result.warnings,
        path=path,
    )

    return result


def _score_confidence(
    *,
    type_: str,
    evidence: List[str],
    warnings: List[str],
    path: str,
) -> str:
    """
    Heuristic confidence rating.

    HIGH:
      - Sub-type confirmed by an explicit LANGUAGE clause
      - OR plain type matched and filename is consistent
    MEDIUM:
      - Plain type matched, filename was generic (.sql/.ddl/.dml)
      - OR sub-type defaulted (no explicit LANGUAGE clause)
    LOW:
      - Filename mismatch present
      - OR matched only by a weak pattern (GRANT/REVOKE keyword anywhere)
    """
    has_filename_mismatch = any("Filename mismatch" in w for w in warnings)
    if has_filename_mismatch:
        return "LOW"

    # Weak matches — single-keyword DCL detection is the most prone
    # to false positives (GRANT can appear in comments or arguments).
    if type_ in ("GRANT", "REVOKE"):
        return "MEDIUM"

    # Sub-types are HIGH when an explicit LANGUAGE clause confirmed
    # them, MEDIUM when defaulted.
    explicit_subtype = any(
        "→ FUNCTION_C" in e or "→ PROCEDURE_JAVA" in e or "→ PROCEDURE_CPP" in e
        for e in evidence
    )
    defaulted_subtype = any(
        "no LANGUAGE C" in e or "no LANGUAGE JAVA/CPP" in e for e in evidence
    )
    if explicit_subtype:
        return "HIGH"
    if defaulted_subtype:
        return "MEDIUM"

    # Plain type with consistent filename → HIGH.
    if path:
        ext = os.path.splitext(path)[1].lower()
        expected = EXTENSION_TO_EXPECTED.get(ext)
        if expected is None:
            # Generic extension — content match is HIGH if the
            # pattern was specific (CREATE TABLE, etc.) but with no
            # filename evidence we step it down to MEDIUM.
            return "MEDIUM"
        if type_ in expected:
            return "HIGH"

    return "MEDIUM"
