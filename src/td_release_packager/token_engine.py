"""
token_engine.py — Token substitution engine.

Reads token values from a .conf file and substitutes
{{TOKENNAME}} placeholders in DDL, DCL, and DML files.

Token format:
    Source files use {{TOKENNAME}} (doubled curly braces).
    Config files use NAME=VALUE (one per line).
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
_TOKEN_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_-]*)\}\}")

# Characters that should never appear in a resolved token value.
# Curly braces would indicate an unresolved {{TOKEN}} reference;
# square brackets are SQL Server bracketed-identifier syntax that
# does not belong in Teradata DDL.
# Parentheses ARE permitted because they appear in legitimate
# SQL type expressions used as token values (e.g. TIMESTAMP(6),
# TIME(6), CURRENT_TIMESTAMP(6)).
_INVALID_VALUE_CHARS = re.compile(r"[\[\]{}]")


def _validate_property_values(
    tokens: Dict[str, str],
    env_config_path: str = "",
    phase: str = "raw",
) -> List[str]:
    """
    Validate property values for common errors.

    Checks performed:
      - Value contains '=' with an UPPERCASE prefix — almost
        certainly two properties lines merged onto one
        (e.g. VIW_DATABASE=STD_DATABASE=...). Values with
        lowercase prefixes (e.g. connection strings) are allowed.
      - Value contains characters invalid in Teradata identifiers
        (parentheses, brackets, braces).
      - Key contains lowercase — convention is UPPERCASE_WITH_UNDERSCORES.

    Args:
        tokens:          Dictionary of token_name → value.
        env_config_path: Path to properties file (for error messages).
        phase:           'raw' (before resolution) or 'resolved'.

    Returns:
        List of error messages. Empty if all valid.
    """
    errors = []

    for name, value in tokens.items():
        # -- Merged lines: value contains '=' --
        # A genuine merged line looks like:
        #   VIW_DATABASE=STD_DATABASE={{ENV_PREFIX}}_SHIPS_VIW
        # where the prefix before '=' in the value is another
        # UPPERCASE_TOKEN_NAME. Connection strings and other
        # legitimate values (e.g. host=myserver;port=1025) have
        # lowercase prefixes and should NOT be flagged.
        if "=" in value:
            prefix = value.split("=", 1)[0].strip()
            # Only flag if the prefix looks like a token name:
            # all uppercase, underscores, digits — e.g. STD_DATABASE
            if prefix and re.fullmatch(r"[A-Z][A-Z0-9_]*", prefix):
                errors.append(
                    f"Token '{name}': value contains '=' — likely "
                    f"two properties lines merged. "
                    f"Found '{prefix}=' in value '{value}'. "
                    f"Check {env_config_path} for a missing line break."
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


def _resolve_vault_value(raw_ref: str, token_name: str) -> str:
    """Resolve a ``vault:path#key`` secret reference (GAP-011).

    Uses the ``hvac`` package when available; falls back to direct HTTP
    calls to the Vault KV v2 API via ``urllib.request``.

    Args:
        raw_ref:    The raw value string, e.g. ``vault:secret/data/ships/prd#password``.
        token_name: Token name for error messages.

    Returns:
        Resolved secret value string.

    Raises:
        ValueError: When the secret cannot be resolved.
    """
    vault_addr = os.environ.get("VAULT_ADDR", "").strip()
    vault_token = os.environ.get("VAULT_TOKEN", "").strip()

    if not vault_addr:
        raise ValueError(
            f"token '{token_name}': vault reference requires VAULT_ADDR env var"
        )
    if not vault_token:
        raise ValueError(
            f"token '{token_name}': vault reference requires VAULT_TOKEN env var"
        )

    # Parse: vault:secret/data/ships/prd#field
    ref = raw_ref[len("vault:"):]
    if "#" not in ref:
        raise ValueError(
            f"token '{token_name}': vault reference must be 'vault:path#key', got '{raw_ref}'"
        )
    path, field = ref.rsplit("#", 1)

    try:
        import hvac

        client = hvac.Client(url=vault_addr, token=vault_token)
        secret = client.secrets.kv.v2.read_secret_version(path=path)
        data = secret["data"]["data"]
        if field not in data:
            raise ValueError(
                f"token '{token_name}': field '{field}' not found in vault path '{path}'"
            )
        return str(data[field])
    except ImportError:
        pass  # Fall back to urllib

    import json
    import urllib.request

    # Vault KV v2: GET /v1/<path>
    url = f"{vault_addr.rstrip('/')}/v1/{path}"
    req = urllib.request.Request(
        url,
        headers={"X-Vault-Token": vault_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise ValueError(
            f"token '{token_name}': vault request failed for path '{path}': {exc}"
        ) from exc

    data = body.get("data", {}).get("data", {})
    if field not in data:
        raise ValueError(
            f"token '{token_name}': field '{field}' not found in vault path '{path}'"
        )
    return str(data[field])


def _resolve_secret_value(value: str, token_name: str) -> str:
    """Resolve ``$env:`` and ``vault:`` prefixes in a token map value (GAP-011).

    ``$env:VAR_NAME``  → ``os.environ['VAR_NAME']`` (fails if not set)
    ``vault:path#key`` → Vault KV v2 secret field (fails if unreachable)
    Plain values       → returned unchanged.

    Args:
        value:      Raw value from the token map .conf file.
        token_name: Token name (used in error messages only).

    Returns:
        Resolved string value.

    Raises:
        ValueError: When a reference cannot be resolved.
    """
    if value.startswith("$env:"):
        var_name = value[len("$env:"):]
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ValueError(
                f"token '{token_name}': env var '{var_name}' is not set"
            )
        logger.debug("token_engine: '%s' resolved from env var '%s'.", token_name, var_name)
        return resolved

    if value.startswith("vault:"):
        return _resolve_vault_value(value, token_name)

    return value


def read_env_config(env_config_path: str) -> Dict[str, str]:
    """
    Read a .conf file into a token dictionary.

    Format:
        # Comment lines start with '#'
        TOKEN_NAME=value
        ANOTHER_TOKEN=value with spaces

    Leading/trailing whitespace is stripped from both names and
    values. Empty lines and comment lines are ignored. Duplicate
    keys use the last-defined value (with a warning).

    Args:
        env_config_path: Path to the .conf file.

    Returns:
        Dictionary of token_name → value.

    Raises:
        FileNotFoundError: If the properties file does not exist.
    """
    if not os.path.exists(env_config_path):
        raise FileNotFoundError(f"Config file not found: {env_config_path}")

    tokens = {}
    with open(env_config_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            # Split on first '=' only (values may contain '=')
            if "=" not in stripped:
                logger.warning(
                    "Properties line %d: no '=' found, skipping: %s", lineno, stripped
                )
                continue

            name, value = stripped.split("=", 1)
            name = name.strip()
            value = value.strip()

            if not name:
                logger.warning(
                    "Properties line %d: empty token name, skipping.", lineno
                )
                continue

            if name in tokens:
                logger.warning(
                    "Properties line %d: duplicate token '%s' — "
                    "overriding previous value.",
                    lineno,
                    name,
                )

            tokens[name] = _resolve_secret_value(value, name)

    logger.info("Read %d tokens from %s", len(tokens), env_config_path)

    # Validate raw values (before resolution) — catches merged lines
    raw_errors = _validate_property_values(
        tokens,
        env_config_path,
        phase="raw",
    )
    if raw_errors:
        error_list = "\n  ".join(raw_errors)
        raise ValueError(
            f"Config file has {len(raw_errors)} error(s):\n"
            f"  {error_list}\n\n"
            f"File: {env_config_path}"
        )

    # Resolve internal references: {{TOKEN}} within values
    tokens = _resolve_internal_references(tokens)

    # Validate resolved values — catches invalid characters
    # and unresolved references
    resolved_errors = _validate_property_values(
        tokens,
        env_config_path,
        phase="resolved",
    )
    if resolved_errors:
        error_list = "\n  ".join(resolved_errors)
        raise ValueError(
            f"Config file has {len(resolved_errors)} error(s) "
            f"after token resolution:\n"
            f"  {error_list}\n\n"
            f"File: {env_config_path}"
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
            if "{{" not in value:
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
                unresolved.append(
                    f"  {name}: references {{{{{', '.join(remaining)}}}}}"
                )
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
                name,
                tokens[name],
                resolved[name],
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
    with open(file_path, "r", encoding="utf-8") as f:
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
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for filename in files:
            # Skip hidden files, underscore-prefixed files, sample files
            if filename.startswith(".") or filename.startswith("_"):
                continue
            if filename.endswith(".sample"):
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


# ---------------------------------------------------------------
# Malformed token detection
# ---------------------------------------------------------------
#
# A well-formed token matches _TOKEN_RE:
#   {{[A-Za-z_][A-Za-z0-9_-]*}}
#
# Anything else with double braces is malformed and will silently
# pass through token substitution unsubstituted, ending up in the
# deployed SQL where the Teradata driver rejects it as an
# "Unrecognized escape syntax" error. Common malformed forms:
#
#   {{ DBC_DATABASE }}       — whitespace inside braces
#   {{DBC_DATABASE\n}}       — newline inside (line wrapping)
#   {{{{DBC_DATABASE}}_X}}   — substring-replace ran twice over an
#                               already-tokenised file
#   {{DBC_DATABASE           — missing closing braces
#   DBC_DATABASE}}           — missing opening braces
#
# These need to be caught BEFORE packaging so the developer can fix
# the source file rather than discover the corruption at deploy time.


def find_malformed_tokens(content: str) -> List[Dict]:
    """
    Find malformed ``{{...}}`` markers in a piece of content.

    Strategy: replace every well-formed token with a same-length run
    of underscores so positions are preserved, then any remaining
    ``{{`` or ``}}`` pair in the result must be malformed (orphan
    braces or a brace-pair with invalid contents). Each finding
    is reported with line, column, and the surrounding line so the
    DBA can locate and fix it without grep.

    Args:
        content: File content to inspect.

    Returns:
        List of issue dicts with keys:
            line          — 1-based line number
            column        — 1-based column on that line
            marker        — the offending '{{' or '}}'
            line_content  — full text of the line, for context
    """

    # Mask out well-formed tokens — same length so positions in
    # the original content are still recoverable.
    def _mask(m):
        return "_" * len(m.group(0))

    stripped = _TOKEN_RE.sub(_mask, content)

    issues = []
    for m in re.finditer(r"\{\{|\}\}", stripped):
        pos = m.start()
        # 1-based line/col derived from the ORIGINAL content
        line_num = content.count("\n", 0, pos) + 1
        line_start = content.rfind("\n", 0, pos) + 1
        col = pos - line_start + 1
        line_end = content.find("\n", pos)
        if line_end == -1:
            line_end = len(content)
        line_content = content[line_start:line_end].rstrip("\r")
        issues.append(
            {
                "line": line_num,
                "column": col,
                "marker": m.group(0),
                "line_content": line_content,
            }
        )

    return issues


def scan_malformed_tokens_in_directory(
    directory: str,
) -> Dict[str, List[Dict]]:
    """
    Scan a directory tree for files containing malformed tokens.

    Mirrors :func:`scan_tokens_in_directory` — same skip rules for
    hidden/underscore-prefixed/sample files. Used by the build flow
    as a hard-fail check before packaging.

    Args:
        directory: Root directory to scan.

    Returns:
        Dictionary of ``file_path → list of issue dicts``. Files
        with no malformed tokens are omitted, so an empty result
        means the tree is clean.
    """
    results: Dict[str, List[Dict]] = {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for filename in files:
            if filename.startswith(".") or filename.startswith("_"):
                continue
            if filename.endswith(".sample"):
                continue
            file_path = os.path.join(root, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, PermissionError):
                continue
            issues = find_malformed_tokens(content)
            if issues:
                results[file_path] = issues
    return results


def format_malformed_tokens_report(
    findings: Dict[str, List[Dict]],
) -> str:
    """
    Render a malformed-tokens report as a printable string.

    The report leads with a short summary and per-file groupings
    so the DBA can fix the source files without further searching.
    A common-cause hint is appended because by far the most likely
    explanation is a re-run of ``ingest --token-map`` against an
    already-tokenised file (substring-replace footgun: the
    harvester sees ``DBC`` inside an existing ``{{DBC_DATABASE}}``
    token and re-substitutes, producing ``{{{{DBC_DATABASE}}...``).

    Args:
        findings: Map of file_path → list of issue dicts as returned
                  by :func:`scan_malformed_tokens_in_directory`.

    Returns:
        Multi-line string, ready to print or include in an exception.
    """
    if not findings:
        return ""

    file_count = len(findings)
    issue_count = sum(len(v) for v in findings.values())

    lines = [
        "=" * 64,
        "  Malformed tokens detected",
        "=" * 64,
        f"  Found {issue_count} malformed token marker(s) in {file_count} file(s).",
        "",
        "  These will silently pass through token substitution and",
        "  appear LITERALLY in the deployed SQL, where the Teradata",
        "  driver rejects them as 'Unrecognized escape syntax'.",
        "",
    ]

    for file_path in sorted(findings):
        lines.append(f"  {file_path}")
        for issue in findings[file_path]:
            lines.append(
                f"    line {issue['line']}, col {issue['column']}: "
                f"orphan '{issue['marker']}' marker"
            )
            # Indent the offending line for context
            lines.append(f"      | {issue['line_content']}")
        lines.append("")

    lines.append("  Common cause: 'ingest --token-map' re-applied to an")
    lines.append("  already-tokenised file. The substring-replace sees 'DBC'")
    lines.append("  (or similar) inside an existing '{{DBC_DATABASE}}' token")
    lines.append("  and re-substitutes, corrupting the brace structure.")
    lines.append("")
    lines.append("  Fix: hand-edit the affected files to restore well-formed")
    lines.append("  '{{TOKEN_NAME}}' markers, or restore from version control")
    lines.append("  before re-running ingest.")
    lines.append("=" * 64)

    return "\n".join(lines)


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
        files = [f for f, tokens in token_usage.items() if token in tokens]
        for fpath in files:
            errors.append(f"  → used in: {fpath}")

    # Tokens defined but never used — warning
    # Exclude reserved metadata properties (not deployment tokens)
    _RESERVED_PROPERTIES = {"SHIPS_ENV", "SHIPS_PROJECT", "ENV_PREFIX"}
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
            raise KeyError(token_name)
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
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()

    resolved, count = substitute_tokens(content, token_values)

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(resolved)

    if count > 0:
        logger.debug("Substituted %d tokens in %s → %s", count, source_path, dest_path)

    return count


# ---------------------------------------------------------------
# Token map — literal database names → {{TOKEN}} placeholders
# ---------------------------------------------------------------

# System databases that should never be tokenised
_SYSTEM_DBS = {
    "DBC",
    "SYSUDTLIB",
    "SYSLIB",
    "SYSJDBC",
    "SYSBAR",
    "SYSTEMFE",
    "SYSSPATIAL",
    "TD_SYSFNLIB",
    "TD_SYSXML",
    "TDSTATS",
    "TDWM",
    "TD_SYSGPL",
    "ALL",
    "DEFAULT",
    "PUBLIC",
    "EXTUSER",
    "SQLJ",
    "SYSUIF",
    "DBCMNGR",
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
        suffix = db_name[len(env_prefix) :]
        # Strip leading underscore/separator
        suffix = suffix.lstrip("_")
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

    with open(path, "w", encoding="utf-8") as f:
        f.write("# token_map.conf — Literal database name → {{TOKEN}} mapping\n")
        f.write("#\n")
        f.write(f"# Generated by SHIPS harvest with --env-prefix {env_prefix}\n")
        f.write("#\n")
        f.write("# Review and edit token names if needed, then re-harvest with:\n")
        f.write("#   python -m td_release_packager harvest \\\n")
        f.write("#       --source <source> --project <project> \\\n")
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

    logger.info("Token map written: %s (%d mappings)", path, len(token_map))


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
        raise FileNotFoundError(f"Token map file not found: {path}")

    token_map = {}

    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            if "=" not in stripped:
                logger.warning(
                    "token_map.conf line %d: no '=' found, skipping: %s",
                    lineno,
                    stripped,
                )
                continue

            literal, token = stripped.split("=", 1)
            literal = literal.strip()
            token = token.strip()

            if not literal:
                continue

            # Validate token format
            if not (token.startswith("{{") and token.endswith("}}")):
                logger.warning(
                    "token_map.conf line %d: value '%s' is not a "
                    "{{TOKEN}} placeholder — skipping.",
                    lineno,
                    token,
                )
                continue

            token_map[literal] = token

    logger.info("Token map loaded: %s (%d mappings)", path, len(token_map))

    return token_map
