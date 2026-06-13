"""Corpus-driven tests for SqlReferenceExtractor implementations.

Each subdirectory of ``src/tests/sql_reference_corpus/`` is a single
case: ``sql.sql`` is the input, ``expected.json`` describes the four
extractor outputs. Every implementation registered in ``_EXTRACTORS``
must produce the documented results — that is how the AST migration
(ADR 0015) gets verified incrementally: add an extractor, watch the
corpus, narrow the diff, declare it the new default.

Phase 1 registers only the regex implementation. Phase 2 adds the
SqlGlot implementation here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

import pytest

from td_release_packager.infer_grants import strip_sql_comments
from td_release_packager.sql_reference_extractor import (
    RegexSqlReferenceExtractor,
    SqlReferenceExtractor,
    StatementOwner,
)
from td_release_packager.sql_reference_extractor_sqlglot import (
    SqlGlotSqlReferenceExtractor,
    is_available as _sqlglot_available,
)


CORPUS_DIR = Path(__file__).parent / "sql_reference_corpus"


# (label, factory) — every registered extractor must produce the
# documented results for every corpus entry.
_EXTRACTORS: list[Tuple[str, callable]] = [
    ("regex", RegexSqlReferenceExtractor),
]
if _sqlglot_available():
    _EXTRACTORS.append(("sqlglot", SqlGlotSqlReferenceExtractor))


def _discover_cases() -> list[Path]:
    if not CORPUS_DIR.is_dir():
        return []
    return sorted(
        d
        for d in CORPUS_DIR.iterdir()
        if d.is_dir() and (d / "sql.sql").is_file() and (d / "expected.json").is_file()
    )


def _case_id(path: Path) -> str:
    return path.name


def _flatten_owner(owner) -> dict | None:
    if owner is None:
        return None
    assert isinstance(owner, StatementOwner)
    return {
        "database": owner.database,
        "object_name": owner.object_name,
        "object_type": owner.object_type,
    }


def _flatten_targets(targets: dict) -> list[dict]:
    rows = []
    for ref, privs in targets.items():
        rows.append(
            {
                "database": ref.database,
                "object_name": ref.object_name,
                "privileges": sorted(privs),
            }
        )
    rows.sort(key=lambda r: (r["database"], r["object_name"]))
    return rows


def _flatten_read_sources(sources: set) -> list[dict]:
    rows = [{"database": s.database, "object_name": s.object_name} for s in sources]
    rows.sort(key=lambda r: (r["database"], r["object_name"]))
    return rows


@pytest.fixture(params=_EXTRACTORS, ids=[label for label, _ in _EXTRACTORS])
def extractor(request) -> SqlReferenceExtractor:
    _label, factory = request.param
    return factory()


@pytest.mark.parametrize(
    "case_dir",
    _discover_cases()
    or [pytest.param(None, marks=pytest.mark.skip(reason="no corpus entries"))],
    ids=lambda c: _case_id(c) if c is not None else "no-cases",
)
def test_corpus_case(extractor: SqlReferenceExtractor, case_dir: Path):
    sql = strip_sql_comments((case_dir / "sql.sql").read_text(encoding="utf-8"))
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))

    actual_owner = _flatten_owner(extractor.extract_statement_owner(sql))
    expected_owner = expected.get("owner")
    assert actual_owner == expected_owner, (
        f"owner mismatch on {case_dir.name}: expected {expected_owner}, got {actual_owner}"
    )

    actual_reads = _flatten_read_sources(extractor.extract_read_sources(sql))
    expected_reads = sorted(
        expected.get("read_sources", []),
        key=lambda r: (r["database"], r["object_name"]),
    )
    assert actual_reads == expected_reads, f"read_sources mismatch on {case_dir.name}"

    actual_writes = _flatten_targets(extractor.extract_write_targets(sql))
    expected_writes = sorted(
        expected.get("write_targets", []),
        key=lambda r: (r["database"], r["object_name"]),
    )
    assert actual_writes == expected_writes, (
        f"write_targets mismatch on {case_dir.name}"
    )

    actual_calls = _flatten_targets(extractor.extract_call_targets(sql))
    expected_calls = sorted(
        expected.get("call_targets", []),
        key=lambda r: (r["database"], r["object_name"]),
    )
    assert actual_calls == expected_calls, f"call_targets mismatch on {case_dir.name}"
