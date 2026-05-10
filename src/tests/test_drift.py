"""
test_drift.py — Tests for the schema drift detection module.

Covers:
    - normalise_show: whitespace stripping, blank-line trimming
    - baseline_path: filename derivation
    - read_baseline: absent file, valid file, unreadable file
    - write_baseline: creates dir, overwrites on second write, normalises text
    - check_drift: no baseline → no drift; matching → no drift;
                   different → drift with diff; CRLF tolerance
    - _dispatch_deploy integration: drift=abort blocks deploy,
                                    drift=skip skips, drift=continue proceeds,
                                    post-deploy baseline written on COMPLETED
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from database_package_deployer.drift import (
    DriftResult,
    baseline_path,
    check_drift,
    normalise_show,
    read_baseline,
    write_baseline,
)


# ---------------------------------------------------------------
# normalise_show
# ---------------------------------------------------------------


class TestNormaliseShow:
    def test_strips_trailing_spaces(self):
        result = normalise_show("CREATE TABLE t   \nFIELD1 INT   ")
        assert result == "CREATE TABLE t\nFIELD1 INT"

    def test_drops_leading_blank_lines(self):
        result = normalise_show("\n\n\nCREATE TABLE t")
        assert result == "CREATE TABLE t"

    def test_drops_trailing_blank_lines(self):
        result = normalise_show("CREATE TABLE t\n\n\n")
        assert result == "CREATE TABLE t"

    def test_preserves_internal_blank_lines(self):
        result = normalise_show("LINE 1\n\nLINE 3")
        assert result == "LINE 1\n\nLINE 3"

    def test_normalises_crlf(self):
        result = normalise_show("CREATE TABLE t\r\nFIELD1 INT\r\n")
        assert result == "CREATE TABLE t\nFIELD1 INT"

    def test_empty_string(self):
        assert normalise_show("") == ""

    def test_only_whitespace(self):
        assert normalise_show("   \n   \n   ") == ""


# ---------------------------------------------------------------
# baseline_path
# ---------------------------------------------------------------


class TestBaselinePath:
    def test_constructs_correct_path(self, tmp_path):
        p = baseline_path(str(tmp_path), "OMR_STD", "Customer")
        assert p == str(tmp_path / "OMR_STD.Customer.baseline")

    def test_filename_uses_dot_separator(self, tmp_path):
        p = baseline_path(str(tmp_path), "MY_DB", "MY_OBJ")
        assert os.path.basename(p) == "MY_DB.MY_OBJ.baseline"


# ---------------------------------------------------------------
# read_baseline
# ---------------------------------------------------------------


class TestReadBaseline:
    def test_returns_none_when_no_file(self, tmp_path):
        result = read_baseline(str(tmp_path), "DB", "OBJ")
        assert result is None

    def test_returns_content_when_file_exists(self, tmp_path):
        path = tmp_path / "DB.OBJ.baseline"
        path.write_text("CREATE TABLE DB.OBJ (id INT);", encoding="utf-8")
        result = read_baseline(str(tmp_path), "DB", "OBJ")
        assert result == "CREATE TABLE DB.OBJ (id INT);"

    def test_returns_none_on_unreadable_file(self, tmp_path):
        # Point at a directory rather than a file to trigger an OS error
        path = tmp_path / "DB.OBJ.baseline"
        path.mkdir()
        result = read_baseline(str(tmp_path), "DB", "OBJ")
        assert result is None


# ---------------------------------------------------------------
# write_baseline
# ---------------------------------------------------------------


class TestWriteBaseline:
    def test_creates_directory_and_file(self, tmp_path):
        baseline_dir = str(tmp_path / "baselines")
        write_baseline(baseline_dir, "DB", "OBJ", "CREATE TABLE DB.OBJ (id INT);")
        path = Path(baseline_dir) / "DB.OBJ.baseline"
        assert path.exists()

    def test_normalises_before_writing(self, tmp_path):
        write_baseline(str(tmp_path), "DB", "OBJ", "CREATE TABLE t   \n\n\n")
        stored = read_baseline(str(tmp_path), "DB", "OBJ")
        assert stored == "CREATE TABLE t"

    def test_overwrites_on_second_write(self, tmp_path):
        write_baseline(str(tmp_path), "DB", "OBJ", "version 1")
        write_baseline(str(tmp_path), "DB", "OBJ", "version 2")
        assert read_baseline(str(tmp_path), "DB", "OBJ") == "version 2"

    def test_handles_unwritable_dir_gracefully(self, tmp_path):
        # Pass a path that cannot be created (file in the way of directory)
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file", encoding="utf-8")
        bad_dir = str(blocker / "baselines")
        write_baseline(bad_dir, "DB", "OBJ", "content")  # must not raise


# ---------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------


class TestCheckDrift:
    def test_no_drift_when_no_baseline(self, tmp_path):
        result = check_drift(
            str(tmp_path), "DB", "OBJ", "CREATE TABLE DB.OBJ (id INT);"
        )
        assert result.detected is False

    def test_no_drift_when_outputs_match(self, tmp_path):
        show = "CREATE TABLE DB.OBJ (id INT);"
        write_baseline(str(tmp_path), "DB", "OBJ", show)
        result = check_drift(str(tmp_path), "DB", "OBJ", show)
        assert result.detected is False

    def test_drift_detected_when_outputs_differ(self, tmp_path):
        write_baseline(str(tmp_path), "DB", "OBJ", "CREATE TABLE DB.OBJ (id INT);")
        result = check_drift(
            str(tmp_path),
            "DB",
            "OBJ",
            "CREATE TABLE DB.OBJ (id INT, region VARCHAR(50));",
        )
        assert result.detected is True
        assert result.diff_text  # non-empty diff
        assert "region" in result.diff_text

    def test_no_drift_despite_trailing_whitespace_variance(self, tmp_path):
        write_baseline(str(tmp_path), "DB", "OBJ", "CREATE TABLE DB.OBJ (id INT);")
        # Same content but with trailing spaces and CRLF line endings
        result = check_drift(
            str(tmp_path),
            "DB",
            "OBJ",
            "CREATE TABLE DB.OBJ (id INT);   \r\n",
        )
        assert result.detected is False

    def test_diff_text_contains_from_and_to_labels(self, tmp_path):
        write_baseline(str(tmp_path), "DB", "T", "line one")
        result = check_drift(str(tmp_path), "DB", "T", "line two")
        assert "last SHIPS deploy" in result.diff_text
        assert "current database" in result.diff_text

    def test_drift_result_carries_baseline_and_current(self, tmp_path):
        write_baseline(str(tmp_path), "DB", "OBJ", "old content")
        result = check_drift(str(tmp_path), "DB", "OBJ", "new content")
        assert result.baseline == "old content"
        assert result.current == "new content"


# ---------------------------------------------------------------
# _dispatch_deploy integration — drift abort/skip/continue/capture
# ---------------------------------------------------------------


class TestDispatchDeployDrift:
    """Verify drift behaviour without a live database.

    _run_show_text is mocked to return canned SHOW output.
    _deploy_* strategy functions are mocked to return COMPLETED.
    """

    def _make_parsed(self, db="OMR_STD", obj="Customer", obj_type=None, strategy=None):
        from database_package_deployer.models import (
            DeployIntent,
            DeployState,
            DeployStrategy,
            ObjectType,
            ParsedStatement,
        )

        ddl = f"REPLACE TABLE {db}.{obj} (id INT);"
        return ParsedStatement(
            file_path=f"/pkg/{db}.{obj}.tbl",
            database_name=db,
            object_name=obj,
            qualified_name=f"{db}.{obj}",
            object_type=obj_type or ObjectType.TABLE,
            strategy=strategy or DeployStrategy.REPLACE_IN_PLACE,
            deploy_intent=DeployIntent.CREATE_ONLY,
            ddl_text=ddl,
            original_text=ddl,
        )

    def _make_manifest(self, tmp_path, parsed=None):
        from database_package_deployer.manifest import DeploymentManifest
        from database_package_deployer.models import DeployState

        m = DeploymentManifest(package_dir=str(tmp_path))
        if parsed is not None:
            m.register_object(
                qualified_name=parsed.qualified_name,
                object_type=parsed.object_type.value,
                ddl_file=parsed.file_path,
            )
        return m

    def _completed_result(self, parsed):
        from database_package_deployer.models import DeployState, ObjectDeployResult

        return ObjectDeployResult(
            database_name=parsed.database_name,
            object_name=parsed.object_name,
            object_type=parsed.object_type,
            state=DeployState.COMPLETED,
            message="deployed",
        )

    def test_abort_on_drift_returns_failed(self, tmp_path):
        from database_package_deployer import deployer

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        baseline_dir = str(tmp_path / "baselines")

        # Write a baseline that differs from the live SHOW
        write_baseline(baseline_dir, "OMR_STD", "Customer", "old DDL")

        with patch.object(deployer, "_run_show_text", return_value="new DDL"):
            result = deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=False,
                baseline_dir=baseline_dir,
                on_drift="abort",
            )

        from database_package_deployer.models import DeployState

        assert result.state == DeployState.FAILED
        assert result.drift_detected is True
        assert result.drift_diff  # non-empty

    def test_skip_on_drift_returns_skipped(self, tmp_path):
        from database_package_deployer import deployer

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        baseline_dir = str(tmp_path / "baselines")
        write_baseline(baseline_dir, "OMR_STD", "Customer", "old DDL")

        with patch.object(deployer, "_run_show_text", return_value="new DDL"):
            result = deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=False,
                baseline_dir=baseline_dir,
                on_drift="skip",
            )

        from database_package_deployer.models import DeployState

        assert result.state == DeployState.SKIPPED
        assert result.drift_detected is True

    def test_continue_on_drift_deploys_anyway(self, tmp_path):
        from database_package_deployer import deployer
        from database_package_deployer.models import DeployState

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        baseline_dir = str(tmp_path / "baselines")
        write_baseline(baseline_dir, "OMR_STD", "Customer", "old DDL")

        completed = self._completed_result(parsed)
        with (
            patch.object(deployer, "_run_show_text", return_value="new DDL"),
            patch.object(deployer, "_deploy_replace_in_place", return_value=completed),
        ):
            result = deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=False,
                baseline_dir=baseline_dir,
                on_drift="continue",
            )

        assert result.state == DeployState.COMPLETED

    def test_no_drift_check_when_baseline_dir_empty(self, tmp_path):
        from database_package_deployer import deployer
        from database_package_deployer.models import DeployState

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        completed = self._completed_result(parsed)

        with (
            patch.object(deployer, "_run_show_text") as mock_show,
            patch.object(deployer, "_deploy_replace_in_place", return_value=completed),
        ):
            deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=False,
                baseline_dir="",  # disabled
            )

        # _run_show_text should not be called for drift check (only for baseline write)
        # baseline_dir is empty so neither drift check nor baseline write runs
        mock_show.assert_not_called()

    def test_no_drift_check_on_dry_run(self, tmp_path):
        from database_package_deployer import deployer
        from database_package_deployer.models import DeployState

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        baseline_dir = str(tmp_path / "baselines")
        write_baseline(baseline_dir, "OMR_STD", "Customer", "old DDL")

        completed = self._completed_result(parsed)
        with (
            patch.object(deployer, "_run_show_text") as mock_show,
            patch.object(deployer, "_deploy_replace_in_place", return_value=completed),
        ):
            deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=True,
                baseline_dir=baseline_dir,
            )

        mock_show.assert_not_called()

    def test_baseline_written_after_successful_deploy(self, tmp_path):
        from database_package_deployer import deployer
        from database_package_deployer.models import DeployState

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        baseline_dir = str(tmp_path / "baselines")

        completed = self._completed_result(parsed)
        post_show = "CREATE TABLE OMR_STD.Customer (id INTEGER) PRIMARY INDEX (id);"

        with (
            patch.object(deployer, "_run_show_text", return_value=post_show),
            patch.object(deployer, "_deploy_replace_in_place", return_value=completed),
        ):
            result = deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=False,
                baseline_dir=baseline_dir,
            )

        assert result.state == DeployState.COMPLETED
        stored = read_baseline(baseline_dir, "OMR_STD", "Customer")
        assert stored is not None
        assert "Customer" in stored

    def test_no_baseline_written_on_failed_deploy(self, tmp_path):
        from database_package_deployer import deployer
        from database_package_deployer.models import DeployState, ObjectDeployResult

        parsed = self._make_parsed()
        manifest = self._make_manifest(tmp_path, parsed)
        baseline_dir = str(tmp_path / "baselines")

        failed = ObjectDeployResult(
            database_name=parsed.database_name,
            object_name=parsed.object_name,
            object_type=parsed.object_type,
            state=DeployState.FAILED,
            error="Syntax error",
        )

        with (
            patch.object(deployer, "_run_show_text", return_value=None),
            patch.object(deployer, "_deploy_replace_in_place", return_value=failed),
        ):
            deployer._dispatch_deploy(
                cursor=MagicMock(),
                parsed=parsed,
                manifest=manifest,
                dry_run=False,
                baseline_dir=baseline_dir,
            )

        # No baseline should have been written
        assert read_baseline(baseline_dir, "OMR_STD", "Customer") is None
