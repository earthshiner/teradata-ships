"""Auto-fixer for the ``hardcoded_name`` inspect rule (#527, #541 Phase 2a).

Plan-file workflow (agent-friendly, no interactive TTY yet):

1. **First run (dry-run or apply on a plan-less project)** — scans the
   payload for hardcoded database qualifiers, proposes a ``{{token}}``
   for each, and writes ``.ships/hardcoded_name.plan.json``. The plan
   is human-editable JSON: operators (or an agent) review it, delete
   entries they don't want tokenised, and rename tokens as needed.
2. **Second run (apply mode, plan present)** — reads the plan and
   rewrites every ``literal`` in payload files to the paired ``token``,
   then deletes the plan file so the next run starts a fresh proposal
   cycle.

Registered ``default_on=False`` — the fix requires operator judgement
per the rules catalogue (``requires_human_review: True``).

Phase 2a features (this module):

* **Smart proposals** — when ``<project>/config/tokenise.conf`` exists,
  apply its substitution rules to each candidate literal to compute
  the proposed token. Falls back to verbatim wrap when no rule matches
  or no config exists.
* **Persistent skip list** — ``.ships/hardcoded_name.exceptions``
  carries operator-declared "always skip this literal" decisions
  across runs. Merged with the system-database set at scan time.

Deferred to Phase 2b:

* Interactive TTY prompt with y/e/s/S/q actions.
* Atomic updates to ``config/tokenise.conf`` and
  ``config/token_map.conf`` alongside the payload rewrite.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from td_release_packager.fixers._registry import FixerSpec, register
from td_release_packager.fixers._result import FixResult, FixResultFile

# Databases the rule check already excludes — they are system-scoped
# and never need tokenising. Kept in sync with the check at
# ``validate._check_hardcoded_names``.
_SYSTEM_DATABASES: frozenset[str] = frozenset(
    {
        "DBC",
        "SYSUDTLIB",
        "SYSLIB",
        "SYSJDBC",
        "TD_SYSFNLIB",
        "TDSTATS",
    }
)


def _looks_like_alias(literal: str) -> bool:
    """Heuristic — short lowercase identifiers are almost always
    table/column aliases (``c.Id``, ``o.Total``, ``t1.foo``), not
    database names.

    Real Teradata database names are typically 4+ chars and CamelCase
    or UPPER_SNAKE_CASE. The check errs on the side of caution: a
    "borderline" 4-char lowercase name like ``prod`` is accepted as
    a database, but a 3-char one (``dev``) is rejected as likely-alias.
    False negatives here (a real database name we skip) surface later
    as an unfixed hardcoded_name warning — annoying but safe. False
    positives (an alias we tokenise) would corrupt the file — so bias
    hard against them.
    """
    if len(literal) <= 3 and literal.islower():
        return True
    return False


_PLAN_SCHEMA_VERSION = 1
_PLAN_RELATIVE_PATH = os.path.join(".ships", "hardcoded_name.plan.json")

_EXCEPTIONS_SCHEMA_VERSION = 1
_EXCEPTIONS_RELATIVE_PATH = os.path.join(".ships", "hardcoded_name.exceptions.json")


def _plan_path(project_dir: str) -> str:
    return os.path.join(project_dir, _PLAN_RELATIVE_PATH)


def _exceptions_path(project_dir: str) -> str:
    return os.path.join(project_dir, _EXCEPTIONS_RELATIVE_PATH)


def _load_exceptions(project_dir: str) -> frozenset[str]:
    """Load the operator-declared "always skip this literal" list.

    File shape (versioned JSON, evolves without breaking older readers):

        {
          "schema_version": 1,
          "exclude": ["Legacy_DB", "Prototype_STAGING"]
        }

    Returns an empty set when the file is missing or malformed —
    exceptions are additive to the built-in system-database exclusions,
    never a replacement for them.
    """
    path = _exceptions_path(project_dir)
    if not os.path.isfile(path):
        return frozenset()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(doc, dict):
        return frozenset()
    if doc.get("schema_version") != _EXCEPTIONS_SCHEMA_VERSION:
        return frozenset()
    exclude = doc.get("exclude")
    if not isinstance(exclude, list):
        return frozenset()
    return frozenset(e for e in exclude if isinstance(e, str) and e)


def _load_tokenise_rules(project_dir: str):
    """Load and parse ``<project>/config/tokenise.conf`` if present.

    Returns a list of parsed migration rules ready for
    ``apply_migration_rules_to_text``, or ``None`` when the file is
    missing or the parser rejects it. Callers treat ``None`` as
    "no smart proposals" and fall back to verbatim wrap.
    """
    path = os.path.join(project_dir, "config", "tokenise.conf")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return None
    # Lazy import — source_migrator is heavyweight and rarely needed.
    from td_release_packager.source_migrator import parse_migration_sed

    try:
        rules, errors = parse_migration_sed(content)
    except Exception:
        return None
    if errors and not rules:
        return None
    return rules


def _smart_propose_token(literal: str, rules) -> str:
    """Compute the proposed ``{{token}}`` for a literal.

    When ``rules`` is None (no tokenise.conf), returns ``{{literal}}``
    verbatim. Otherwise runs the tokenise rules against the literal —
    if the transformed text differs and looks like a well-formed
    token reference, returns it. Otherwise falls back to verbatim.
    """
    verbatim = f"{{{{{literal}}}}}"
    if not rules:
        return verbatim

    from td_release_packager.source_migrator import apply_migration_rules_to_text

    try:
        transformed, _hits = apply_migration_rules_to_text(literal, rules)
    except Exception:
        return verbatim
    # Only accept the transformation when it produced a non-trivial
    # token-shaped result. A rule that maps ``ProdDB`` to ``{{DB_PREFIX}}``
    # is exactly what we want; a rule that maps it to itself, or to a
    # bare identifier, is not — fall back to the safe wrap.
    if transformed == literal:
        return verbatim
    if "{{" not in transformed or "}}" not in transformed:
        return verbatim
    return transformed


def _load_plan(project_dir: str) -> dict | None:
    """Load the plan file if it exists and is well-formed. Otherwise None."""
    path = _plan_path(project_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(plan, dict):
        return None
    if plan.get("schema_version") != _PLAN_SCHEMA_VERSION:
        return None
    if not isinstance(plan.get("proposals"), list):
        return None
    return plan


def _write_plan(project_dir: str, proposals: list[dict]) -> str:
    """Write ``proposals`` to the plan file. Creates ``.ships/`` if needed."""
    path = _plan_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proposals": proposals,
    }
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def _discover_literals(
    source_dir: str, extra_exclude: frozenset[str] = frozenset()
) -> dict[str, list[dict]]:
    """Walk the payload and return ``{literal: [occurrences]}``.

    Skips SHIPS-generated paths, system databases, operator-declared
    exceptions, and references inside comments or string literals.
    Occurrence entries carry ``{"file", "line"}`` for the human/agent
    reviewing the plan.

    Args:
        source_dir:    SHIPS project directory to walk.
        extra_exclude: Additional literals to skip. Merged with the
                       built-in ``_SYSTEM_DATABASES`` set — never
                       replaces it. Callers typically populate this
                       from ``_load_exceptions``.
    """
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.validate import (
        _DB_QUALIFIED_REF_RE,
        _build_exclusion_mask,
        _prune_generated_dirs,
        _strip_identifier_quotes,
    )

    literals: dict[str, list[dict]] = {}
    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    # Include DDL / DML / DCL extensions the rule normally scans.
    extensions.update(
        {".tbl", ".viw", ".mcr", ".spl", ".fnc", ".trg", ".sto", ".dml", ".dcl", ".grt"}
    )

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            exclusion_mask = _build_exclusion_mask(content)

            for match in _DB_QUALIFIED_REF_RE.finditer(content):
                if exclusion_mask[match.start()]:
                    continue
                raw_db = match.group(1)
                # Skip tokens — ``{{X}}.Foo`` is already tokenised.
                if raw_db.startswith("{{"):
                    continue
                db_name = _strip_identifier_quotes(raw_db)
                if db_name.upper() in _SYSTEM_DATABASES:
                    continue
                if db_name in extra_exclude:
                    continue
                if _looks_like_alias(db_name):
                    continue

                line_num = content[: match.start(1)].count("\n") + 1
                literals.setdefault(db_name, []).append(
                    {
                        "file": os.path.relpath(file_path, source_dir),
                        "line": line_num,
                    }
                )

    return literals


def _apply_plan(
    source_dir: str,
    plan: dict,
    result: FixResult,
    dry_run: bool,
) -> None:
    """Substitute each ``literal → token`` mapping across payload files.

    Populates ``result.files_changed`` and ``result.totals``. Skips
    references inside comments / string literals via the same
    exclusion mask the check uses, and preserves quoting.
    """
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.validate import (
        _DB_QUALIFIED_REF_RE,
        _build_exclusion_mask,
        _prune_generated_dirs,
        _strip_identifier_quotes,
    )

    # Build a fast lookup from case-preserving literal → token.
    subs: dict[str, str] = {}
    for proposal in plan.get("proposals", []):
        literal = proposal.get("literal")
        token = proposal.get("token")
        if isinstance(literal, str) and isinstance(token, str):
            subs[literal] = token

    if not subs:
        return

    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    extensions.update(
        {".tbl", ".viw", ".mcr", ".spl", ".fnc", ".trg", ".sto", ".dml", ".dcl", ".grt"}
    )

    total_subs = 0

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                result.errors.append(
                    {
                        "file": os.path.relpath(file_path, source_dir),
                        "error": f"read failed: {exc}",
                    }
                )
                continue

            exclusion_mask = _build_exclusion_mask(content)

            substitutions: list[tuple[int, int, str]] = []
            per_file: dict[str, int] = {}

            for match in _DB_QUALIFIED_REF_RE.finditer(content):
                if exclusion_mask[match.start()]:
                    continue
                raw_db = match.group(1)
                if raw_db.startswith("{{"):
                    continue
                db_name = _strip_identifier_quotes(raw_db)
                token = subs.get(db_name)
                if token is None:
                    continue

                start = match.start(1)
                end = match.end(1)
                substitutions.append((start, end, token))
                per_file[db_name] = per_file.get(db_name, 0) + 1

            if not substitutions:
                continue

            new_content = content
            for start, end, token in sorted(substitutions, reverse=True):
                new_content = new_content[:start] + token + new_content[end:]

            if not dry_run:
                try:
                    with open(file_path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(new_content)
                except OSError as exc:
                    result.errors.append(
                        {
                            "file": os.path.relpath(file_path, source_dir),
                            "error": f"write failed: {exc}",
                        }
                    )
                    continue

            rel_path = os.path.relpath(file_path, source_dir)
            total_subs += len(substitutions)
            result.files_changed.append(
                FixResultFile(
                    file=rel_path,
                    details={
                        "substitutions": per_file,
                        "total": len(substitutions),
                    },
                )
            )

    result.totals["substitutions"] = total_subs


def fix_hardcoded_name(source_dir: str, dry_run: bool = False) -> FixResult:
    """Tokenise hardcoded database qualifiers under ``payload/``.

    The fixer runs one of two modes based on whether a plan file exists
    at ``<project>/.ships/hardcoded_name.plan.json``:

    * **Propose mode** (no plan on disk): walk the payload, propose a
      ``{{literal}}`` token for each hardcoded qualifier, and write the
      plan file. No payload writes even when ``dry_run=False`` — the
      operator (or an agent) must review the plan and re-run to apply.
    * **Apply mode** (plan on disk): read the plan, rewrite each
      ``literal`` to the paired ``token`` in every payload reference,
      then delete the plan file so the next run starts a fresh
      proposal cycle. Under ``dry_run=True`` no writes happen and the
      plan is left in place.

    Args:
        source_dir: SHIPS project directory (parent of ``payload/``).
        dry_run:    When True, no filesystem writes. Propose mode still
                    computes the proposals for reporting; apply mode
                    still walks the plan for reporting.
    """
    result = FixResult(rule_id="hardcoded_name", dry_run=dry_run)

    plan = _load_plan(source_dir)

    if plan is None:
        # Propose mode. Consult the two Phase-2a inputs — the persistent
        # exceptions file (skip literals the operator has already told
        # us are not to be tokenised) and ``config/tokenise.conf`` (use
        # its rules to pick smart tokens).
        exceptions = _load_exceptions(source_dir)
        rules = _load_tokenise_rules(source_dir)
        literals = _discover_literals(source_dir, extra_exclude=exceptions)
        proposals = [
            {
                "literal": literal,
                "token": _smart_propose_token(literal, rules),
                "occurrences": occurrences,
            }
            for literal, occurrences in sorted(literals.items())
        ]
        if proposals and not dry_run:
            _write_plan(source_dir, proposals)
        # Propose mode records the count under a distinct key so callers
        # can tell propose runs from apply runs. Apply mode reports
        # ``substitutions``.
        result.totals["proposals"] = len(proposals)
        return result

    # Apply mode.
    _apply_plan(source_dir, plan, result, dry_run)
    if not dry_run and result.files_changed:
        # Consume the plan so the next run starts fresh.
        try:
            os.remove(_plan_path(source_dir))
        except OSError as exc:
            result.errors.append(
                {
                    "file": _PLAN_RELATIVE_PATH,
                    "error": f"could not delete plan after apply: {exc}",
                }
            )
    return result


SPEC = register(
    FixerSpec(
        rule_id="hardcoded_name",
        apply=fix_hardcoded_name,
        default_on=False,
        write_scope="payload",
    )
)
