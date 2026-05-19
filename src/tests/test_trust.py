"""
test_trust.py — Tests for the Phase 1 Trust Report.

Covers:
    - Signal computation from ships.decisions.json (inspect stages)
    - Provenance signal from filesystem state
    - Label derivation (READY / READY-WITH-CAVEATS / BLOCKED)
    - to_dict serialisation matches ships.build.json schema
    - Banner formatting
    - Integration: build_package stamps trust block in ships.build.json
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from td_release_packager.trust import (
    LABEL_BLOCKED,
    LABEL_CAVEATS,
    LABEL_READY,
    TRUST_FAIL,
    TRUST_PASS,
    TRUST_UNKNOWN,
    TRUST_WARN,
    TrustReport,
    TrustSignal,
    _derive_label,
    _inspect_signal,
    _provenance_signal,
    compute_trust_report,
    format_trust_banner,
)
from td_release_packager.orchestrator.issue_codes import (
    INSPECT_LINT_VIOLATION,
    INSPECT_TOKEN_MALFORMED,
)


# ---------------------------------------------------------------
# _inspect_signal
# ---------------------------------------------------------------


class TestInspectSignal:
    def _make_stage(self, issues):
        return {"stage": "inspect", "issues": issues}

    def test_pass_when_no_matching_issues(self):
        stage = self._make_stage([])
        sig = _inspect_signal(stage, INSPECT_TOKEN_MALFORMED, "Tokens bad", "Tokens OK")
        assert sig.status == TRUST_PASS

    def test_warn_on_warning_severity_issues(self):
        stage = self._make_stage(
            [
                {
                    "code": INSPECT_LINT_VIOLATION,
                    "severity": "warning",
                    "message": "rule X",
                },
            ]
        )
        sig = _inspect_signal(stage, INSPECT_LINT_VIOLATION, "Lint", "OK")
        assert sig.status == TRUST_WARN
        assert len(sig.issues) == 1

    def test_fail_on_error_severity_issues(self):
        stage = self._make_stage(
            [
                {
                    "code": INSPECT_TOKEN_MALFORMED,
                    "severity": "error",
                    "message": "bad token",
                },
            ]
        )
        sig = _inspect_signal(stage, INSPECT_TOKEN_MALFORMED, "Tokens bad", "OK")
        assert sig.status == TRUST_FAIL

    def test_fail_takes_precedence_over_warn(self):
        stage = self._make_stage(
            [
                {
                    "code": INSPECT_LINT_VIOLATION,
                    "severity": "warning",
                    "message": "warn",
                },
                {
                    "code": INSPECT_LINT_VIOLATION,
                    "severity": "error",
                    "message": "error",
                },
            ]
        )
        sig = _inspect_signal(stage, INSPECT_LINT_VIOLATION, "Lint", "OK")
        assert sig.status == TRUST_FAIL

    def test_unknown_when_stage_is_none(self):
        sig = _inspect_signal(None, INSPECT_TOKEN_MALFORMED, "Tokens bad", "OK")
        assert sig.status == TRUST_UNKNOWN

    def test_issues_capped_at_ten(self):
        issues = [
            {
                "code": INSPECT_LINT_VIOLATION,
                "severity": "warning",
                "message": f"issue {i}",
            }
            for i in range(15)
        ]
        stage = self._make_stage(issues)
        sig = _inspect_signal(stage, INSPECT_LINT_VIOLATION, "Lint", "OK")
        assert len(sig.issues) <= 10


# ---------------------------------------------------------------
# _provenance_signal
# ---------------------------------------------------------------


class TestProvenanceSignal:
    def test_pass_when_provenance_in_context_dir(self, tmp_path):
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        (context_dir / "ships.provenance.json").write_text("{}", encoding="utf-8")
        sig = _provenance_signal(str(tmp_path))
        assert sig.status == TRUST_PASS

    def test_warn_when_provenance_absent(self, tmp_path):
        sig = _provenance_signal(str(tmp_path))
        assert sig.status == TRUST_WARN


# ---------------------------------------------------------------
# _derive_label
# ---------------------------------------------------------------


class TestDeriveLabel:
    def test_ready_all_pass(self):
        signals = {
            "a": TrustSignal(status=TRUST_PASS, message="ok"),
            "b": TrustSignal(status=TRUST_PASS, message="ok"),
        }
        assert _derive_label(signals) == LABEL_READY

    def test_caveats_on_warn(self):
        signals = {
            "a": TrustSignal(status=TRUST_PASS, message="ok"),
            "b": TrustSignal(status=TRUST_WARN, message="warning"),
        }
        assert _derive_label(signals) == LABEL_CAVEATS

    def test_caveats_on_unknown(self):
        signals = {
            "a": TrustSignal(status=TRUST_UNKNOWN, message="?"),
        }
        assert _derive_label(signals) == LABEL_CAVEATS

    def test_blocked_on_fail(self):
        signals = {
            "a": TrustSignal(status=TRUST_FAIL, message="fail"),
            "b": TrustSignal(status=TRUST_PASS, message="ok"),
        }
        assert _derive_label(signals) == LABEL_BLOCKED

    def test_blocked_takes_precedence_over_warn(self):
        signals = {
            "a": TrustSignal(status=TRUST_FAIL, message="fail"),
            "b": TrustSignal(status=TRUST_WARN, message="warn"),
        }
        assert _derive_label(signals) == LABEL_BLOCKED


# ---------------------------------------------------------------
# compute_trust_report
# ---------------------------------------------------------------


def _write_decisions(path: Path, runs: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8")


class TestComputeTrustReport:
    def test_ready_with_clean_decisions(self, tmp_path):
        _write_decisions(
            tmp_path / "ships.decisions.json",
            [
                {
                    "command": "inspect",
                    "stages": [{"stage": "inspect", "status": "success", "issues": []}],
                }
            ],
        )
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.provenance.json").write_text(
            "{}", encoding="utf-8"
        )
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.label == LABEL_READY

    def test_blocked_on_token_malformed_error(self, tmp_path):
        _write_decisions(
            tmp_path / "ships.decisions.json",
            [
                {
                    "command": "inspect",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "error",
                            "issues": [
                                {
                                    "code": INSPECT_TOKEN_MALFORMED,
                                    "severity": "error",
                                    "message": "{{DB}}  malformed",
                                },
                            ],
                        }
                    ],
                }
            ],
        )
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.label == LABEL_BLOCKED
        assert report.signals["inspect_token_format"].status == TRUST_FAIL

    def test_caveats_on_lint_warning(self, tmp_path):
        _write_decisions(
            tmp_path / "ships.decisions.json",
            [
                {
                    "command": "inspect",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "warning",
                            "issues": [
                                {
                                    "code": INSPECT_LINT_VIOLATION,
                                    "severity": "warning",
                                    "message": "naming convention",
                                },
                            ],
                        }
                    ],
                }
            ],
        )
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.provenance.json").write_text(
            "{}", encoding="utf-8"
        )
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.label == LABEL_CAVEATS
        assert report.signals["inspect_lint"].status == TRUST_WARN

    def test_lint_issues_keep_file_location(self, tmp_path):
        _write_decisions(
            tmp_path / "ships.decisions.json",
            [
                {
                    "command": "inspect",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "error",
                            "issues": [
                                {
                                    "code": INSPECT_LINT_VIOLATION,
                                    "severity": "error",
                                    "message": (
                                        "[db_qualifier] Object 'GDEV1P_BB' "
                                        "missing database qualifier."
                                    ),
                                    "location": "payload/database/DDL/views/V.viw:1",
                                }
                            ],
                        }
                    ],
                }
            ],
        )

        report = compute_trust_report(str(tmp_path), str(tmp_path))

        issue = report.signals["inspect_lint"].issues[0]
        assert issue.startswith("payload/database/DDL/views/V.viw:1:")
        assert "[db_qualifier]" in issue

    def test_caveats_when_no_decisions_json(self, tmp_path):
        """No ships.decisions.json means inspect never ran — signals are UNKNOWN."""
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.label == LABEL_CAVEATS  # UNKNOWN signals → caveats
        for sig in report.signals.values():
            if "inspect" in sig.message.lower() or sig.status == TRUST_UNKNOWN:
                break
        else:
            pytest.fail("Expected at least one UNKNOWN signal")

    def test_uses_last_inspect_stage(self, tmp_path):
        """When multiple runs exist, the last inspect stage wins."""
        _write_decisions(
            tmp_path / "ships.decisions.json",
            [
                {
                    "command": "inspect",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "error",
                            "issues": [
                                {
                                    "code": INSPECT_TOKEN_MALFORMED,
                                    "severity": "error",
                                    "message": "old",
                                },
                            ],
                        }
                    ],
                },
                {
                    "command": "inspect",
                    "stages": [{"stage": "inspect", "status": "success", "issues": []}],
                },
            ],
        )
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.provenance.json").write_text(
            "{}", encoding="utf-8"
        )
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        # Second run (clean) should win
        assert report.signals["inspect_token_format"].status == TRUST_PASS

    def test_to_dict_schema(self, tmp_path):
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        d = report.to_dict()
        assert "label" in d
        assert "computed_at" in d
        assert "signals" in d
        for sig_dict in d["signals"].values():
            assert "status" in sig_dict
            assert "message" in sig_dict
            assert "issues" in sig_dict


# ---------------------------------------------------------------
# format_trust_banner
# ---------------------------------------------------------------


class TestFormatTrustBanner:
    def test_ready_banner_contains_label(self):
        report = TrustReport(
            label=LABEL_READY,
            computed_at="2026-05-09T14:30:00+00:00",
            signals={"test": TrustSignal(status=TRUST_PASS, message="OK")},
        )
        banner = format_trust_banner(report)
        assert LABEL_READY in banner
        assert "test" in banner

    def test_blocked_banner_contains_label(self):
        report = TrustReport(
            label=LABEL_BLOCKED,
            computed_at="2026-05-09T14:30:00+00:00",
            signals={"bad": TrustSignal(status=TRUST_FAIL, message="Error found")},
        )
        banner = format_trust_banner(report)
        assert LABEL_BLOCKED in banner


# ---------------------------------------------------------------
# Integration: build_package stamps trust in ships.build.json
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def tmp_project(tmp_path):
    project = tmp_path / "project"
    for sub in (
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/pre-requisites/databases",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".build_counter").write_text("0\n", encoding="utf-8")
    return project


class TestBuildPackageStampsTrust:
    def test_trust_block_in_build_json(self, tmp_path, tmp_project):
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        _write(
            tmp_project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")

        cfg = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="TestPkg",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (main_arc, manifest), _companion = build_package(cfg)

        with zipfile.ZipFile(main_arc) as zf:
            build_json_name = next(
                n for n in zf.namelist() if n.endswith("ships.build.json")
            )
            build_data = json.loads(zf.read(build_json_name))

        assert "trust" in build_data, "ships.build.json must contain a trust block"
        trust = build_data["trust"]
        assert "label" in trust
        assert trust["label"] in (LABEL_READY, LABEL_CAVEATS, LABEL_BLOCKED)
        assert "signals" in trust
        assert "inspect_token_format" in trust["signals"]
        assert "provenance_complete" in trust["signals"]
        assert trust["signals"]["provenance_complete"]["status"] == TRUST_PASS
        assert trust["signals"]["build_reproducible"]["status"] == TRUST_PASS

    def test_trust_label_ready_with_clean_inspect(self, tmp_path, tmp_project):
        """When ships.decisions.json has a clean inspect run, label should be READY."""
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        _write(
            tmp_project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        # Seed a clean ships.decisions.json
        decisions = {
            "schema_version": 1,
            "runs": [
                {
                    "command": "inspect",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "success",
                            "issues": [],
                        }
                    ],
                }
            ],
        }
        (tmp_project / "ships.decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )
        # Seed provenance
        (tmp_project / "context").mkdir(parents=True, exist_ok=True)
        (tmp_project / "context" / "ships.provenance.json").write_text(
            "{}", encoding="utf-8"
        )

        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")

        cfg = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="TestPkg",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (main_arc, _), _companion = build_package(cfg)

        with zipfile.ZipFile(main_arc) as zf:
            build_json_name = next(
                n for n in zf.namelist() if n.endswith("ships.build.json")
            )
            build_data = json.loads(zf.read(build_json_name))

        assert build_data["trust"]["label"] == LABEL_READY


# ---------------------------------------------------------------
# build_reproducible signal
# ---------------------------------------------------------------


from td_release_packager.trust import _build_reproducible_signal  # noqa: E402


class TestBuildReproducibleSignal:
    def test_pass_when_build_json_absent(self, tmp_path):
        sig = _build_reproducible_signal(str(tmp_path))
        assert sig.status == TRUST_PASS

    def test_pass_when_source_dirty_false(self, tmp_path):
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.build.json").write_text(
            '{"source_dirty": false}', encoding="utf-8"
        )
        sig = _build_reproducible_signal(str(tmp_path))
        assert sig.status == TRUST_PASS

    def test_pass_when_source_dirty_absent(self, tmp_path):
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.build.json").write_text(
            '{"build_number": "0001"}', encoding="utf-8"
        )
        sig = _build_reproducible_signal(str(tmp_path))
        assert sig.status == TRUST_PASS

    def test_warn_when_source_dirty_true(self, tmp_path):
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.build.json").write_text(
            '{"source_dirty": true}', encoding="utf-8"
        )
        sig = _build_reproducible_signal(str(tmp_path))
        assert sig.status == TRUST_WARN
        assert "dirty" in sig.message.lower()

    def test_dirty_build_triggers_caveats_label(self, tmp_path):
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.build.json").write_text(
            '{"source_dirty": true}', encoding="utf-8"
        )
        (tmp_path / "context").mkdir(parents=True, exist_ok=True)
        (tmp_path / "context" / "ships.provenance.json").write_text(
            "{}", encoding="utf-8"
        )
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.label == LABEL_CAVEATS
        assert report.signals["build_reproducible"].status == TRUST_WARN
