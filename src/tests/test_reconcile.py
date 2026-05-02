"""
Tests for the reconciliation utility (td_release_packager.reconcile).

Mirrors the layout of test_malformed_tokens.py: one class per public
surface area, with edge cases broken out to their own tests.

Note: these tests assume the module is importable as
``td_release_packager.reconcile``. Adjust the import if you keep the
file under a different package layout.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Dict, Tuple

import pytest

from td_release_packager import reconcile


# --------------------------------------------------------------------------- #
#                              Test fixtures                                  #
# --------------------------------------------------------------------------- #

# Default token map used across most tests. One real token, one decoy
# that should never match anything.
_DEFAULT_TOKEN_MAP: Dict[str, str] = {
    "MortgagePlatform_Domain_V": "{{DOM_DATABASE_V}}",
    "MortgagePlatform_Domain_T": "{{DOM_DATABASE_T}}",
}


@pytest.fixture
def payload_dir(tmp_path: Path) -> Path:
    """An empty harvested-DDL tree with the standard subdirectories."""
    root = tmp_path / "database" / "DDL"
    for sub in ("views", "tables", "grants"):
        (root / sub).mkdir(parents=True)
    return root


def _write(path: Path, content: str = "-- placeholder\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_twin(
    payload_dir: Path,
    subdir: str,
    object_name: str,
    extension: str = ".viw",
    literal_prefix: str = "MortgagePlatform_Domain_V",
    token_prefix: str = "{{DOM_DATABASE_V}}",
    *,
    same_content: bool = False,
) -> Tuple[Path, Path]:
    """Create a literal/tokenised twin pair and return both paths."""
    literal = payload_dir / subdir / f"{literal_prefix}.{object_name}{extension}"
    tokenised = payload_dir / subdir / f"{token_prefix}.{object_name}{extension}"
    _write(literal, f"-- literal version of {object_name}\n")
    if same_content:
        _write(tokenised, f"-- literal version of {object_name}\n")
    else:
        _write(tokenised, f"-- tokenised version of {object_name}\n")
    return literal, tokenised


# --------------------------------------------------------------------------- #
#                       1. Filename parsing helpers                           #
# --------------------------------------------------------------------------- #


class TestSplitPrefix:
    """_split_prefix is the lowest-level pure function -- exercise it first."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            (
                "MortgagePlatform_Domain_V.CustomerAddress.viw",
                ("MortgagePlatform_Domain_V", "CustomerAddress.viw"),
            ),
            (
                "{{DOM_DATABASE_V}}.CustomerAddress.viw",
                ("{{DOM_DATABASE_V}}", "CustomerAddress.viw"),
            ),
            ("MyDb.MyTable.tbl", ("MyDb", "MyTable.tbl")),
            ("MyDb.Object.With.Dots.viw", ("MyDb", "Object.With.Dots.viw")),
        ],
    )
    def test_valid_filenames(self, filename, expected):
        assert reconcile._split_prefix(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "noextension",  # no dot
            ".viw",  # leading dot only
            "MyDb.",  # trailing dot only
            "README.md",  # not a DDL extension
            "MyDb.thing.txt",  # txt is not a DDL extension
        ],
    )
    def test_invalid_filenames_return_none(self, filename):
        assert reconcile._split_prefix(filename) == (None, None)

    @pytest.mark.parametrize(
        "prefix,expected",
        [
            ("{{DOM_DATABASE_V}}", True),
            ("{{A}}", True),
            ("{{Token-1}}", True),
            ("MortgagePlatform_Domain_V", False),
            ("{DOM_DATABASE_V}", False),  # single braces
            ("{{}}", False),  # empty token
            ("{{1ST_DB}}", False),  # cannot start with digit
        ],
    )
    def test_is_token_prefix(self, prefix, expected):
        assert reconcile._is_token_prefix(prefix) == expected


# --------------------------------------------------------------------------- #
#                          2. Twin detection                                  #
# --------------------------------------------------------------------------- #


class TestFindTwinPairs:
    """The core detection routine."""

    def test_detects_simple_twin(self, payload_dir: Path):
        _make_twin(payload_dir, "views", "CustomerAddress")
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert len(pairs) == 1
        assert pairs[0].logical_path == "views/CustomerAddress.viw"
        assert pairs[0].literal_source.name.startswith("MortgagePlatform_Domain_V.")
        assert pairs[0].tokenised_source.name.startswith("{{DOM_DATABASE_V}}.")

    def test_returns_empty_when_only_tokenised_present(self, payload_dir: Path):
        _write(payload_dir / "views" / "{{DOM_DATABASE_V}}.OnlyTok.viw")
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert pairs == []

    def test_returns_empty_when_only_literal_present(self, payload_dir: Path):
        # An orphaned literal -- out of scope for twin detection.
        _write(payload_dir / "views" / "MortgagePlatform_Domain_V.OnlyLit.viw")
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert pairs == []

    def test_unknown_literal_prefix_is_ignored(self, payload_dir: Path):
        # SomeOtherDb is not in the token map -- must not be treated as
        # a twin candidate even if a tokenised file with the same
        # remainder exists.
        _write(payload_dir / "views" / "SomeOtherDb.Foo.viw")
        _write(payload_dir / "views" / "{{DOM_DATABASE_V}}.Foo.viw")
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert pairs == []

    def test_three_way_collision_raises(self, payload_dir: Path):
        # Two literals and one token -- ambiguous, must raise.
        _write(payload_dir / "views" / "MortgagePlatform_Domain_V.X.viw")
        _write(payload_dir / "views" / "MortgagePlatform_Domain_T.X.viw")
        _write(payload_dir / "views" / "{{DOM_DATABASE_V}}.X.viw")
        # Both literals are in the token_map so both qualify, giving 3.
        with pytest.raises(ValueError, match=reconcile.ERR_THREE_WAY_COLLISION):
            reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)

    def test_identical_content_flag(self, payload_dir: Path):
        _make_twin(payload_dir, "views", "Same", same_content=True)
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert pairs[0].files_identical is True

    def test_differing_content_flag(self, payload_dir: Path):
        _make_twin(payload_dir, "views", "Different", same_content=False)
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert pairs[0].files_identical is False

    def test_results_are_sorted(self, payload_dir: Path):
        _make_twin(payload_dir, "views", "Zebra")
        _make_twin(
            payload_dir,
            "tables",
            "Apple",
            extension=".tbl",
            literal_prefix="MortgagePlatform_Domain_T",
            token_prefix="{{DOM_DATABASE_T}}",
        )
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        # tables/ before views/ alphabetically.
        assert [p.logical_path for p in pairs] == [
            "tables/Apple.tbl",
            "views/Zebra.viw",
        ]

    def test_skips_hidden_directories(self, payload_dir: Path):
        # Hidden dir contents must be ignored even if they look like twins.
        hidden = payload_dir / ".cache"
        _write(hidden / "MortgagePlatform_Domain_V.Hidden.viw")
        _write(hidden / "{{DOM_DATABASE_V}}.Hidden.viw")
        pairs = reconcile.find_twin_pairs(payload_dir, _DEFAULT_TOKEN_MAP)
        assert pairs == []

    def test_missing_payload_dir_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            reconcile.find_twin_pairs(tmp_path / "does-not-exist", {})

    def test_payload_path_not_a_directory_raises(self, tmp_path: Path):
        f = tmp_path / "afile.txt"
        f.write_text("x")
        with pytest.raises(NotADirectoryError):
            reconcile.find_twin_pairs(f, {})


# --------------------------------------------------------------------------- #
#                          3. Diff rendering                                  #
# --------------------------------------------------------------------------- #


class TestRenderDiff:
    """Diff output is a user convenience -- it must never raise."""

    def test_byte_identical_returns_marker_string(self, payload_dir: Path):
        literal, tokenised = _make_twin(payload_dir, "views", "Same", same_content=True)
        pair = reconcile._build_twin_pair("views/Same.viw", literal, tokenised)
        assert reconcile.render_diff(pair) == "(files are byte-identical)"

    def test_differing_files_produce_unified_diff(self, payload_dir: Path):
        literal, tokenised = _make_twin(
            payload_dir, "views", "Diff", same_content=False
        )
        pair = reconcile._build_twin_pair("views/Diff.viw", literal, tokenised)
        out = reconcile.render_diff(pair)
        assert "--- " in out
        assert "+++ " in out
        assert "literal version" in out
        assert "tokenised version" in out

    def test_unreadable_file_returns_error_string(self, payload_dir: Path):
        literal, tokenised = _make_twin(payload_dir, "views", "Gone")
        pair = reconcile._build_twin_pair("views/Gone.viw", literal, tokenised)
        # Delete one before diffing -- must not raise.
        literal.unlink()
        out = reconcile.render_diff(pair)
        assert reconcile.ERR_DIFF_READ in out


# --------------------------------------------------------------------------- #
#                       4. Interactive prompt + actions                       #
# --------------------------------------------------------------------------- #


class TestPromptUser:
    """The prompt loop, driven by mocked stdin/stdout."""

    def _make_pair(self, payload_dir: Path) -> reconcile.TwinPair:
        literal, tokenised = _make_twin(payload_dir, "views", "X")
        return reconcile._build_twin_pair("views/X.viw", literal, tokenised)

    @pytest.mark.parametrize(
        "input_text,expected",
        [
            ("\n", reconcile._ACTION_KEEP_TOKENISED),  # bare Enter
            ("k\n", reconcile._ACTION_KEEP_TOKENISED),
            ("K\n", reconcile._ACTION_KEEP_TOKENISED),  # case-insensitive
            ("l\n", reconcile._ACTION_KEEP_LITERAL),
            ("s\n", reconcile._ACTION_SKIP),
            ("q\n", reconcile._ACTION_QUIT),
        ],
    )
    def test_single_keystroke_actions(self, payload_dir: Path, input_text, expected):
        pair = self._make_pair(payload_dir)
        out = io.StringIO()
        result = reconcile._prompt_user(
            pair,
            in_stream=io.StringIO(input_text),
            out_stream=out,
        )
        assert result == expected

    def test_eof_treated_as_quit(self, payload_dir: Path):
        pair = self._make_pair(payload_dir)
        result = reconcile._prompt_user(
            pair,
            in_stream=io.StringIO(""),  # immediate EOF
            out_stream=io.StringIO(),
        )
        assert result == reconcile._ACTION_QUIT

    def test_diff_then_action(self, payload_dir: Path):
        pair = self._make_pair(payload_dir)
        out = io.StringIO()
        result = reconcile._prompt_user(
            pair,
            in_stream=io.StringIO("d\nk\n"),  # diff, then keep tokenised
            out_stream=out,
        )
        assert result == reconcile._ACTION_KEEP_TOKENISED
        assert "literal version" in out.getvalue()  # diff was shown

    def test_invalid_input_re_prompts(self, payload_dir: Path):
        pair = self._make_pair(payload_dir)
        out = io.StringIO()
        result = reconcile._prompt_user(
            pair,
            in_stream=io.StringIO("xyz\nk\n"),
            out_stream=out,
        )
        assert result == reconcile._ACTION_KEEP_TOKENISED
        assert "Unknown option" in out.getvalue()


class TestApplyAction:
    """File-system side effects of the chosen action."""

    def test_keep_tokenised_deletes_literal(self, payload_dir: Path):
        literal, tokenised = _make_twin(payload_dir, "views", "X")
        pair = reconcile._build_twin_pair("views/X.viw", literal, tokenised)
        res = reconcile._apply_action(pair, reconcile._ACTION_KEEP_TOKENISED)
        assert res.action == "kept_tokenised"
        assert res.deleted_file == literal
        assert not literal.exists()
        assert tokenised.exists()

    def test_keep_literal_deletes_tokenised(self, payload_dir: Path):
        literal, tokenised = _make_twin(payload_dir, "views", "X")
        pair = reconcile._build_twin_pair("views/X.viw", literal, tokenised)
        res = reconcile._apply_action(pair, reconcile._ACTION_KEEP_LITERAL)
        assert res.action == "kept_literal"
        assert res.deleted_file == tokenised
        assert literal.exists()
        assert not tokenised.exists()

    def test_skip_deletes_nothing(self, payload_dir: Path):
        literal, tokenised = _make_twin(payload_dir, "views", "X")
        pair = reconcile._build_twin_pair("views/X.viw", literal, tokenised)
        res = reconcile._apply_action(pair, reconcile._ACTION_SKIP)
        assert res.action == "skipped"
        assert res.deleted_file is None
        assert literal.exists()
        assert tokenised.exists()


# --------------------------------------------------------------------------- #
#                       5. End-to-end orchestration                           #
# --------------------------------------------------------------------------- #


class TestRunInteractiveReconciliation:
    """Drive the full session end to end with mocked stdin/stdout."""

    def _run(
        self,
        payload_dir: Path,
        tmp_path: Path,
        keystrokes: str,
    ) -> Tuple[reconcile.ReconciliationResult, str]:
        json_out = tmp_path / "logs" / "reconcile.json"
        out = io.StringIO()
        result = reconcile.run_interactive_reconciliation(
            project_root=tmp_path,
            payload_dir=payload_dir,
            token_map=_DEFAULT_TOKEN_MAP,
            token_map_path=tmp_path / "token_map.conf",
            json_output_path=json_out,
            in_stream=io.StringIO(keystrokes),
            out_stream=out,
            require_tty=False,
        )
        return result, out.getvalue()

    def test_no_twins_clean_exit(self, payload_dir: Path, tmp_path: Path):
        result, output = self._run(payload_dir, tmp_path, "")
        assert result.pairs == []
        assert result.resolutions == []
        assert "No twin pairs found" in output
        # JSON must still be written even on the clean-tree path.
        json_path = tmp_path / "logs" / "reconcile.json"
        assert json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["summary"]["twin_pairs_found"] == 0

    def test_resolves_multiple_pairs(self, payload_dir: Path, tmp_path: Path):
        _make_twin(payload_dir, "views", "First")
        _make_twin(payload_dir, "views", "Second")
        result, _ = self._run(payload_dir, tmp_path, "k\nl\n")
        assert result.kept_tokenised_count == 1
        assert result.kept_literal_count == 1
        assert result.skipped_count == 0
        assert result.quit_early is False

    def test_quit_mid_session_records_completed_only(
        self, payload_dir: Path, tmp_path: Path
    ):
        _make_twin(payload_dir, "views", "First")
        _make_twin(payload_dir, "views", "Second")
        result, _ = self._run(payload_dir, tmp_path, "k\nq\n")
        assert result.quit_early is True
        assert len(result.resolutions) == 1
        assert result.resolutions[0].action == "kept_tokenised"

    def test_json_payload_shape(self, payload_dir: Path, tmp_path: Path):
        _make_twin(payload_dir, "views", "Shape")
        self._run(payload_dir, tmp_path, "k\n")
        payload = json.loads((tmp_path / "logs" / "reconcile.json").read_text())
        assert {
            "session_id",
            "project_root",
            "token_map",
            "summary",
            "twin_pairs",
            "resolutions",
        } <= payload.keys()
        assert payload["summary"]["kept_tokenised"] == 1
        assert len(payload["twin_pairs"]) == 1
        pair_record = payload["twin_pairs"][0]
        assert pair_record["logical_path"] == "views/Shape.viw"
        assert "literal_mtime" in pair_record
        assert isinstance(pair_record["files_identical"], bool)

    def test_non_tty_refused_when_required(self, payload_dir: Path, tmp_path: Path):
        _make_twin(payload_dir, "views", "X")
        with pytest.raises(RuntimeError, match=reconcile.ERR_NOT_INTERACTIVE):
            reconcile.run_interactive_reconciliation(
                project_root=tmp_path,
                payload_dir=payload_dir,
                token_map=_DEFAULT_TOKEN_MAP,
                token_map_path=tmp_path / "token_map.conf",
                json_output_path=tmp_path / "logs" / "out.json",
                in_stream=io.StringIO("k\n"),
                out_stream=io.StringIO(),
                require_tty=True,  # default in real usage
            )


# --------------------------------------------------------------------------- #
#                          6. Summary banner                                  #
# --------------------------------------------------------------------------- #


class TestFormatSummaryBanner:
    def _result_with(self, **counts):
        r = reconcile.ReconciliationResult(
            session_id="20260502T140000Z",
            project_root=Path("/proj"),
            token_map_path=Path("/proj/token_map.conf"),
        )
        # Manufacture resolutions to drive the count properties.
        for action, n in counts.items():
            for _ in range(n):
                r.resolutions.append(
                    reconcile.TwinResolution(
                        pair=reconcile.TwinPair(
                            logical_path="x",
                            literal_source=Path("a"),
                            tokenised_source=Path("b"),
                            literal_size=0,
                            tokenised_size=0,
                            literal_mtime=__import__("datetime").datetime.now(),
                            tokenised_mtime=__import__("datetime").datetime.now(),
                            files_identical=False,
                        ),
                        action=action,
                    )
                )
        return r

    def test_zero_state(self):
        r = reconcile.ReconciliationResult(
            session_id="x",
            project_root=Path("."),
            token_map_path=Path("."),
        )
        banner = reconcile.format_summary_banner(r)
        assert "Twin pairs found:    0" in banner
        assert "Quit early:          no" in banner

    def test_mixed_counts(self):
        r = self._result_with(
            kept_tokenised=2,
            kept_literal=1,
            skipped=1,
        )
        r.pairs = [None] * 4  # type: ignore[list-item]
        banner = reconcile.format_summary_banner(r)
        assert "Twin pairs found:    4" in banner
        assert "Tokenised kept:      2" in banner
        assert "Literal kept:        1" in banner
        assert "Skipped:             1" in banner
