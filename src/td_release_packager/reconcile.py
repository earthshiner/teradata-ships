"""
Reconciliation utility for SHIPS harvested DDL trees.

Detects "twin" file pairs in `database/DDL/` where both a literal-named
form (e.g. ``MortgagePlatform_Domain_V.CustomerAddress.viw``) and its
tokenised counterpart (e.g. ``{{DOM_DATABASE_V}}.CustomerAddress.viw``)
exist side by side. These twins resolve to the same package destination
at build time and cause the builder to abort with a duplicate-path
error.

The reconciler walks the payload tree, identifies every twin pair using
``token_map.conf`` as the source of truth for literal-to-token mapping,
and prompts the user to resolve each pair interactively. The default
action keeps the tokenised file and deletes the literal-named one --
that is the right answer in the overwhelming majority of cases.

Both a human-readable summary (stdout) and a machine-readable JSON
audit record are always produced.

Exposed as ``td_release_packager harvest --reconcile`` -- this module
is invoked by ``cli.py`` and is independent of the normal harvest
pipeline.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import filecmp
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, TextIO, Tuple


# Reference IDs for user-facing errors.
# Format follows Rule 10: [ErrorCode] Human-readable summary. Suggested action.
ERR_NO_TOKEN_MAP = "E_NO_TOKEN_MAP"
ERR_NOT_INTERACTIVE = "E_NOT_INTERACTIVE"
ERR_THREE_WAY_COLLISION = "E_THREE_WAY_COLLISION"
ERR_DELETE_FAILED = "E_DELETE_FAILED"
ERR_DIFF_READ = "E_DIFF_READ"

# File extensions recognised as DDL artefacts in the harvested tree.
#
# NOTE: Do NOT extend this list here.  The canonical extension set lives in
# ``td_release_packager.discovery.DEFAULT_HARVEST_EXTENSIONS`` and is
# project-overridable via ``ships.yaml``'s ``discovery.extensions`` block.
# This local constant is kept only as a fast-path cache populated at
# import time from the canonical source, so that ``_iter_ddl_files`` does
# not re-resolve on every call inside a tight walk loop.
#
# If a new extension is needed, add it to ``discovery.DEFAULT_HARVEST_EXTENSIONS``.
def _build_ddl_extensions() -> frozenset:
    """Return the canonical harvest-extension set from ``discovery``.

    Resolved once at module import time and cached as ``_DDL_EXTENSIONS``.
    Falls back to an empty frozenset on import error so that the module
    remains loadable in minimal test environments.
    """
    try:
        from td_release_packager.discovery import resolve_harvest_extensions
        return resolve_harvest_extensions()
    except Exception:  # noqa: BLE001
        return frozenset()


_DDL_EXTENSIONS: frozenset = _build_ddl_extensions()

# Tokens conform to {{IDENTIFIER}} where IDENTIFIER is the same shape
# enforced by token_engine: alpha + alphanumerics/underscore/hyphen.
_TOKEN_PREFIX_RE = re.compile(r"^\{\{([A-Za-z_][A-Za-z0-9_-]*)\}\}$")


# --------------------------------------------------------------------------- #
#                              Public dataclasses                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TwinPair:
    """A literal/tokenised file pair resolving to the same package path.

    ``logical_path`` is the shared portion of the path (subdirectory
    plus the object-and-extension component), used to identify the
    pair in reports without leaking either prefix as canonical.
    """

    logical_path: str
    literal_source: Path
    tokenised_source: Path
    literal_size: int
    tokenised_size: int
    literal_mtime: _dt.datetime
    tokenised_mtime: _dt.datetime
    files_identical: bool


@dataclass
class TwinResolution:
    """The outcome of resolving a single twin pair."""

    pair: TwinPair
    action: str  # one of: kept_tokenised, kept_literal, skipped, error
    deleted_file: Optional[Path] = None
    error_message: Optional[str] = None


@dataclass
class ReconciliationResult:
    """Aggregate outcome for a reconciliation session."""

    session_id: str
    project_root: Path
    token_map_path: Path
    pairs: List[TwinPair] = field(default_factory=list)
    resolutions: List[TwinResolution] = field(default_factory=list)
    quit_early: bool = False

    @property
    def kept_tokenised_count(self) -> int:
        return sum(1 for r in self.resolutions if r.action == "kept_tokenised")

    @property
    def kept_literal_count(self) -> int:
        return sum(1 for r in self.resolutions if r.action == "kept_literal")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.resolutions if r.action == "skipped")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.resolutions if r.action == "error")


# --------------------------------------------------------------------------- #
#                          Filename parsing helpers                           #
# --------------------------------------------------------------------------- #


def _split_prefix(filename: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a DDL filename into ``(prefix, remainder)``.

    Returns ``(None, None)`` if the filename does not have the expected
    ``<PREFIX>.<rest>`` shape or if its extension is not a recognised
    DDL extension.

    The prefix is either a literal database name or a ``{{TOKEN}}``
    marker. The remainder is everything after the first dot, including
    the object name and extension.
    """
    if "." not in filename:
        return (None, None)
    prefix, _, rest = filename.partition(".")
    if not prefix or not rest:
        return (None, None)
    if not any(filename.endswith(ext) for ext in _DDL_EXTENSIONS):
        return (None, None)
    return (prefix, rest)


def _is_token_prefix(prefix: str) -> bool:
    """True if ``prefix`` matches the ``{{IDENTIFIER}}`` shape."""
    return bool(_TOKEN_PREFIX_RE.match(prefix))


# --------------------------------------------------------------------------- #
#                               Twin detection                                #
# --------------------------------------------------------------------------- #


def find_twin_pairs(
    payload_dir: Path,
    token_map: Dict[str, str],
) -> List[TwinPair]:
    """Walk ``payload_dir`` and return every literal/tokenised twin pair.

    Args:
        payload_dir: The harvested DDL tree root (typically
            ``<project>/database/DDL``).
        token_map: Literal-to-token mapping loaded from
            ``token_map.conf`` (e.g. ``{"MortgagePlatform_Domain_V":
            "{{DOM_DATABASE_V}}"}``).

    Returns:
        A list of ``TwinPair`` records, one per detected pair, sorted
        by ``logical_path`` for stable output.

    Raises:
        FileNotFoundError: if ``payload_dir`` does not exist.
        ValueError: if a three-way (or higher) collision is detected;
            the message lists every colliding file so the user can
            untangle manually.
    """
    if not payload_dir.exists():
        raise FileNotFoundError(f"Payload directory not found: {payload_dir}")
    if not payload_dir.is_dir():
        raise NotADirectoryError(f"Payload path is not a directory: {payload_dir}")

    # Group files by their (subdir, remainder) key. Each group should
    # contain at most two members: one literal-prefixed, one tokenised.
    # Anything more is a collision the user must resolve by hand.
    groups: Dict[Tuple[str, str], List[Path]] = {}

    for path in _iter_ddl_files(payload_dir):
        prefix, remainder = _split_prefix(path.name)
        if prefix is None or remainder is None:
            continue

        # Only pair files when the literal prefix has a known token,
        # OR the file already uses a token prefix. Anything else is
        # not a candidate for twinning under the current scope.
        if not (_is_token_prefix(prefix) or prefix in token_map):
            continue

        subdir = str(path.parent.relative_to(payload_dir)).replace("\\", "/")
        key = (subdir, remainder)
        groups.setdefault(key, []).append(path)

    pairs: List[TwinPair] = []
    for (subdir, remainder), members in sorted(groups.items()):
        if len(members) < 2:
            # Single file -- not a twin (it is either a clean tokenised
            # file or an orphaned literal; orphans are out of scope for
            # this iteration).
            continue
        if len(members) > 2:
            listing = "\n  ".join(str(p) for p in members)
            raise ValueError(
                f"[{ERR_THREE_WAY_COLLISION}] {len(members)} files resolve "
                f"to the same package path '{subdir}/{remainder}'. "
                "Reconciliation handles two-way twins only -- please "
                "resolve this manually by deleting all but one file.\n"
                f"  {listing}"
            )

        # Exactly two -- classify each member.
        literal: Optional[Path] = None
        tokenised: Optional[Path] = None
        for member in members:
            mprefix, _ = _split_prefix(member.name)
            if mprefix is None:
                continue
            if _is_token_prefix(mprefix):
                tokenised = member
            elif mprefix in token_map:
                literal = member

        if literal is None or tokenised is None:
            # Two files but not a literal/tokenised pair (e.g. two
            # tokens or two literals). Not a twin under our definition.
            continue

        pairs.append(
            _build_twin_pair(
                logical_path=f"{subdir}/{remainder}" if subdir != "." else remainder,
                literal=literal,
                tokenised=tokenised,
            )
        )

    return pairs


def _iter_ddl_files(payload_dir: Path):
    """Yield every DDL file under ``payload_dir``, skipping hidden dirs."""
    for path in payload_dir.rglob("*"):
        if not path.is_file():
            continue
        # Skip hidden / underscore-prefixed directories anywhere on the
        # path -- mirrors token_engine.scan_tokens_in_directory's rules.
        if any(
            part.startswith((".", "_"))
            for part in path.relative_to(payload_dir).parts[:-1]
        ):
            continue
        if not any(path.name.endswith(ext) for ext in _DDL_EXTENSIONS):
            continue
        yield path


def _build_twin_pair(
    logical_path: str,
    literal: Path,
    tokenised: Path,
) -> TwinPair:
    """Construct a ``TwinPair`` with stat metadata captured up front."""
    literal_stat = literal.stat()
    tokenised_stat = tokenised.stat()
    return TwinPair(
        logical_path=logical_path,
        literal_source=literal,
        tokenised_source=tokenised,
        literal_size=literal_stat.st_size,
        tokenised_size=tokenised_stat.st_size,
        literal_mtime=_dt.datetime.fromtimestamp(
            literal_stat.st_mtime, tz=_dt.timezone.utc
        ),
        tokenised_mtime=_dt.datetime.fromtimestamp(
            tokenised_stat.st_mtime, tz=_dt.timezone.utc
        ),
        files_identical=filecmp.cmp(literal, tokenised, shallow=False),
    )


# --------------------------------------------------------------------------- #
#                              Diff rendering                                 #
# --------------------------------------------------------------------------- #


def render_diff(pair: TwinPair, n_context: int = 3) -> str:
    """Render a unified diff between the two members of ``pair``.

    Returns a printable string. On read error, returns a structured
    error message rather than raising -- the diff is a user
    convenience and should not abort the session.
    """
    try:
        literal_text = pair.literal_source.read_text(encoding="utf-8", errors="replace")
        tokenised_text = pair.tokenised_source.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return (
            f"[{ERR_DIFF_READ}] Could not read files for diff: {exc}. "
            f"Check file permissions and try again."
        )

    diff = difflib.unified_diff(
        literal_text.splitlines(keepends=True),
        tokenised_text.splitlines(keepends=True),
        fromfile=pair.literal_source.name,
        tofile=pair.tokenised_source.name,
        n=n_context,
    )
    rendered = "".join(diff)
    return rendered if rendered else "(files are byte-identical)"


# --------------------------------------------------------------------------- #
#                         Interactive resolution flow                         #
# --------------------------------------------------------------------------- #

# Action codes returned by _prompt_user.
_ACTION_KEEP_TOKENISED = "kept_tokenised"
_ACTION_KEEP_LITERAL = "kept_literal"
_ACTION_SKIP = "skipped"
_ACTION_QUIT = "quit"

_PROMPT = (
    "Action? [k]eep tokenised (default) / keep [l]iteral / [s]kip / [d]iff / [q]uit: "
)


def _prompt_user(
    pair: TwinPair,
    *,
    in_stream: TextIO = sys.stdin,
    out_stream: TextIO = sys.stdout,
) -> str:
    """Prompt the user for a decision on ``pair``.

    Returns one of ``_ACTION_KEEP_TOKENISED``, ``_ACTION_KEEP_LITERAL``,
    ``_ACTION_SKIP``, ``_ACTION_QUIT``. Diff requests are handled
    internally and re-prompt.
    """
    while True:
        out_stream.write(_PROMPT)
        out_stream.flush()
        raw = in_stream.readline()
        if not raw:
            # EOF on stdin -- treat as quit so the JSON gets written.
            return _ACTION_QUIT
        choice = raw.strip().lower()
        if choice == "":
            return _ACTION_KEEP_TOKENISED
        if choice == "k":
            return _ACTION_KEEP_TOKENISED
        if choice == "l":
            return _ACTION_KEEP_LITERAL
        if choice == "s":
            return _ACTION_SKIP
        if choice == "q":
            return _ACTION_QUIT
        if choice == "d":
            out_stream.write("\n")
            out_stream.write(render_diff(pair))
            out_stream.write("\n")
            out_stream.flush()
            continue
        out_stream.write(f"Unknown option: {choice!r}. Try again.\n")
        out_stream.flush()


def _format_pair_header(pair: TwinPair, index: int, total: int) -> str:
    """Render the human-readable header that precedes each prompt."""
    identical_note = "  [content identical]" if pair.files_identical else ""
    return (
        f"\nTwin {index} of {total}: {pair.logical_path}{identical_note}\n"
        f"  Literal:    {pair.literal_source.name}  "
        f"({pair.literal_size:,} bytes, modified "
        f"{pair.literal_mtime:%Y-%m-%d %H:%M:%S} UTC)\n"
        f"  Tokenised:  {pair.tokenised_source.name}  "
        f"({pair.tokenised_size:,} bytes, modified "
        f"{pair.tokenised_mtime:%Y-%m-%d %H:%M:%S} UTC)\n"
    )


def _delete_file(path: Path) -> Tuple[bool, Optional[str]]:
    """Delete ``path``, returning (success, error_message)."""
    try:
        path.unlink()
        return (True, None)
    except OSError as exc:
        return (
            False,
            f"[{ERR_DELETE_FAILED}] Could not delete {path}: {exc}. "
            f"Check file permissions and any open handles.",
        )


def _apply_action(
    pair: TwinPair,
    action: str,
) -> TwinResolution:
    """Carry out the user's chosen action on a twin pair."""
    if action == _ACTION_KEEP_TOKENISED:
        ok, err = _delete_file(pair.literal_source)
        if ok:
            return TwinResolution(
                pair=pair,
                action=_ACTION_KEEP_TOKENISED,
                deleted_file=pair.literal_source,
            )
        return TwinResolution(pair=pair, action="error", error_message=err)

    if action == _ACTION_KEEP_LITERAL:
        ok, err = _delete_file(pair.tokenised_source)
        if ok:
            return TwinResolution(
                pair=pair,
                action=_ACTION_KEEP_LITERAL,
                deleted_file=pair.tokenised_source,
            )
        return TwinResolution(pair=pair, action="error", error_message=err)

    # Skipped (quit handled by caller before reaching here).
    return TwinResolution(pair=pair, action=_ACTION_SKIP)


# --------------------------------------------------------------------------- #
#                          Top-level orchestration                            #
# --------------------------------------------------------------------------- #


def run_interactive_reconciliation(
    *,
    project_root: Path,
    payload_dir: Path,
    token_map: Dict[str, str],
    token_map_path: Path,
    json_output_path: Path,
    in_stream: TextIO = sys.stdin,
    out_stream: TextIO = sys.stdout,
    require_tty: bool = True,
) -> ReconciliationResult:
    """Drive the interactive reconciliation session end to end.

    The session walks ``payload_dir``, prompts for each twin pair,
    applies the chosen action, and writes a JSON audit record to
    ``json_output_path``. The summary banner is left to the caller --
    this function returns the result dataclass.

    Args:
        project_root: The SHIPS project root (used in the JSON record).
        payload_dir: Tree to scan, typically ``project_root /
            "database" / "DDL"``.
        token_map: Literal-to-token mapping.
        token_map_path: Path to ``token_map.conf`` (recorded in JSON).
        json_output_path: Where to write the audit JSON. Parent dir is
            created if missing.
        in_stream / out_stream: Injected for testability.
        require_tty: If True (default), refuse to run unless
            ``in_stream`` is a TTY.

    Raises:
        RuntimeError: if ``require_tty`` is True and ``in_stream`` is
            not a TTY.
    """
    # Fail fast on non-interactive stdin -- this is an interactive tool
    # by design; CI use cases need a future --reconcile-strategy flag.
    if require_tty and not _is_tty(in_stream):
        raise RuntimeError(
            f"[{ERR_NOT_INTERACTIVE}] --reconcile requires an interactive "
            "terminal. Run from a real shell, or wait for the planned "
            "--reconcile-strategy flag for non-interactive use."
        )

    session_id = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = ReconciliationResult(
        session_id=session_id,
        project_root=project_root,
        token_map_path=token_map_path,
    )

    pairs = find_twin_pairs(payload_dir, token_map)
    result.pairs = pairs

    if not pairs:
        out_stream.write("\nNo twin pairs found. Project tree is clean.\n")
        _write_json(result, json_output_path)
        return result

    out_stream.write(f"\nFound {len(pairs)} twin pair(s) in {payload_dir}.\n")

    for index, pair in enumerate(pairs, start=1):
        out_stream.write(_format_pair_header(pair, index, len(pairs)))
        out_stream.flush()

        try:
            action = _prompt_user(pair, in_stream=in_stream, out_stream=out_stream)
        except KeyboardInterrupt:
            out_stream.write("\nInterrupted -- abandoning remaining pairs.\n")
            result.quit_early = True
            break

        if action == _ACTION_QUIT:
            result.quit_early = True
            break

        resolution = _apply_action(pair, action)
        result.resolutions.append(resolution)

        if resolution.action == "error":
            out_stream.write(f"  ✗ {resolution.error_message}\n")
        elif resolution.deleted_file is not None:
            out_stream.write(f"  ✓ Deleted: {resolution.deleted_file}\n")
        else:
            out_stream.write("  - Skipped (both files retained).\n")
        out_stream.flush()

    _write_json(result, json_output_path)
    return result


def _is_tty(stream: TextIO) -> bool:
    """Best-effort TTY detection -- some streams don't implement isatty."""
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


# --------------------------------------------------------------------------- #
#                               Reporting                                     #
# --------------------------------------------------------------------------- #


def format_summary_banner(result: ReconciliationResult) -> str:
    """Build the post-session human-readable summary banner."""
    width = 64
    bar = "=" * width
    lines = [
        bar,
        "  SHIPS Harvest -- Reconciliation Summary",
        bar,
        f"  Twin pairs found:    {len(result.pairs)}",
        f"  Tokenised kept:      {result.kept_tokenised_count}",
        f"  Literal kept:        {result.kept_literal_count}",
        f"  Skipped:             {result.skipped_count}",
        f"  Errors:              {result.error_count}",
        f"  Quit early:          {'yes' if result.quit_early else 'no'}",
        bar,
    ]
    return "\n".join(lines) + "\n"


def _write_json(result: ReconciliationResult, output_path: Path) -> None:
    """Write the machine-readable audit record."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": result.session_id,
        "project_root": str(result.project_root),
        "token_map": str(result.token_map_path),
        "quit_early": result.quit_early,
        "summary": {
            "twin_pairs_found": len(result.pairs),
            "kept_tokenised": result.kept_tokenised_count,
            "kept_literal": result.kept_literal_count,
            "skipped": result.skipped_count,
            "errors": result.error_count,
        },
        "twin_pairs": [_pair_to_dict(p) for p in result.pairs],
        "resolutions": [_resolution_to_dict(r) for r in result.resolutions],
    }
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False),
        encoding="utf-8",
    )


def _pair_to_dict(pair: TwinPair) -> dict:
    return {
        "logical_path": pair.logical_path,
        "literal_source": str(pair.literal_source),
        "tokenised_source": str(pair.tokenised_source),
        "literal_size": pair.literal_size,
        "tokenised_size": pair.tokenised_size,
        "literal_mtime": pair.literal_mtime.isoformat(),
        "tokenised_mtime": pair.tokenised_mtime.isoformat(),
        "files_identical": pair.files_identical,
    }


def _resolution_to_dict(res: TwinResolution) -> dict:
    return {
        "logical_path": res.pair.logical_path,
        "action": res.action,
        "deleted_file": str(res.deleted_file) if res.deleted_file else None,
        "error_message": res.error_message,
    }
