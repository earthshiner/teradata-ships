"""
test_package_no_collision.py — G2 + G3 golden-regression for issue #365.

G2 — N tokenised payload files detokenise to N release files, with
no collision. The bijection guard in builder._copy_payload_files
catches the case where a non-injective token_map would collapse
distinct payload files onto a single release path.

G3 — for every release file, the qualifier parsed from the
**filename** equals the qualifier parsed from the **body**. Filename
and content are driven by the same env substitution, so they cannot
drift.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from td_release_packager.builder import build_package
from td_release_packager.models import BuildConfig


# Six tables in one tokenised database. Same shape as G1 but built
# into the Package path: harvest already produced the tokenised
# payload, Package must detokenise it into a release tree.
_OBJECTS = ["Customer", "Account", "Branch", "Product", "Transaction", "Channel"]


def _seed_payload(project: Path) -> None:
    """Write N tokenised .tbl files into the project's payload."""
    tables_dir = project / "payload" / "database" / "DDL" / "tables"
    for obj in _OBJECTS:
        ddl = (
            f"CREATE MULTISET TABLE {{{{STD_DATABASE}}}}.{obj} (\n"
            f"    {obj}_Id INTEGER NOT NULL,\n"
            "    label VARCHAR(100)\n"
            f") PRIMARY INDEX ({obj}_Id);\n"
        )
        (tables_dir / f"{{{{STD_DATABASE}}}}.{obj}.tbl").write_text(
            ddl, encoding="utf-8"
        )


def _build_and_extract_tbl_files(
    tmp_path: Path, project: Path, env_config: Path
) -> Dict[str, str]:
    """Build the package and return ``{filename: body}`` for every
    ``.tbl`` file in the resulting archive."""
    config = BuildConfig(
        source_dir=str(project),
        environment="DEV",
        package_name="Pkg",
        env_config_file=str(env_config),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )
    ((archive_path, _manifest), _companion) = build_package(config)

    tbl_bodies: Dict[str, str] = {}
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            if not name.endswith(".tbl"):
                continue
            base = name.rsplit("/", 1)[-1]
            tbl_bodies[base] = archive.read(name).decode("utf-8")
    return tbl_bodies


# ===========================================================================
# G2 — bijection between payload and release files
# ===========================================================================


def test_n_payload_files_yield_n_release_files(
    tmp_path: Path, tmp_project: Path, sample_env_config_file: Path
) -> None:
    """G2 — *N* tokenised payload files → *N* detokenised release
    files. A collapse leaves fewer."""
    _seed_payload(tmp_project)
    tbl_bodies = _build_and_extract_tbl_files(
        tmp_path, tmp_project, sample_env_config_file
    )

    assert len(tbl_bodies) == len(_OBJECTS), (
        f"Expected {len(_OBJECTS)} release files, got "
        f"{len(tbl_bodies)}: {sorted(tbl_bodies)}"
    )


def test_bijection_guard_rejects_non_injective_token_map(
    tmp_path: Path, tmp_project: Path
) -> None:
    """G2 guard — two distinct tokens that resolve to the same literal
    cause two distinct payload files to detokenise onto the same
    release path. The bijection guard must surface a ValueError, not
    silently overwrite."""
    tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"
    # Two distinct tokens, two distinct payload filenames, same
    # downstream literal. Same object name on each side so the only
    # axis of distinction is the qualifier token.
    (tables_dir / "{{TOK_A}}.Customer.tbl").write_text(
        "CREATE MULTISET TABLE {{TOK_A}}.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    (tables_dir / "{{TOK_B}}.Customer.tbl").write_text(
        "CREATE MULTISET TABLE {{TOK_B}}.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )

    # Env config maps both tokens to the same literal — a non-injective
    # token_map is the canonical shape of the bug this guard catches.
    env_config = tmp_path / "BIJECT.conf"
    env_config.write_text(
        "SHIPS_ENV=BIJECT\nTOK_A=SAME_DB\nTOK_B=SAME_DB\n",
        encoding="utf-8",
    )

    config = BuildConfig(
        source_dir=str(tmp_project),
        environment="BIJECT",
        package_name="Pkg",
        env_config_file=str(env_config),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )

    with pytest.raises(ValueError, match="detokenisation collision"):
        build_package(config)


# ===========================================================================
# G3 — qualifier parsed from filename equals qualifier parsed from body
# ===========================================================================


_FILENAME_QUALIFIER_RE = re.compile(r"^([A-Za-z_][\w]*)\.")
_BODY_QUALIFIER_RE = re.compile(
    r"CREATE\s+(?:MULTISET\s+|SET\s+)?TABLE\s+([A-Za-z_][\w]*)\.",
    re.IGNORECASE,
)


def test_release_filename_qualifier_matches_body_qualifier(
    tmp_path: Path, tmp_project: Path, sample_env_config_file: Path
) -> None:
    """G3 — for each release file, qualifier parsed from the filename
    equals qualifier parsed from the body. Name↔body lock-step."""
    _seed_payload(tmp_project)
    tbl_bodies = _build_and_extract_tbl_files(
        tmp_path, tmp_project, sample_env_config_file
    )

    assert tbl_bodies, "Test did not produce any release .tbl files"

    drifts: List[str] = []
    for filename, body in tbl_bodies.items():
        fn_match = _FILENAME_QUALIFIER_RE.match(filename)
        body_match = _BODY_QUALIFIER_RE.search(body)
        assert fn_match, f"Filename {filename!r} has no parseable qualifier"
        assert body_match, f"Body of {filename!r} has no parseable qualifier"
        fn_q = fn_match.group(1)
        body_q = body_match.group(1)
        if fn_q != body_q:
            drifts.append(f"{filename}: filename={fn_q!r}, body={body_q!r}")

    assert drifts == [], "Name↔body qualifier drift: " + "; ".join(drifts)
