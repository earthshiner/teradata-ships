"""
test_harvest_volatile_routing.py — Volatile-table-bearing files stay intact (#509).

A file containing ``CREATE [MULTISET|SET] VOLATILE TABLE`` is an ordered
execution script — the volatile table lives in the session's spool and is
dropped at session end, so any consumer ``INSERT … FROM vt_<name>`` MUST
run in the same session. Splitting per-statement breaks the script
semantically.

The pre-pass routes such files through ``_place_ordered_sql`` (same path
the existing GRANT/REVOKE/non-DCL choreography uses). These tests pin
that decision plus the regression check that ordinary multi-CREATE-TABLE
files still split.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.ingest import ingest_directory


def _scaffold_project(root: Path) -> Path:
    payload = root / "payload" / "database"
    for sub in (
        "DDL/tables",
        "DDL/views",
        "DML",
        "DCL/inter_db",
        "DCL/roles",
        "pre-requisites/databases",
    ):
        (payload / sub).mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / ".ships").mkdir(parents=True, exist_ok=True)
    (root / ".ships" / ".build_counter").write_text("0", encoding="utf-8")
    return root


def _write(source: Path, name: str, body: str) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / name).write_text(body, encoding="utf-8")


def test_file_with_volatile_table_is_placed_as_ordered_sql(tmp_path: Path) -> None:
    """A file containing CREATE VOLATILE TABLE + a consumer INSERT must
    NOT be split — it lands as a single ``*.ordered.osql`` artefact so
    the deployer runs every statement in one session."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "etl-load.sql",
        # Mirrors the user's CargoIntel 03-etl-load.sql shape: GRANT,
        # volatile-table CREATE + INSERT, then a persistent INSERT that
        # reads from the volatile table.
        "GRANT SELECT ON A_T TO B_T;\n"
        "\n"
        "CREATE VOLATILE TABLE vt_temp (id BIGINT NOT NULL)\n"
        "ON COMMIT PRESERVE ROWS;\n"
        "\n"
        "INSERT INTO vt_temp (id) SELECT id FROM A_T.source_table;\n"
        "\n"
        "DELETE FROM B_T.target_table;\n"
        "INSERT INTO B_T.target_table SELECT id FROM vt_temp;\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    # The file ends up under DML/ as one .ordered.osql artefact — NOT
    # split into a .tbl + .dml + .grt soup.
    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert len(osql_files) == 1, [str(p) for p in osql_files]
    body = osql_files[0].read_text(encoding="utf-8")
    # Every statement survived in the original ordering.
    assert "GRANT SELECT" in body
    assert "CREATE VOLATILE TABLE vt_temp" in body
    assert "INSERT INTO vt_temp" in body
    assert "DELETE FROM B_T.target_table" in body
    assert "INSERT INTO B_T.target_table" in body
    # And the CREATE comes BEFORE the consumer INSERT — ordering preserved.
    assert body.index("CREATE VOLATILE TABLE") < body.index(
        "INSERT INTO B_T.target_table"
    )


def test_file_with_multiset_volatile_also_routes_to_ordered_sql(
    tmp_path: Path,
) -> None:
    """CREATE MULTISET VOLATILE TABLE (the common Teradata form) is
    detected the same way."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "etl-load.sql",
        "CREATE MULTISET VOLATILE TABLE vt_x (id BIGINT)\n"
        "ON COMMIT PRESERVE ROWS;\n"
        "\n"
        "INSERT INTO Db.target SELECT id FROM vt_x;\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert len(osql_files) == 1


def test_file_with_no_volatile_table_still_splits_per_object(
    tmp_path: Path,
) -> None:
    """Regression: a plain multi-CREATE-TABLE file (no volatile) splits
    into atomic eponymous files — the volatile-table routing must NOT
    trip on ordinary DDL."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "reference.sql",
        "CREATE MULTISET TABLE A_T.Customer (id BIGINT) PRIMARY INDEX (id);\n"
        "\n"
        "CREATE MULTISET TABLE B_T.Order (id BIGINT) PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    # No ordered-sql artefact — the file split into per-object files.
    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert osql_files == [], [str(p) for p in osql_files]
    tbl_files = sorted(p.name for p in (project / "payload").rglob("*.tbl"))
    assert "A_T.Customer.tbl" in tbl_files
    assert "B_T.Order.tbl" in tbl_files


def test_volatile_table_inside_string_literal_does_not_route(
    tmp_path: Path,
) -> None:
    """Defensive: a `CREATE VOLATILE TABLE` substring INSIDE a SQL string
    literal is data, not code, and must NOT trigger the routing. The
    detection runs on comment-stripped (string-literal-blanked) content
    via ``_strip_comments`` to make this guarantee."""
    source = tmp_path / "source"
    project = _scaffold_project(tmp_path / "project")
    _write(
        source,
        "log_record.sql",
        # Two normal CREATE TABLEs, with a string literal that contains
        # the phrase `CREATE VOLATILE TABLE`. The file must split as if
        # the literal weren't there.
        "CREATE MULTISET TABLE A_T.Customer (id BIGINT) PRIMARY INDEX (id);\n"
        "\n"
        "COMMENT ON TABLE A_T.Customer IS\n"
        "'see ETL doc: CREATE VOLATILE TABLE step';\n"
        "\n"
        "CREATE MULTISET TABLE B_T.Order (id BIGINT) PRIMARY INDEX (id);\n",
    )

    result = ingest_directory(str(source), str(project), detect_tokens=False)
    assert result.errors == []

    osql_files = list((project / "payload").rglob("*.ordered.osql"))
    assert osql_files == [], [str(p) for p in osql_files]
