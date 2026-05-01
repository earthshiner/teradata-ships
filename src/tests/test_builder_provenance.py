"""
Integration test — builder writes a v2 ProvenanceDocument.

Drives _copy_payload with synthetic source files representing the
scenarios that trigger different stage statuses:

    - Eponymous rename (DDL has qualified Database.Object name)
    - Token in filename (e.g. {{DOM_DATABASE_T}}.db)
    - No-op (filename already eponymous, no tokens)
    - Binary file (UnicodeDecodeError path)

Asserts the resulting ProvenanceDocument has:
    - One entry per source file (no drops, no dupes)
    - Each entry has all four canonical stages in order
    - Stage statuses match the scenario
    - Final paths match the actual on-disk package layout
"""

import os
import shutil
import sys
import tempfile

import pytest

# Make sure the patched builder is importable
_REPO_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from td_release_packager.builder import _copy_payload  # noqa: E402
from td_release_packager.models import SOURCE_DIR_MAP  # noqa: E402
from ddl_deployer.provenance import (  # noqa: E402
    STAGE_ORDER,
    Status,
)


# -------------------------------------------------------------------
# Synthetic source DDL
# -------------------------------------------------------------------

# 1. Eponymous-renameable: filename does not match qualified name in DDL
TABLE_DDL = """\
CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Mortgage
(
      Mortgage_Id INTEGER NOT NULL
    , Applicant_Name VARCHAR(200)
    , Loan_Amount DECIMAL(15,2)
)
PRIMARY INDEX (Mortgage_Id)
;
"""

# 2. Token-in-filename: filename uses {{TOKEN}} which the eponymous
#    rename can't resolve (no qualified Database.Object in DDL).
DATABASE_DDL = """\
CREATE DATABASE {{DOM_DATABASE_T}} AS PERMANENT = 1000000000;
"""

# 3. Already eponymous, no tokens — should be no_op for both
#    eponymous and token_resolved stages.
ALREADY_EPONYMOUS_DDL = """\
CREATE VIEW MyTestDb_V.Customer AS
LOCKING ROW FOR ACCESS
SELECT customer_id, customer_name
FROM MyTestDb_T.Customer
;
"""


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _write(path: str, content) -> None:
    """Write content to a path, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    encoding = None if isinstance(content, bytes) else "utf-8"
    with open(path, mode, encoding=encoding) as f:
        f.write(content)


def _phase_folder() -> str:
    """
    Return a folder name the builder's phase mapper recognises.

    SOURCE_DIR_MAP (in td_release_packager.models) is the canonical
    map from source-tree folder names to deployment phases. Picking
    a key from it at runtime makes the test portable across folder-
    naming changes — whatever the project considers a valid source
    folder, the test will use one. We pick the first key
    deterministically so failures are reproducible.
    """
    if not SOURCE_DIR_MAP:
        pytest.skip("SOURCE_DIR_MAP is empty — cannot construct test paths")
    return sorted(SOURCE_DIR_MAP.keys())[0]


@pytest.fixture
def workspace():
    """Provide a temporary source/package workspace."""
    base = tempfile.mkdtemp(prefix="ships_provenance_test_")
    src = os.path.join(base, "source")
    pkg = os.path.join(base, "package")
    os.makedirs(src)
    os.makedirs(pkg)
    yield (src, pkg)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def phase_folder():
    """A source folder name the builder will accept."""
    return _phase_folder()


@pytest.fixture
def token_values():
    """Match what a real properties file would supply."""
    return {
        "DOM_DATABASE_T": "P_MP_DOM_T",
        "DOM_DATABASE_V": "P_MP_DOM_V",
    }


# -------------------------------------------------------------------
# Scenarios
# -------------------------------------------------------------------


class TestProvenanceFromBuilder:
    """End-to-end: feed the builder synthetic files, verify the
    ProvenanceDocument it produces."""

    def test_eponymous_renamed_file(self, workspace, phase_folder, token_values):
        """Filename gets rewritten from DDL — eponymous=applied,
        token_resolved=no_op."""
        src, pkg = workspace
        # Source file with non-eponymous name; DDL declares the
        # qualified name {{DOM_DATABASE_T}}.Mortgage which token-
        # subs to P_MP_DOM_T.Mortgage.
        _write(
            os.path.join(src, phase_folder, "MortgagePlatform_Domain_Mortgage.tbl"),
            TABLE_DDL,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        assert len(doc.entries) == 1
        # Final path keyed by phase + filename — phase is determined
        # by builder logic; just verify the chain shape regardless
        chain = list(doc.entries.values())[0]
        assert chain.is_complete()
        assert [s.stage for s in chain.stages] == STAGE_ORDER

        # Stage assertions
        source, eponymous, token_resolved, package = chain.stages
        assert source.status == Status.APPLIED
        assert eponymous.status == Status.APPLIED
        assert "Renamed from DDL" in eponymous.note
        assert token_resolved.status == Status.NO_OP
        assert package.status == Status.APPLIED

        # The eponymous stage should produce the resolved name
        # (P_MP_DOM_T.Mortgage.tbl) — not the original name
        assert "P_MP_DOM_T.Mortgage" in eponymous.path
        assert "MortgagePlatform_Domain" not in eponymous.path

    def test_token_in_filename(self, workspace, phase_folder, token_values):
        """Filename uses {{TOKEN}} — the token is resolved before the
        file lands in the package. Either eponymous or token_resolved
        may do the work depending on whether the DDL is eponymously
        nameable; we assert the contract (no token survives), not
        which stage handled it."""
        src, pkg = workspace
        _write(
            os.path.join(src, phase_folder, "{{DOM_DATABASE_T}}.db"),
            DATABASE_DDL,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        assert len(doc.entries) == 1
        chain = list(doc.entries.values())[0]
        source, eponymous, token_resolved, package = chain.stages

        assert source.status == Status.APPLIED

        # At least one of the two transformation stages must have
        # been APPLIED — together they ensure {{TOKEN}} markers in
        # the source filename are resolved before packaging.
        assert (
            eponymous.status == Status.APPLIED
            or token_resolved.status == Status.APPLIED
        ), (
            f"Neither eponymous ({eponymous.status.value}) nor "
            f"token_resolved ({token_resolved.status.value}) was "
            f"APPLIED — the {{{{TOKEN}}}} in the filename was not "
            f"resolved by any stage."
        )

        # The package path must be clean — no surviving {{TOKEN}}
        # markers, and the resolved value present.
        assert "{{" not in package.path
        assert "P_MP_DOM_T" in package.path

    def test_already_eponymous_no_tokens(self, workspace, phase_folder, token_values):
        """Filename matches DDL, no tokens — both transformations
        fire as no_op with explanatory notes."""
        src, pkg = workspace
        _write(
            os.path.join(src, phase_folder, "MyTestDb_V.Customer.viw"),
            ALREADY_EPONYMOUS_DDL,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        assert len(doc.entries) == 1
        chain = list(doc.entries.values())[0]
        source, eponymous, token_resolved, package = chain.stages

        assert source.status == Status.APPLIED
        assert eponymous.status == Status.NO_OP
        assert eponymous.note  # Per discipline rule 9
        assert token_resolved.status == Status.NO_OP
        assert token_resolved.note
        assert package.status == Status.APPLIED

    def test_binary_file_skipped_stages(self, workspace, phase_folder, token_values):
        """Binary file takes the UnicodeDecodeError path —
        eponymous and token_resolved both 'skipped'."""
        src, pkg = workspace
        # Invalid-UTF-8 content: 0xFF is never a valid leading byte
        # in UTF-8, so opening this file with encoding='utf-8' will
        # raise UnicodeDecodeError and the builder will take the
        # binary code path. (A previous version of this test used
        # bytes(range(64)) which is all valid ASCII and therefore
        # valid UTF-8 — the file took the text path and the test
        # asserted on the wrong status.)
        _write(
            os.path.join(src, phase_folder, "udf_helpers.jar"),
            b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\xff\xfe\xfd\xfc" * 16,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        assert len(doc.entries) == 1
        chain = list(doc.entries.values())[0]
        source, eponymous, token_resolved, package = chain.stages

        assert source.status == Status.APPLIED
        assert eponymous.status == Status.SKIPPED
        assert "Binary" in eponymous.note
        assert token_resolved.status == Status.SKIPPED
        assert "Binary" in token_resolved.note
        assert package.status == Status.APPLIED

    def test_chain_keyed_by_final_package_path(
        self, workspace, phase_folder, token_values
    ):
        """Document entries are keyed by final package path — this
        is the contract the report renderer relies on for lookup."""
        src, pkg = workspace
        _write(
            os.path.join(src, phase_folder, "MortgagePlatform_Domain_Mortgage.tbl"),
            TABLE_DDL,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        # The single entry's key should equal its chain's final_path
        for key, chain in doc.entries.items():
            assert key == chain.final_path()

    def test_multiple_files_no_dupes(self, workspace, phase_folder, token_values):
        """Multiple files produce independent chains, no key
        collisions, no missing entries."""
        src, pkg = workspace
        # Three files in the same phase folder with distinct
        # eponymous outputs — they should produce three distinct
        # ProvenanceDocument entries.
        _write(
            os.path.join(src, phase_folder, "Mortgage.tbl"),
            TABLE_DDL,
        )
        _write(
            os.path.join(src, phase_folder, "{{DOM_DATABASE_T}}.db"),
            DATABASE_DDL,
        )
        _write(
            os.path.join(src, phase_folder, "MyTestDb_V.Customer.viw"),
            ALREADY_EPONYMOUS_DDL,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        assert len(doc.entries) == 3
        # All keys distinct (no collision, no overwrite)
        assert len(set(doc.entries.keys())) == 3
        # Every chain is complete
        for chain in doc.entries.values():
            assert chain.is_complete()


class TestProvenanceJSONOutput:
    """The full pipeline writes a valid v2 JSON file."""

    def test_json_file_loads_back(
        self, workspace, phase_folder, token_values, tmp_path
    ):
        """Build, write JSON, reload — content survives."""
        src, pkg = workspace
        _write(
            os.path.join(src, phase_folder, "Mortgage.tbl"),
            TABLE_DDL,
        )

        _, _, _, _, doc = _copy_payload(src, pkg, token_values)

        json_path = str(tmp_path / "_provenance.json")
        doc.write(json_path)

        from ddl_deployer.provenance import ProvenanceDocument

        loaded = ProvenanceDocument.load(json_path)

        assert loaded.version == doc.version
        assert set(loaded.entries.keys()) == set(doc.entries.keys())
