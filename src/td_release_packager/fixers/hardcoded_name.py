"""Auto-fixer for the ``hardcoded_name`` inspect rule (#527, #541 Phase 2a + 2b).

Plan-file workflow (agent-friendly):

1. **First run (propose mode)** — scans the payload for hardcoded
   database qualifiers, proposes a ``{{token}}`` for each, and writes
   ``.ships/hardcoded_name.plan.json``. Operators (or an agent) review,
   delete entries they don't want tokenised, and rename tokens.
2. **Second run (apply mode)** — reads the plan, rewrites every
   ``literal`` in payload files to the paired ``token``, and updates
   ``config/tokenise.conf`` + ``config/token_map.conf`` so the
   mapping survives re-harvest. The plan file is consumed on
   success.

Registered ``default_on=False`` — the fix requires operator judgement
per the rules catalogue (``requires_human_review: True``).

Features:

* **Smart proposals** — when ``<project>/config/tokenise.conf`` exists,
  apply its substitution rules to each candidate literal to compute
  the proposed token. Falls back to verbatim wrap when no rule matches
  or no config exists. (Phase 2a.)
* **Persistent skip list** — ``.ships/hardcoded_name.exceptions``
  carries operator-declared "always skip this literal" decisions
  across runs. Merged with the system-database set at scan time.
  (Phase 2a.)
* **Atomic config updates** — apply mode extends
  ``config/tokenise.conf`` and ``config/token_map.conf`` alongside
  the payload rewrite. If any config write fails, every payload
  rewrite is rolled back to its pre-fix content. (Phase 2b.)
* **Interactive review helper** — :func:`interactive_review` walks
  the plan proposals one at a time with y/e/s/S/q actions and updates
  the plan in place. Wired to the CLI via ``ships review-plan``.
  Testable via stream injection. (Phase 2b.)
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


def _extend_tokenise_conf(project_dir: str, subs: dict[str, str]) -> None:
    """Append ``regex::^<literal>$:=<token>`` rules to ``config/tokenise.conf``.

    Idempotent — skips rules whose exact ``regex::…`` line is already
    present. Creates the file (with a header) when it doesn't exist.
    Every rule anchors both ends so ``Prod`` doesn't match ``ProdOther``.
    """
    conf_dir = os.path.join(project_dir, "config")
    conf_path = os.path.join(conf_dir, "tokenise.conf")
    existing = ""
    if os.path.isfile(conf_path):
        with open(conf_path, encoding="utf-8") as fh:
            existing = fh.read()
    new_lines: list[str] = []
    for literal, token in sorted(subs.items()):
        rule = f"regex::^{literal}$:={token}"
        if rule not in existing and rule not in "\n".join(new_lines):
            new_lines.append(rule)
    if not new_lines:
        return
    os.makedirs(conf_dir, exist_ok=True)
    with open(conf_path, "a", encoding="utf-8", newline="") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        if not existing:
            fh.write(
                "# tokenise.conf — literal → token substitution rules.\n"
                "# One rule per line: regex::PATTERN:=REPLACEMENT\n"
                "# Auto-appended by `ships fix --rules hardcoded_name`.\n"
                "\n"
            )
        for line in new_lines:
            fh.write(line + "\n")


def _extend_token_map_conf(project_dir: str, subs: dict[str, str]) -> None:
    """Append ``LITERAL=TOKEN`` entries to ``config/token_map.conf``.

    Idempotent — skips literals already listed as an LHS. The mapping
    lets ``ships scan`` (and any tool reading the map) see the token
    without re-inferring it from tokenise.conf.
    """
    conf_dir = os.path.join(project_dir, "config")
    conf_path = os.path.join(conf_dir, "token_map.conf")
    existing_literals: set[str] = set()
    existing_text = ""
    if os.path.isfile(conf_path):
        with open(conf_path, encoding="utf-8") as fh:
            existing_text = fh.read()
        for line in existing_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, _, _ = stripped.partition("=")
            if key.strip():
                existing_literals.add(key.strip())
    new_lines: list[str] = []
    for literal, token in sorted(subs.items()):
        if literal in existing_literals:
            continue
        new_lines.append(f"{literal}={token}")
    if not new_lines:
        return
    os.makedirs(conf_dir, exist_ok=True)
    with open(conf_path, "a", encoding="utf-8", newline="") as fh:
        if existing_text and not existing_text.endswith("\n"):
            fh.write("\n")
        if not existing_text:
            fh.write(
                "# token_map.conf — literal database name → {{TOKEN}} map.\n"
                "# One mapping per line: LITERAL_NAME={{TOKEN_NAME}}\n"
                "# Auto-appended by `ships fix --rules hardcoded_name`.\n"
                "\n"
            )
        for line in new_lines:
            fh.write(line + "\n")


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

    # Snapshots of pre-fix payload content, keyed by absolute path.
    # Populated only in apply mode so a config-write failure can roll
    # back every payload rewrite the fixer just performed.
    payload_snapshots: dict[str, bytes] = {}

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
                    payload_snapshots[file_path] = content.encode("utf-8")
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

    # Extend the two config files atomically with the payload rewrite.
    # On failure, roll back every payload write from the snapshots so
    # the fixer either commits everything or nothing. Skipped under
    # dry_run — that path never wrote anything to begin with.
    if not dry_run and payload_snapshots:
        try:
            _extend_tokenise_conf(source_dir, subs)
            _extend_token_map_conf(source_dir, subs)
        except OSError as exc:
            for restored_path, original_bytes in payload_snapshots.items():
                try:
                    with open(restored_path, "wb") as fh:
                        fh.write(original_bytes)
                except OSError:
                    # Best-effort rollback — if we can't restore a
                    # file, surface it so the operator can eyeball
                    # the tree. Don't crash.
                    result.errors.append(
                        {
                            "file": os.path.relpath(restored_path, source_dir),
                            "error": (
                                "rollback failed after config write error — "
                                "restore this file manually"
                            ),
                        }
                    )
            result.errors.append(
                {
                    "file": "config/",
                    "error": (f"config write failed, payload rolled back: {exc}"),
                }
            )
            # Signal the caller no changes stuck.
            result.files_changed.clear()
            result.totals["substitutions"] = 0


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


# ---------------------------------------------------------------------------
# Interactive review (Phase 2b)
# ---------------------------------------------------------------------------


class ReviewResult:
    """Summary of an :func:`interactive_review` session."""

    __slots__ = ("accepted", "edited", "skipped", "skipped_all", "quit_early")

    def __init__(self) -> None:
        self.accepted = 0
        self.edited = 0
        self.skipped = 0
        self.skipped_all: list[str] = []
        self.quit_early = False

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "edited": self.edited,
            "skipped": self.skipped,
            "skipped_all": list(self.skipped_all),
            "quit_early": self.quit_early,
        }


def _write_exceptions(project_dir: str, exclude: list[str]) -> None:
    """Merge ``exclude`` into ``.ships/hardcoded_name.exceptions.json``.

    Preserves any literals already listed — additive only. Creates
    the file when it doesn't exist.
    """
    path = _exceptions_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing: set[str] = set()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and isinstance(doc.get("exclude"), list):
                existing = {e for e in doc["exclude"] if isinstance(e, str)}
        except (OSError, json.JSONDecodeError):
            existing = set()
    merged = sorted(existing | {e for e in exclude if e})
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(
            {"schema_version": _EXCEPTIONS_SCHEMA_VERSION, "exclude": merged},
            fh,
            indent=2,
        )
        fh.write("\n")


def interactive_review(
    project_dir: str,
    input_stream=None,
    output_stream=None,
) -> ReviewResult:
    """Walk the plan proposals interactively and update the plan in place.

    For each proposal presents the literal, the current proposed token,
    and the list of occurrences, then prompts for one of five actions:

    * ``y`` — accept the proposal as-is.
    * ``e`` — edit the token before accepting. Prompts for a replacement
      (must contain ``{{...}}`` — reject and re-prompt otherwise).
    * ``s`` — skip this proposal (remove from the plan).
    * ``S`` — skip AND add the literal to
      ``.ships/hardcoded_name.exceptions.json`` so future runs don't
      re-propose it.
    * ``q`` — quit without processing any more proposals. Unreviewed
      proposals are left in the plan file as-is.

    The plan on disk is rewritten to reflect the operator's decisions.

    Args:
        project_dir:   SHIPS project directory (parent of ``.ships/``).
        input_stream:  File-like source of input lines. Defaults to
                       ``sys.stdin``. Tests inject :class:`io.StringIO`.
        output_stream: File-like sink for prompts and messages.
                       Defaults to ``sys.stdout``.

    Returns:
        :class:`ReviewResult` with per-action counts and a
        ``quit_early`` flag.
    """
    import sys

    inp = input_stream if input_stream is not None else sys.stdin
    out = output_stream if output_stream is not None else sys.stdout

    review = ReviewResult()

    plan = _load_plan(project_dir)
    if plan is None:
        out.write(
            f"no plan file to review ({_PLAN_RELATIVE_PATH} missing or malformed)\n"
        )
        return review

    proposals = list(plan.get("proposals", []))
    keep: list[dict] = []
    total = len(proposals)

    def _readline() -> str:
        out.flush()
        return inp.readline().rstrip("\n")

    for idx, proposal in enumerate(proposals, start=1):
        if review.quit_early:
            keep.append(proposal)
            continue

        literal = proposal.get("literal", "")
        token = proposal.get("token", "")
        occurrences = proposal.get("occurrences", [])

        out.write(
            f"\n[{idx}/{total}] literal: {literal!r}\n"
            f"        token:   {token!r}\n"
            f"        {len(occurrences)} occurrence(s)\n"
        )
        for occ in occurrences[:5]:
            out.write(f"          {occ.get('file')}:{occ.get('line')}\n")
        if len(occurrences) > 5:
            out.write(f"          ... {len(occurrences) - 5} more\n")

        while True:
            out.write("  [y] accept  [e] edit  [s] skip  [S] skip-all  [q] quit: ")
            choice = _readline().strip()
            if choice in {"y", "e", "s", "S", "q"}:
                break
            out.write("  ! unrecognised — pick one of y/e/s/S/q\n")

        if choice == "y":
            keep.append(proposal)
            review.accepted += 1
        elif choice == "e":
            while True:
                out.write(f"    new token (blank to keep {token!r}): ")
                new_token = _readline().strip()
                if not new_token:
                    keep.append(proposal)
                    review.accepted += 1
                    break
                if "{{" in new_token and "}}" in new_token:
                    edited = dict(proposal)
                    edited["token"] = new_token
                    keep.append(edited)
                    review.edited += 1
                    break
                out.write("    ! token must contain '{{...}}'\n")
        elif choice == "s":
            review.skipped += 1
        elif choice == "S":
            review.skipped += 1
            review.skipped_all.append(literal)
        elif choice == "q":
            review.quit_early = True
            keep.append(proposal)

    # Write plan back with the operator's decisions applied.
    if review.skipped_all:
        _write_exceptions(project_dir, review.skipped_all)
    if keep:
        _write_plan(project_dir, keep)
    else:
        # Every proposal was skipped — remove the empty plan so the
        # next run starts a fresh cycle.
        try:
            os.remove(_plan_path(project_dir))
        except OSError:
            pass

    return review


SPEC = register(
    FixerSpec(
        rule_id="hardcoded_name",
        apply=fix_hardcoded_name,
        default_on=False,
        write_scope="payload",
    )
)
