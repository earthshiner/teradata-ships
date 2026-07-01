"""``ships inspect`` is read-only — regression guard for #522.

The ``--fix-ddl-terminators`` and ``--fix-non-ascii`` flags used to make
``inspect`` write to ``payload/`` as a side effect of a verb whose name
promises "look, don't touch." #522 removed those flags; the auto-fixers
live under ``ships fix`` now.

This module asserts inspect never writes to ``payload/`` under any of
the argument combinations it accepts. Snapshots every file's content
+ mtime before, runs inspect, and diffs after — any change fails the
test with the offending path so a future regression surfaces exactly
which flag or code path reintroduced the write.

Note the ``--fix-grants`` exception: it still writes ``.grt`` files
under ``dcl/`` (also part of ``payload/`` in the SHIPS project
layout). It stays until #526 migrates grants into the fix registry.
The read-only test therefore uses ``--no-fix-grants`` on every run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    """A minimum SHIPS-shaped project seeded with content that would
    previously have tripped the removed inspect --fix-* flags."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)

    # (a) A DDL statement missing its terminator — used to be auto-fixed.
    _write(
        project / "payload/database/DDL/tables/Dev.T.tbl",
        "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
    )
    # (b) An em-dash — used to be substituted under --fix-non-ascii.
    _write(
        project / "payload/database/DDL/tables/Dev.U.tbl",
        "-- section — heading\n"
        "CREATE MULTISET TABLE Dev.U (Id INTEGER) PRIMARY INDEX (Id);\n",
    )
    return project


def _snapshot(project: Path) -> dict[Path, tuple[bytes, float]]:
    """Content + mtime for every file under payload/ so we can diff after."""
    out: dict[Path, tuple[bytes, float]] = {}
    for p in (project / "payload").rglob("*"):
        if p.is_file():
            out[p] = (p.read_bytes(), p.stat().st_mtime)
    return out


def _run_inspect(project: Path, *extra_args: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "td_release_packager",
        "inspect",
        "--project",
        str(project),
        # --no-fix-grants because --fix-grants stays until #526.
        "--no-fix-grants",
        *extra_args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _assert_no_payload_writes(before: dict, after: dict, invocation: str) -> None:
    added = sorted(str(p) for p in after.keys() - before.keys())
    removed = sorted(str(p) for p in before.keys() - after.keys())
    changed = sorted(
        str(p) for p in before.keys() & after.keys() if before[p] != after[p]
    )
    assert not added, f"{invocation} added files under payload/: {added}"
    assert not removed, f"{invocation} deleted files under payload/: {removed}"
    assert not changed, (
        f"{invocation} modified files under payload/ (content or mtime): {changed}"
    )


# ---------------------------------------------------------------
# Read-only under every arg combination
# ---------------------------------------------------------------


class TestInspectIsReadOnly:
    def test_bare_inspect_writes_nothing(self, tmp_path):
        project = _setup_project(tmp_path)
        before = _snapshot(project)
        _run_inspect(project)
        after = _snapshot(project)
        _assert_no_payload_writes(before, after, "ships inspect --no-fix-grants")

    def test_inspect_strict_writes_nothing(self, tmp_path):
        project = _setup_project(tmp_path)
        before = _snapshot(project)
        _run_inspect(project, "--strict")
        after = _snapshot(project)
        _assert_no_payload_writes(
            before, after, "ships inspect --strict --no-fix-grants"
        )

    def test_inspect_skip_grants_writes_nothing(self, tmp_path):
        project = _setup_project(tmp_path)
        before = _snapshot(project)
        _run_inspect(project, "--skip-grants")
        after = _snapshot(project)
        _assert_no_payload_writes(
            before, after, "ships inspect --skip-grants --no-fix-grants"
        )

    def test_removed_flags_reject_cleanly(self, tmp_path):
        """The old flags now fail with an argparse error, not a silent no-op.

        This guards against the "flag removed but callers still pass it" story
        where argparse would happily accept an unknown flag if someone
        re-introduced a shim by accident.
        """
        project = _setup_project(tmp_path)
        for removed_flag in (
            "--fix-ddl-terminators",
            "--no-fix-ddl-terminators",
            "--fix-non-ascii",
        ):
            result = _run_inspect(project, removed_flag)
            assert result.returncode != 0, (
                f"{removed_flag} was accepted by inspect — expected argparse error"
            )
            assert (
                "unrecognized arguments" in result.stderr
                or "invalid choice" in result.stderr
            ), f"{removed_flag} rejected with unexpected message: {result.stderr!r}"


# ---------------------------------------------------------------
# Sanity — the fixers still work when called through `ships fix`
# ---------------------------------------------------------------


class TestFixersStillWorkViaShipsFix:
    """Migration story: what inspect used to do, `ships fix` now does."""

    def test_ships_fix_still_fixes_ddl_terminator(self, tmp_path):
        project = _setup_project(tmp_path)
        f = project / "payload/database/DDL/tables/Dev.T.tbl"
        assert not f.read_text(encoding="utf-8").rstrip().endswith(";")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "fix",
                "--project",
                str(project),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert f.read_text(encoding="utf-8").endswith(");\n")

    def test_ships_fix_still_substitutes_non_ascii_opt_in(self, tmp_path):
        project = _setup_project(tmp_path)
        f = project / "payload/database/DDL/tables/Dev.U.tbl"
        assert "—" in f.read_text(encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "fix",
                "--project",
                str(project),
                "--rules",
                "non_ascii",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "—" not in f.read_text(encoding="utf-8")
