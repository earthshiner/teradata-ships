"""
token_engine.py — Token substitution engine.

Reads token values from a .properties file and substitutes
{{TOKENNAME}} placeholders in DDL, DCL, and DML files.

Token format:
    Source files use {{TOKENNAME}} (doubled curly braces).
    Properties files use NAME=VALUE (one per line).
    Lines starting with '#' are comments.

Token substitution happens at BUILD time — the packaged files
contain resolved, environment-specific values. This means the
DBA deploys without needing any token knowledge.

Validation:
    - All tokens found in source files must be defined in properties.
    - Undefined tokens cause a build failure (not a silent pass-through).
    - Unused tokens in properties produce a warning.
"""

import logging
import os
import re
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# -- Regex for {{TOKENNAME}} --
# Captures the token name between doubled curly braces.
# Allows alphanumeric, underscores, and hyphens in names.
_TOKEN_RE = re.compile(r'\{\{([A-Za-z_][A-Za-z0-9_-]*)\}\}')

# Characters that should never appear in a resolved token value
# (Teradata identifiers: alphanumeric, underscores, and {{}} for
# unresolved internal references only).
_INVALID_VALUE_CHARS = re.compile(r'[=;()\[\]{}]')


def _validate_property_values(
    tokens: Dict[str, str],
    properties_path: str = "",
    phase: str = "raw",
) -> List[str]:
    """
    Validate property values for common errors.

    Checks performed:
      - Value contains '=' — almost certainly two properties
        lines merged onto one (e.g. VIW_DATABASE=STD_DATABASE=...).
      - Value contains characters invalid in Teradata identifiers
        (semicolons, parentheses, brackets).
      - Key contains lowercase — convention is UPPERCASE_WITH_UNDERSCORES.

    Args:
        tokens:          Dictionary of token_name → value.
        properties_path: Path to properties file (for error messages).
        phase:           'raw' (before resolution) or 'resolved'.

    Returns:
        List of error messages. Empty if all valid.
    """
    errors = []

    for name, value in tokens.items():
        # -- Merged lines: value contains '=' --
        # A valid token value is a database name, object name, or
        # environment prefix — none of which contain '='. If the
        # value has '=', someone likely merged two lines:
        #   VIW_DATABASE=STD_DATABASE={{ENV_PREFIX}}_SHIPS_VIW
        if '=' in value:
            # Extract the suspicious prefix before the '='
            prefix = value.split('=', 1)[0].strip()
            errors.append(
                f"Token '{name}': value contains '=' — likely "
                f"two properties lines merged. "
                f"Found '{prefix}=' in value '{value}'. "
                f"Check {properties_path} for a missing line break."
            )

        # -- Invalid characters in resolved values --
        if phase == "resolved":
            invalid = _INVALID_VALUE_CHARS.findall(value)
            if invalid:
                chars = ", ".join(repr(c) for c in set(invalid))
                errors.append(
                    f"Token '{name}': resolved value contains "
                    f"invalid characters ({chars}): '{value}'"
                )

            # Unresolved {{TOKEN}} references after resolution
            remaining = _TOKEN_RE.findall(value)
            if remaining:
                refs = ", ".join(f"{{{{{r}}}}}" for r in remaining)
                errors.append(
                    f"Token '{name}': value still contains "
                    f"unresolved references after resolution: {refs}"
                )

    # -- Key naming convention (warning, not error) --
    for name in tokens:
        if name != name.upper():
            logger.warning(
                "Token '%s': convention is UPPERCASE_WITH_UNDERSCORES.",
                name,
            )

    return errors


def read_properties(properties_path: str) -> Dict[str, str]:
    """
    Read a .properties file into a token dictionary.

    Format:
        # Comment lines start with '#'
        TOKEN_NAME=value
        ANOTHER_TOKEN=value with spaces

    Leading/trailing whitespace is stripped from both names and
    values. Empty lines and comment lines are ignored. Duplicate
    keys use the last-defined value (with a warning).

    Args:
        properties_path: Path to the .properties file.

    Returns:
        Dictionary of token_name → value.

    Raises:
        FileNotFoundError: If the properties file does not exist.
    """
    if not os.path.exists(properties_path):
        raise FileNotFoundError(
            f"Properties file not found: {properties_path}"
        )

    tokens = {}
    with open(properties_path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith('#'):
                continue

            # Split on first '=' only (values may contain '=')
            if '=' not in stripped:
                logger.warning(
                    "Properties line %d: no '=' found, skipping: %s",
                    lineno, stripped
                )
                continue

            name, value = stripped.split('=', 1)
            name = name.strip()
            value = value.strip()

            if not name:
                logger.warning(
                    "Properties line %d: empty token name, skipping.",
                    lineno
                )
                continue

            if name in tokens:
                logger.warning(
                    "Properties line %d: duplicate token '%s' — "
                    "overriding previous value.",
                    lineno, name
                )

            tokens[name] = value

    logger.info(
        "Read %d tokens from %s",
        len(tokens), properties_path
    )

    # Validate raw values (before resolution) — catches merged lines
    raw_errors = _validate_property_values(
        tokens, properties_path, phase="raw",
    )
    if raw_errors:
        error_list = "\n  ".join(raw_errors)
        raise ValueError(
            f"Properties file has {len(raw_errors)} error(s):\n"
            f"  {error_list}\n\n"
            f"File: {properties_path}"
        )

    # Resolve internal references: {{TOKEN}} within values
    tokens = _resolve_internal_references(tokens)

    # Validate resolved values — catches invalid characters
    # and unresolved references
    resolved_errors = _validate_property_values(
        tokens, properties_path, phase="resolved",
    )
    if resolved_errors:
        error_list = "\n  ".join(resolved_errors)
        raise ValueError(
            f"Properties file has {len(resolved_errors)} error(s) "
            f"after token resolution:\n"
            f"  {error_list}\n\n"
            f"File: {properties_path}"
        )

    return tokens


def _resolve_internal_references(
    tokens: Dict[str, str],
    max_passes: int = 10,
) -> Dict[str, str]:
    """
    Resolve {{TOKEN}} references within property values.

    Allows properties to reference other properties:
        SHIPS_ENV=DEV
        SEM_DATABASE={{SHIPS_ENV}}01_OMR_SEM  → DEV01_OMR_SEM

    Iterates until no more substitutions occur or max_passes is
    reached (circular reference protection).

    Args:
        tokens:     Dictionary of token_name → raw value.
        max_passes: Maximum resolution iterations.

    Returns:
        Dictionary with all internal references resolved.

    Raises:
        ValueError: If circular references prevent resolution.
    """
    resolved = dict(tokens)

    for pass_num in range(max_passes):
        substitutions = 0

        for name, value in resolved.items():
            if '{{' not in value:
                continue

            def replacer(match):
                ref_name = match.group(1)
                if ref_name in resolved and ref_name != name:
                    return resolved[ref_name]
                # Leave unresolved references as-is
                return match.group(0)

            new_value = _TOKEN_RE.sub(replacer, value)
            if new_value != value:
                resolved[name] = new_value
                substitutions += 1

        if substitutions == 0:
            break
    else:
        # Reached max_passes — check for unresolved references
        unresolved = []
        for name, value in resolved.items():
            remaining = _TOKEN_RE.findall(value)
            if remaining:
                unresolved.append(f"  {name}: references {{{{{', '.join(remaining)}}}}}")
        if unresolved:
            raise ValueError(
                f"Circular or unresolvable token references "
                f"after {max_passes} passes:\n" + "\n".join(unresolved)
            )

    # Log any resolved references
    for name in tokens:
        if tokens[name] != resolved[name]:
            logger.debug(
                "Resolved %s: %s → %s",
                name, tokens[name], resolved[name],
            )

    return resolved


def scan_tokens_in_file(file_path: str) -> Set[str]:
    """
    Scan a file and return all {{TOKENNAME}} references found.

    Args:
        file_path: Path to the file to scan.

    Returns:
        Set of token names (without the {{ }} delimiters).
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return set(_TOKEN_RE.findall(content))


def scan_tokens_in_directory(directory: str) -> Dict[str, Set[str]]:
    """
    Scan all payload files in a directory tree for token references.

    Skips files starting with '_' or '.' (scaffolding samples,
    hidden files) and non-text extensions.

    Args:
        directory: Root directory to scan.

    Returns:
        Dictionary of file_path → set of token names found.
    """
    results = {}
    for root, dirs, files in os.walk(directory):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for filename in files:
            # Skip hidden files, underscore-prefixed files, sample files
            if filename.startswith('.') or filename.startswith('_'):
                continue
            if filename.endswith('.sample'):
                continue
            file_path = os.path.join(root, filename)
            try:
                tokens = scan_tokens_in_file(file_path)
                if tokens:
                    results[file_path] = tokens
            except (UnicodeDecodeError, PermissionError):
                # Skip binary files or unreadable files
                pass
    return results


def validate_tokens(
    token_values: Dict[str, str],
    token_usage: Dict[str, Set[str]],
) -> Tuple[List[str], List[str]]:
    """
    Validate that all referenced tokens are defined, and flag unused ones.

    Args:
        token_values: Dictionary of defined token_name → value.
        token_usage:  Dictionary of file_path → set of token names used.

    Returns:
        Tuple of (errors, warnings).
        Errors: tokens referenced but not defined.
        Warnings: tokens defined but never referenced.
    """
    # Collect all referenced tokens across all files
    all_referenced = set()
    for tokens in token_usage.values():
        all_referenced.update(tokens)

    defined = set(token_values.keys())

    # Tokens used but not defined — build error
    undefined = all_referenced - defined
    errors = [
        f"Token '{{{{{t}}}}}' is referenced but not defined in properties."
        for t in sorted(undefined)
    ]

    # Add file locations for undefined tokens
    for token in sorted(undefined):
        files = [
            f for f, tokens in token_usage.items()
            if token in tokens
        ]
        for fpath in files:
            errors.append(f"  → used in: {fpath}")

    # Tokens defined but never used — warning
    # Exclude reserved metadata properties (not deployment tokens)
    _RESERVED_PROPERTIES = {'SHIPS_ENV', 'SHIPS_PROJECT', 'ENV_PREFIX'}
    unused = defined - all_referenced - _RESERVED_PROPERTIES
    warnings = [
        f"Token '{{{{{t}}}}}' is defined in properties but never referenced."
        for t in sorted(unused)
    ]

    return (errors, warnings)


def substitute_tokens(
    content: str,
    token_values: Dict[str, str],
) -> Tuple[str, int]:
    """
    Replace all {{TOKENNAME}} occurrences in a string with their values.

    Args:
        content:      The file content string.
        token_values: Dictionary of token_name → value.

    Returns:
        Tuple of (substituted_content, substitution_count).

    Raises:
        KeyError: If a token is found that has no defined value.
                  (Call validate_tokens first to prevent this.)
    """
    count = 0

    def _replacer(match):
        nonlocal count
        token_name = match.group(1)
        if token_name not in token_values:
            raise KeyError(
                f"Undefined token '{{{{{token_name}}}}}' — "
                f"not found in properties file."
            )
        count += 1
        return token_values[token_name]

    result = _TOKEN_RE.sub(_replacer, content)
    return (result, count)


def substitute_file(
    source_path: str,
    dest_path: str,
    token_values: Dict[str, str],
) -> int:
    """
    Read a source file, substitute tokens, write to destination.

    Creates the destination directory if it does not exist.

    Args:
        source_path:  Path to the source file with {{TOKENS}}.
        dest_path:    Path to write the resolved file.
        token_values: Dictionary of token_name → value.

    Returns:
        Count of substitutions made.
    """
    with open(source_path, 'r', encoding='utf-8') as f:
        content = f.read()

    resolved, count = substitute_tokens(content, token_values)

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(resolved)

    if count > 0:
        logger.debug(
            "Substituted %d tokens in %s → %s",
            count, source_path, dest_path
        )

    return count


# ---------------------------------------------------------------
# Token map — literal database names → {{TOKEN}} placeholders
# ---------------------------------------------------------------

# System databases that should never be tokenised
_SYSTEM_DBS = {
    'DBC', 'SYSUDTLIB', 'SYSLIB', 'SYSJDBC', 'SYSBAR',
    'SYSTEMFE', 'SYSSPATIAL', 'TD_SYSFNLIB',
    'TD_SYSXML', 'TDSTATS', 'TDWM', 'TD_SYSGPL',
    'ALL', 'DEFAULT', 'PUBLIC', 'EXTUSER',
    'SQLJ', 'SYSUIF', 'DBCMNGR',
}


def derive_token_name(db_name: str, env_prefix: str) -> str:
    """
    Derive a token name by stripping the environment prefix.

    Removes the prefix and any trailing underscore or separator,
    leaving the environment-independent suffix as the token name.

    Examples:
        derive_token_name('A_D01_OMR_STD', 'A_D01')  → 'OMR_STD'
        derive_token_name('P_OMR_SEM', 'P')           → 'OMR_SEM'
        derive_token_name('DEV01_CORE', 'DEV01')       → 'CORE'

    Args:
        db_name:     The literal database name.
        env_prefix:  The environment prefix to strip.

    Returns:
        The derived token name (suffix after prefix).
        If the prefix doesn't match, returns the full db_name.
    """
    # Case-insensitive prefix match
    if db_name.upper().startswith(env_prefix.upper()):
        suffix = db_name[len(env_prefix):]
        # Strip leading underscore/separator
        suffix = suffix.lstrip('_')
        if suffix:
            return suffix
    # Prefix didn't match or nothing remained — use full name
    return db_name


def generate_token_map(
    db_names: Dict[str, List[str]],
    env_prefix: str = None,
) -> Dict[str, str]:
    """
    Build a literal → {{TOKEN}} mapping from discovered database names.

    Filters out system databases. If env_prefix is provided, strips
    it to derive shorter token names. If not, uses the full database
    name as the token name.

    Args:
        db_names:    Dict of database_name → list of files referencing it.
                     (As produced by ingest's token candidate detection.)
        env_prefix:  Optional environment prefix to strip (e.g. 'A_D01').
                     If None, the full database name becomes the token.

    Returns:
        Dict of literal_name → '{{TOKEN_NAME}}'.
        e.g. {'A_D01_OMR_STD': '{{OMR_STD}}'} (with prefix)
        or   {'CORE_STD': '{{CORE_STD}}'}      (without prefix)
    """
    token_map = {}

    for db_name in sorted(db_names.keys()):
        # Skip system databases
        if db_name.upper() in _SYSTEM_DBS:
            continue

        if env_prefix:
            token_name = derive_token_name(db_name, env_prefix)
        else:
            token_name = db_name

        token_map[db_name] = "{{" + token_name + "}}"

    return token_map


def write_token_map(
    path: str,
    token_map: Dict[str, str],
    db_names: Dict[str, List[str]],
    env_prefix: str,
) -> None:
    """
    Write a token_map.conf file.

    Includes reference counts and file lists as comments so
    the developer (or agent) can review the mapping.

    Args:
        path:        Output file path.
        token_map:   Dict of literal_name → '{{TOKEN_NAME}}'.
        db_names:    Dict of database_name → list of files referencing it.
        env_prefix:  The environment prefix used for derivation.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, 'w', encoding='utf-8') as f:
        f.write("# token_map.conf — Literal database name → {{TOKEN}} mapping\n")
        f.write("#\n")
        f.write(f"# Generated by SHIPS harvest with --env-prefix {env_prefix}\n")
        f.write("#\n")
        f.write("# Review and edit token names if needed, then re-harvest with:\n")
        f.write(f"#   python -m td_release_packager harvest \\\n")
        f.write(f"#       --source <source> --project <project> \\\n")
        f.write(f"#       --token-map {path}\n")
        f.write("#\n")
        f.write("# Format: LITERAL_DB_NAME={{TOKEN_NAME}}\n")
        f.write("#\n\n")

        for literal, token in sorted(token_map.items()):
            files = db_names.get(literal, [])
            unique_files = sorted(set(files))
            ref_count = len(files)
            file_count = len(unique_files)
            f.write(f"# {ref_count} references across {file_count} files\n")
            f.write(f"{literal}={token}\n\n")

    logger.info(
        "Token map written: %s (%d mappings)",
        path, len(token_map)
    )


def read_token_map(path: str) -> Dict[str, str]:
    """
    Read a token_map.conf file into a literal → {{TOKEN}} dict.

    Format:
        # Comment lines start with '#'
        LITERAL_DB_NAME={{TOKEN_NAME}}

    This dict can be passed directly to ingest_directory's
    apply_tokens parameter.

    Args:
        path: Path to the token_map.conf file.

    Returns:
        Dict of literal_name → '{{TOKEN_NAME}}'.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Token map file not found: {path}"
        )

    token_map = {}

    with open(path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith('#'):
                continue

            if '=' not in stripped:
                logger.warning(
                    "token_map.conf line %d: no '=' found, skipping: %s",
                    lineno, stripped
                )
                continue

            literal, token = stripped.split('=', 1)
            literal = literal.strip()
            token = token.strip()

            if not literal:
                continue

            # Validate token format
            if not (token.startswith('{{') and token.endswith('}}')):
                logger.warning(
                    "token_map.conf line %d: value '%s' is not a "
                    "{{TOKEN}} placeholder — skipping.",
                    lineno, token
                )
                continue

            token_map[literal] = token

    logger.info(
        "Token map loaded: %s (%d mappings)",
        path, len(token_map)
    )

    return token_map
