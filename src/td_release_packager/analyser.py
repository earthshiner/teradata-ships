"""
analyser.py — DDL dependency analyser for SHIPS.

Scans parsed DDL files in a project, extracts inter-object
references, builds a directed dependency graph, detects cycles,
and produces a topologically sorted wave ordering for deployment.

Slots into the SHIPS workflow between Harvest and Inspect:

    [S] Scaffold → [H] Harvest → [analyse] → [I] Inspect → [P] Package → [S] Ship

Usage:
    python -m td_release_packager analyze --source <project_dir>

Algorithm:
    1. Build object index from all DDL files in the project.
    2. Build function group index (base name → [specific names])
       to handle Teradata function overloading.
    3. For each DDL file, scan the body (stripping comments and
       string literals) for qualified DB.ObjectName references
       that match other objects in the index.
    4. Build a directed graph: edge from A → B means A depends on B.
    5. Topological sort via Kahn's algorithm produces deployment
       layers (waves).
    6. Generate _waves.txt with wave barriers between layers.

Handles:
    - Cross-database references (flagged as external dependencies)
    - Overloaded functions (function group index)
    - Circular dependencies (detected and reported)
    - Self-references (ignored)
    - Comments and string literals (stripped before scanning)
"""

import logging
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Data models
# ---------------------------------------------------------------

@dataclass
class IndexedObject:
    """
    An object registered in the package index.

    Attributes:
        qualified_name:  'Database.ObjectName' identifier.
        object_type:     DDL type: TABLE, VIEW, MACRO, etc.
        file_path:       Path to the DDL file (relative to project).
        ddl_text:        Raw DDL content.
        base_function:   For overloaded functions, the base function
                         name (without SPECIFIC suffix). None for
                         non-function objects.
    """

    qualified_name: str
    object_type: str
    file_path: str
    ddl_text: str
    base_function: Optional[str] = None


@dataclass
class AnalysisResult:
    """
    Result of dependency analysis.

    Attributes:
        objects:              All indexed objects.
        dependencies:         Dict mapping qualified_name → set of
                              qualified names it depends on.
        external_deps:        Dict mapping qualified_name → set of
                              unresolved external references.
        waves:                List of lists — each inner list is a
                              set of objects deployable in parallel.
        cycles:               List of cycles detected (each cycle
                              is a list of qualified names).
        function_groups:      Dict mapping base function name → list
                              of specific qualified names.
        waves_file_content:   Generated _waves.txt content.
    """

    objects: Dict[str, IndexedObject] = field(default_factory=dict)
    dependencies: Dict[str, Set[str]] = field(default_factory=dict)
    external_deps: Dict[str, Set[str]] = field(default_factory=dict)
    waves: List[List[str]] = field(default_factory=list)
    cycles: List[List[str]] = field(default_factory=list)
    function_groups: Dict[str, List[str]] = field(default_factory=dict)
    waves_file_content: str = ""


# ---------------------------------------------------------------
# Comment and string literal stripping
# ---------------------------------------------------------------

# Matches:  -- line comments, /* block comments */, 'string literals'
_NOISE_RE = re.compile(
    r"--[^\n]*"                # -- line comment
    r"|/\*.*?\*/"              # /* block comment */
    r"|'(?:[^']|'')*'",        # 'string literal' (doubled quotes)
    re.DOTALL,
)


def _strip_noise(ddl_text: str) -> str:
    """
    Remove comments and string literals from DDL text.

    Replaces matched regions with spaces (preserving length)
    so that positional analysis remains valid.
    """
    return _NOISE_RE.sub(lambda m: ' ' * len(m.group(0)), ddl_text)


# ---------------------------------------------------------------
# Header extraction — identify where the DDL body starts
# ---------------------------------------------------------------

# Patterns that mark the end of the CREATE/REPLACE header
# and the start of the body (where references live).
_HEADER_END_PATTERNS = {
    "TABLE": re.compile(
        r'(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?'
        r'(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?'
        r'(?:TRACE\s+)?'
        r'TABLE\s+'
        r'(?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?'
        r'\s*[\s,(\n]',
        re.IGNORECASE,
    ),
    "VIEW": re.compile(
        r'(?:CREATE|REPLACE)\s+VIEW\s+'
        r'(?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?'
        r'\s+AS\b',
        re.IGNORECASE,
    ),
    "MACRO": re.compile(
        r'(?:CREATE|REPLACE)\s+MACRO\s+'
        r'(?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?'
        r'\s+AS\b',
        re.IGNORECASE,
    ),
    "FUNCTION": re.compile(
        r'(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\s+'
        r'(?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?'
        r'.*?RETURNS\b',
        re.IGNORECASE | re.DOTALL,
    ),
    "PROCEDURE": re.compile(
        r'(?:CREATE|REPLACE)\s+PROCEDURE\s+'
        r'(?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?'
        r'\s*\([^)]*\)',          # Skip parameter list
        re.IGNORECASE | re.DOTALL,
    ),
    "TRIGGER": re.compile(
        r'(?:CREATE|REPLACE)\s+TRIGGER\s+'
        r'(?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?'
        r'\s+(?:AFTER|BEFORE|INSTEAD\s+OF)\b',
        re.IGNORECASE,
    ),
}


def _extract_body(ddl_text: str, object_type: str) -> str:
    """
    Extract the DDL body (after the header) where references live.

    For views:  everything after 'AS'
    For macros: everything after 'AS'
    For tables: the column definitions + constraints
    For others: the entire text (conservative — scan everything)
    """
    pattern = _HEADER_END_PATTERNS.get(object_type)
    if pattern:
        match = pattern.search(ddl_text)
        if match:
            return ddl_text[match.end():]
    # For procedures, functions, triggers — scan the whole body
    # (the header itself may contain ON db.table for triggers)
    return ddl_text


# ---------------------------------------------------------------
# Qualified name scanner
# ---------------------------------------------------------------

# Matches DB.ObjectName patterns — two-part qualified names.
# Avoids matching inside keywords like CREATE, REPLACE, etc.
_QUALIFIED_REF_RE = re.compile(
    r'\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\b'
)

# Keywords and noise that look like qualified names but aren't
_IGNORE_PREFIXES = {
    'DBC', 'SYSLIB', 'SYSUDTLIB', 'SYSUIF', 'TD_SYSFNLIB',
    'TD_SYSXML', 'SQLJ', 'SYSSPATIAL', 'DBCMNGR',
    'DEFAULT', 'CHARACTER', 'FORMAT', 'CHECKSUM',
    'NO', 'WITH', 'ON', 'PRIMARY', 'UNIQUE', 'PARTITION',
}

_IGNORE_SUFFIXES = {
    'FALLBACK', 'LOG', 'JOURNAL', 'MERGEBLOCKRATIO',
    'BLOCKCOMPRESSION', 'DATABLOCKSIZE', 'FREESPACE',
}


def _scan_references(
    ddl_text: str,
    object_type: str,
    own_qualified: str,
    known_databases: Set[str],
) -> Tuple[Set[str], Set[str]]:
    """
    Scan DDL body for qualified DB.ObjectName references.

    Only considers references where the database prefix matches
    a known database in the package, or is long enough to be a
    plausible database name (>2 chars). This filters out table
    aliases like c.Cust_Id, o.Order_Amt, n.Msg.

    Args:
        ddl_text:         Raw DDL content.
        object_type:      The object type (TABLE, VIEW, etc.).
        own_qualified:    The object's own qualified name (to skip
                          self-references).
        known_databases:  Set of database names found in the package
                          (upper-cased).

    Returns:
        Tuple of (internal_refs, external_refs) — both sets of
        qualified names.
    """
    # Strip comments and string literals
    clean = _strip_noise(ddl_text)

    # Extract body (skip the header for tables/views/macros)
    body = _extract_body(clean, object_type)

    internal = set()
    external = set()

    for match in _QUALIFIED_REF_RE.finditer(body):
        db_part = match.group(1)
        obj_part = match.group(2)

        # Skip system databases and DDL noise
        if db_part.upper() in _IGNORE_PREFIXES:
            continue
        if obj_part.upper() in _IGNORE_SUFFIXES:
            continue

        # Skip short prefixes — these are table/trigger aliases
        # (e.g. c.Cust_Id, n.Msg, o.Order_Amt)
        if len(db_part) <= 2:
            continue

        qualified = f"{db_part}.{obj_part}"

        # Skip self-references
        if qualified.upper() == own_qualified.upper():
            continue

        # Classify as internal or external
        if db_part.upper() in known_databases:
            internal.add(qualified)
        else:
            external.add(qualified)

    return (internal, external)


# ---------------------------------------------------------------
# Object index builder
# ---------------------------------------------------------------

# Classification patterns (same as ingest.py)
_CLASSIFY_PATTERNS = [
    (re.compile(r'CREATE\s+JOIN\s+INDEX\b', re.I), "JOIN_INDEX"),
    (re.compile(r'CREATE\s+HASH\s+INDEX\b', re.I), "HASH_INDEX"),
    (re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\b', re.I), "INDEX"),
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b.*?TABLE\s+OPERATOR', re.I | re.DOTALL), "SCRIPT_TABLE_OPERATOR"),
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:MULTISET|SET)?\s*(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?(?:TRACE\s+)?TABLE\b', re.I), "TABLE"),
    (re.compile(r'(?:CREATE|REPLACE)\s+VIEW\b', re.I), "VIEW"),
    (re.compile(r'(?:CREATE|REPLACE)\s+MACRO\b', re.I), "MACRO"),
    (re.compile(r'(?:CREATE|REPLACE)\s+PROCEDURE\b', re.I), "PROCEDURE"),
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b', re.I), "FUNCTION"),
    (re.compile(r'(?:CREATE|REPLACE)\s+TRIGGER\b', re.I), "TRIGGER"),
]

# Qualified name extraction
_QUALIFIED_NAME_RE = re.compile(
    r'(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?'
    r'(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?'
    r'(?:TRACE\s+)?'
    r'(?:SPECIFIC\s+)?'
    r'(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|'
    r'JOIN\s+INDEX|HASH\s+INDEX)\s+'
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)

# Base function name (from header, not SPECIFIC clause)
_FUNCTION_HEADER_RE = re.compile(
    r'(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\s+'
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)

# SPECIFIC clause inside function body
_SPECIFIC_RE = re.compile(
    r'SPECIFIC\s+("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)


def _classify(content: str) -> Optional[str]:
    """Classify a DDL file by object type."""
    for pattern, obj_type in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            return obj_type
    return None


def _extract_name(content: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract database.object name from DDL."""
    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return (None, None)
    qualified = match.group(1).replace('"', '')
    parts = qualified.split('.')
    if len(parts) == 2:
        return (parts[0].strip(), parts[1].strip())
    return (None, parts[0].strip() if parts else None)


def _extract_function_base_name(content: str) -> Optional[str]:
    """
    Extract the base function name from the header.

    For overloaded functions, this is the name in the
    CREATE/REPLACE FUNCTION statement (not the SPECIFIC name).
    Returns 'DB.FunctionName' or None.
    """
    match = _FUNCTION_HEADER_RE.search(content)
    if not match:
        return None
    return match.group(1).replace('"', '')


# ---------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------

def analyse_project(project_dir: str) -> AnalysisResult:
    """
    Analyse DDL dependencies in a SHIPS project.

    Scans all DDL files in the project payload, builds a
    dependency graph, detects cycles, and produces a
    topologically sorted wave ordering.

    Args:
        project_dir: Path to the SHIPS project root.

    Returns:
        AnalysisResult with the full dependency analysis.
    """
    result = AnalysisResult()

    # -- Locate DDL files --
    payload_dir = _find_payload(project_dir)
    if not payload_dir:
        logger.error("No payload directory found in %s", project_dir)
        return result

    ddl_files = _collect_ddl_files(payload_dir)
    logger.info("Found %d DDL files in %s", len(ddl_files), payload_dir)

    # -- Phase 1: Build object index --
    for file_path in ddl_files:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        obj_type = _classify(content)
        if obj_type is None:
            continue

        db_name, obj_name = _extract_name(content)
        if not db_name or not obj_name:
            continue

        # For functions, use SPECIFIC name to avoid overload collisions
        base_fn = None
        if obj_type == "FUNCTION":
            base_fn = f"{db_name}.{obj_name}"  # Base function name
            specific_match = _SPECIFIC_RE.search(content)
            if specific_match:
                specific_qual = specific_match.group(1).replace('"', '')
                parts = specific_qual.split('.')
                # Use the specific name as the object name
                obj_name = parts[-1].strip()

        qualified = f"{db_name}.{obj_name}"
        rel_path = os.path.relpath(file_path, project_dir)

        obj = IndexedObject(
            qualified_name=qualified,
            object_type=obj_type,
            file_path=rel_path,
            ddl_text=content,
            base_function=base_fn,
        )

        result.objects[qualified] = obj
        result.dependencies[qualified] = set()

    # -- Phase 2: Build function group index --
    # Maps base function name → [specific qualified names]
    fn_groups = defaultdict(list)
    for qn, obj in result.objects.items():
        if obj.object_type == "FUNCTION" and obj.base_function:
            fn_groups[obj.base_function.upper()].append(qn)

    result.function_groups = dict(fn_groups)

    # Build known database names (for alias filtering)
    known_databases = set()
    for qn in result.objects:
        db_part = qn.split('.')[0]
        known_databases.add(db_part.upper())

    # Build a reverse lookup: upper-cased name → set of qualified names
    # This handles both exact matches and function group matches.
    name_index = {}
    for qn in result.objects:
        name_index[qn.upper()] = {qn}

    # Add function groups: base name maps to all overloads
    for base_name, specifics in fn_groups.items():
        base_upper = base_name.upper()
        if base_upper not in name_index:
            name_index[base_upper] = set()
        name_index[base_upper].update(specifics)

    # -- Phase 3: Scan references and build edges --
    for qn, obj in result.objects.items():
        internal_refs, ext_refs = _scan_references(
            obj.ddl_text, obj.object_type, qn, known_databases,
        )

        for ref in internal_refs:
            ref_upper = ref.upper()

            if ref_upper in name_index:
                # Internal dependency — add edges to all matching objects
                for target in name_index[ref_upper]:
                    if target != qn:  # No self-edges
                        result.dependencies[qn].add(target)
            else:
                # Known database but unknown object — still external
                if qn not in result.external_deps:
                    result.external_deps[qn] = set()
                result.external_deps[qn].add(ref)

        # External references
        if ext_refs:
            if qn not in result.external_deps:
                result.external_deps[qn] = set()
            result.external_deps[qn].update(ext_refs)

    # -- Phase 4: Detect cycles --
    result.cycles = _detect_cycles(result.dependencies)

    # -- Phase 5: Topological sort → waves --
    if result.cycles:
        logger.warning(
            "Circular dependencies detected — wave ordering may "
            "be incomplete. %d cycle(s) found.",
            len(result.cycles),
        )

    result.waves = _topological_sort(
        result.dependencies, result.objects,
    )

    # -- Phase 6: Generate _waves.txt --
    result.waves_file_content = _generate_waves_txt(
        result.waves, result.objects, project_dir,
    )

    return result


# ---------------------------------------------------------------
# Cycle detection (DFS-based)
# ---------------------------------------------------------------

def _detect_cycles(
    dependencies: Dict[str, Set[str]],
) -> List[List[str]]:
    """
    Detect cycles in the dependency graph using DFS.

    Returns a list of cycles, where each cycle is a list
    of qualified names forming a loop.
    """
    cycles = []
    visited = set()
    rec_stack = set()
    path = []

    def dfs(node):
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for dep in dependencies.get(node, set()):
            if dep not in visited:
                dfs(dep)
            elif dep in rec_stack:
                # Found a cycle — extract it
                cycle_start = path.index(dep)
                cycle = path[cycle_start:] + [dep]
                cycles.append(cycle)

        path.pop()
        rec_stack.discard(node)

    for node in dependencies:
        if node not in visited:
            dfs(node)

    return cycles


# ---------------------------------------------------------------
# Topological sort (Kahn's algorithm → layers)
# ---------------------------------------------------------------

def _topological_sort(
    dependencies: Dict[str, Set[str]],
    objects: Dict[str, IndexedObject],
) -> List[List[str]]:
    """
    Topological sort producing deployment layers (waves).

    Uses Kahn's algorithm. Objects with no dependencies go
    in wave 0, objects depending only on wave 0 in wave 1, etc.

    Objects involved in cycles are placed in the final wave
    with a warning.

    Args:
        dependencies: Dict mapping object → set of dependencies.
        objects:      The full object index.

    Returns:
        List of waves. Each wave is a list of qualified names
        that can be deployed in parallel.
    """
    # Build in-degree map
    in_degree = {node: 0 for node in dependencies}
    for node, deps in dependencies.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] = in_degree.get(dep, 0)

    # Recalculate: count how many objects depend on each node
    in_degree = {node: 0 for node in dependencies}
    for node, deps in dependencies.items():
        for dep in deps:
            if dep in in_degree:
                pass  # dep exists in our set

    # Actually build in-degree properly:
    # in_degree[X] = number of objects that X depends on (within package)
    in_degree = {}
    for node in dependencies:
        count = sum(1 for d in dependencies[node] if d in dependencies)
        in_degree[node] = count

    waves = []
    remaining = set(dependencies.keys())

    while remaining:
        # Find all nodes with in-degree 0 (no unresolved dependencies)
        wave = [
            n for n in remaining
            if all(d not in remaining for d in dependencies.get(n, set()))
        ]

        if not wave:
            # All remaining nodes are in cycles — dump them
            # in a final wave with a warning
            logger.warning(
                "Cycle detected: placing %d objects in final wave",
                len(remaining),
            )
            waves.append(sorted(remaining))
            break

        # Sort within wave for deterministic output:
        # Tables first, then indexes, then views/macros/procs, then triggers
        type_order = {
            "TABLE": 0, "JOIN_INDEX": 1, "HASH_INDEX": 1,
            "INDEX": 1, "FUNCTION": 2, "MACRO": 2,
            "VIEW": 3, "PROCEDURE": 3, "TRIGGER": 4,
        }
        wave.sort(key=lambda n: (
            type_order.get(objects[n].object_type, 99),
            n,
        ))

        waves.append(wave)
        remaining -= set(wave)

    return waves


# ---------------------------------------------------------------
# _waves.txt generator
# ---------------------------------------------------------------

def _generate_waves_txt(
    waves: List[List[str]],
    objects: Dict[str, IndexedObject],
    project_dir: str,
) -> str:
    """
    Generate _waves.txt content from topological layers.

    Each wave is separated by a '---' barrier line. Files within
    a wave can be deployed in parallel.

    Args:
        waves:       List of waves from topological sort.
        objects:     The full object index.
        project_dir: Project root (for relative paths).

    Returns:
        _waves.txt file content as a string.
    """
    lines = [
        "# _waves.txt — auto-generated by SHIPS dependency analyser",
        "# Objects within a wave have no mutual dependencies and",
        "# can be deployed in parallel. Wave barriers (---) enforce",
        "# ordering: all objects in wave N must complete before",
        "# wave N+1 begins.",
        "#",
        "# Regenerate with:",
        "#   python -m td_release_packager analyze --source <project>",
        "",
    ]

    # Collect object types per wave for the summary comment
    for i, wave in enumerate(waves):
        type_counts = defaultdict(int)
        for qn in wave:
            if qn in objects:
                type_counts[objects[qn].object_type] += 1

        type_summary = ", ".join(
            f"{count} {t.lower()}{'s' if count > 1 else ''}"
            for t, count in sorted(type_counts.items())
        )

        if i > 0:
            lines.append("---")

        lines.append(f"# Wave {i + 1}: {type_summary}")

        for qn in wave:
            if qn in objects:
                lines.append(objects[qn].file_path)

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------
# File discovery helpers
# ---------------------------------------------------------------

def _find_payload(project_dir: str) -> Optional[str]:
    """Locate the payload directory in a SHIPS project."""
    for candidate in ['payload/database', 'payload']:
        path = os.path.join(project_dir, candidate)
        if os.path.isdir(path):
            return path
    # Fallback: if the project_dir itself contains DDL subdirs
    for subdir in ['DDL', 'tables', 'views']:
        if os.path.isdir(os.path.join(project_dir, subdir)):
            return project_dir
    return None


def _collect_ddl_files(payload_dir: str) -> List[str]:
    """Collect all DDL files from the payload directory."""
    extensions = {
        '.tbl', '.viw', '.spl', '.mcr', '.fnc',
        '.trg', '.jix', '.idx', '.sql', '.ddl',
        '.sto', '.jcl',
    }
    files = []
    for root, dirs, filenames in os.walk(payload_dir):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith('.') or f.startswith('_'):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in extensions:
                files.append(os.path.join(root, f))
    return files


# ---------------------------------------------------------------
# CLI-friendly summary
# ---------------------------------------------------------------

def format_summary(result: AnalysisResult) -> str:
    """
    Format the analysis result as a human-readable summary.

    Args:
        result: The AnalysisResult from analyse_project.

    Returns:
        Multi-line string for CLI output.
    """
    lines = []
    lines.append(f"  Objects indexed:      {len(result.objects)}")
    lines.append(f"  Waves generated:      {len(result.waves)}")

    # Count total internal edges
    total_edges = sum(len(deps) for deps in result.dependencies.values())
    lines.append(f"  Internal dependencies: {total_edges}")

    # External dependencies
    ext_count = sum(len(deps) for deps in result.external_deps.values())
    lines.append(f"  External references:  {ext_count}")

    # Cycles
    if result.cycles:
        lines.append(f"  ⚠ Cycles detected:   {len(result.cycles)}")
    else:
        lines.append(f"  Cycles:              None")

    # Function groups
    overloaded = {k: v for k, v in result.function_groups.items() if len(v) > 1}
    if overloaded:
        lines.append(f"  Overloaded functions: {len(overloaded)}")
        for base, specifics in sorted(overloaded.items()):
            lines.append(f"    {base} × {len(specifics)} overloads")

    lines.append("")

    # Wave summary
    for i, wave in enumerate(result.waves):
        type_counts = defaultdict(int)
        for qn in wave:
            if qn in result.objects:
                type_counts[result.objects[qn].object_type] += 1

        type_str = ", ".join(
            f"{c} {t}" for t, c in sorted(type_counts.items())
        )
        lines.append(f"  Wave {i + 1}: {len(wave)} objects ({type_str})")
        for qn in wave:
            deps = result.dependencies.get(qn, set())
            dep_str = f" → depends on: {', '.join(sorted(deps))}" if deps else ""
            lines.append(f"    {qn}{dep_str}")

    # External dependencies
    if result.external_deps:
        lines.append("")
        lines.append("  External dependencies (outside package):")
        for qn, ext_refs in sorted(result.external_deps.items()):
            for ref in sorted(ext_refs):
                lines.append(f"    {qn} → {ref}")

    # Cycles
    if result.cycles:
        lines.append("")
        lines.append("  ⚠ Circular dependencies:")
        for cycle in result.cycles:
            lines.append(f"    {' → '.join(cycle)}")

    return "\n".join(lines)
