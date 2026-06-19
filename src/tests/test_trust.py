"""
test_trust.py — Tests for the Phase 1 Trust Report.

Covers:
    - Signal computation from ships.decisions.json (inspect stages)
    - Provenance signal from filesystem state
    - Status derivation (READY / READY_WITH_CAVEATS / BLOCKED)
    - to_dict serialisation matches the canonical ships.trust.json schema
    - Banner formatting
    - Integration: build_package writes canonical context/ships.trust.json
      and ships.build.json carries only a trust_ref pointer.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from td_release_packager.trust import (
    STATUS_BLOCKED,
    STATUS_CAVEATS,
    STATUS_READY,
    TRUST_FAIL,
    TRUST_PASS,
    TRUST_RESULT_REF,
    TRUST_UNKNOWN,
    TRUST_WARN,
    TrustReport,
    TrustSignal,
    _derive_status,
    _inspect_signal,
    _provenance_signal,
    compute_trust_report,
    format_trust_banner,
    load_trust_result,
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


class TestDeriveStatus:
    def test_ready_all_pass(self):
        signals = {
            "a": TrustSignal(status=TRUST_PASS, message="ok"),
            "b": TrustSignal(status=TRUST_PASS, message="ok"),
        }
        assert _derive_status(signals) == STATUS_READY

    def test_caveats_on_warn(self):
        signals = {
            "a": TrustSignal(status=TRUST_PASS, message="ok"),
            "b": TrustSignal(status=TRUST_WARN, message="warning"),
        }
        assert _derive_status(signals) == STATUS_CAVEATS

    def test_caveats_on_unknown(self):
        signals = {
            "a": TrustSignal(status=TRUST_UNKNOWN, message="?"),
        }
        assert _derive_status(signals) == STATUS_CAVEATS

    def test_blocked_on_fail(self):
        signals = {
            "a": TrustSignal(status=TRUST_FAIL, message="fail"),
            "b": TrustSignal(status=TRUST_PASS, message="ok"),
        }
        assert _derive_status(signals) == STATUS_BLOCKED

    def test_blocked_takes_precedence_over_warn(self):
        signals = {
            "a": TrustSignal(status=TRUST_FAIL, message="fail"),
            "b": TrustSignal(status=TRUST_WARN, message="warn"),
        }
        assert _derive_status(signals) == STATUS_BLOCKED


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
        # Empty token-resolution artefact keeps the sixth signal at PASS;
        # no envs to audit -> nothing to find.
        (tmp_path / "context" / "ships.token_resolution.json").write_text(
            '{"schema_version": "1.0", "environments": []}', encoding="utf-8"
        )
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.status == STATUS_READY

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
        assert report.status == STATUS_BLOCKED
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
        assert report.status == STATUS_CAVEATS
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

    def test_generated_release_artifact_issues_do_not_block_new_package(self, tmp_path):
        releases_file = (
            tmp_path
            / "releases"
            / "DEV_GCFR_BUILD_0056"
            / ".ships-work"
            / "payload"
            / "logs"
            / "rollback"
            / "OldProc.spl"
        )
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
                                    "message": "[db_qualifier] stale rollback code",
                                    "location": str(releases_file),
                                }
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
        (tmp_path / "context" / "ships.token_resolution.json").write_text(
            '{"schema_version": "1.0", "environments": []}', encoding="utf-8"
        )

        report = compute_trust_report(str(tmp_path), str(tmp_path))

        assert report.status == STATUS_READY
        assert report.signals["inspect_lint"].status == TRUST_PASS

    def test_caveats_when_no_decisions_json(self, tmp_path):
        """No ships.decisions.json means inspect never ran — signals are UNKNOWN."""
        report = compute_trust_report(str(tmp_path), str(tmp_path))
        assert report.status == STATUS_CAVEATS  # UNKNOWN signals → caveats
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
        assert d["schema_version"]
        assert d["status"] in (STATUS_READY, STATUS_CAVEATS, STATUS_BLOCKED)
        assert "deploy_allowed" in d
        assert "override_allowed" in d
        assert "evaluated_at" in d
        assert isinstance(d["evidence_paths"], list)
        assert isinstance(d["blocking_signals"], list)
        assert isinstance(d["warning_signals"], list)
        assert "signals" in d
        for sig_dict in d["signals"].values():
            assert "status" in sig_dict
            assert "message" in sig_dict
            assert "issues" in sig_dict
            assert "evidence_paths" in sig_dict

    def test_deploy_allowed_only_when_not_blocked(self):
        ready = TrustReport(
            status=STATUS_READY,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={"x": TrustSignal(status=TRUST_PASS, message="ok")},
        )
        caveats = TrustReport(
            status=STATUS_CAVEATS,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={"x": TrustSignal(status=TRUST_WARN, message="warn")},
        )
        blocked = TrustReport(
            status=STATUS_BLOCKED,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={"x": TrustSignal(status=TRUST_FAIL, message="fail")},
        )
        assert ready.deploy_allowed is True
        assert ready.override_allowed is False
        assert caveats.deploy_allowed is True
        assert caveats.override_allowed is True
        assert blocked.deploy_allowed is False
        assert blocked.override_allowed is False

    def test_blocking_and_warning_signal_lists(self):
        report = TrustReport(
            status=STATUS_BLOCKED,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={
                "ok": TrustSignal(status=TRUST_PASS, message="ok"),
                "soft": TrustSignal(status=TRUST_WARN, message="warn"),
                "hard": TrustSignal(status=TRUST_FAIL, message="fail"),
                "missing": TrustSignal(status=TRUST_UNKNOWN, message="?"),
            },
        )
        assert report.blocking_signals == ["hard"]
        assert set(report.warning_signals) == {"soft", "missing"}

    def test_evidence_paths_rolled_up(self):
        report = TrustReport(
            status=STATUS_READY,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={
                "a": TrustSignal(
                    status=TRUST_PASS,
                    message="ok",
                    evidence_paths=["context/ships.build.json"],
                ),
                "b": TrustSignal(
                    status=TRUST_PASS,
                    message="ok",
                    evidence_paths=["context/ships.build.json", "ships.decisions.json"],
                ),
            },
        )
        # De-duplicated, preserving first-seen order.
        assert report.evidence_paths == [
            "context/ships.build.json",
            "ships.decisions.json",
        ]


# ---------------------------------------------------------------
# format_trust_banner
# ---------------------------------------------------------------


class TestFormatTrustBanner:
    def test_ready_banner_contains_status(self):
        report = TrustReport(
            status=STATUS_READY,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={"test": TrustSignal(status=TRUST_PASS, message="OK")},
        )
        banner = format_trust_banner(report)
        assert STATUS_READY in banner
        assert "test" in banner

    def test_blocked_banner_contains_status(self):
        report = TrustReport(
            status=STATUS_BLOCKED,
            evaluated_at="2026-05-09T14:30:00+00:00",
            signals={"bad": TrustSignal(status=TRUST_FAIL, message="Error found")},
        )
        banner = format_trust_banner(report)
        assert STATUS_BLOCKED in banner


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

            trust_json_name = next(
                n for n in zf.namelist() if n.endswith("ships.trust.json")
            )
            trust = json.loads(zf.read(trust_json_name))

        # ships.build.json carries only a pointer to the canonical trust file.
        assert build_data["trust"] == {"trust_ref": TRUST_RESULT_REF}

        # The canonical trust file holds the full document.
        assert trust["status"] in (STATUS_READY, STATUS_CAVEATS, STATUS_BLOCKED)
        assert trust["schema_version"]
        assert "deploy_allowed" in trust
        assert "override_allowed" in trust
        assert "evaluated_at" in trust
        assert isinstance(trust["evidence_paths"], list)
        assert "signals" in trust
        assert "inspect_token_format" in trust["signals"]
        assert "provenance_complete" in trust["signals"]
        assert trust["signals"]["provenance_complete"]["status"] == TRUST_PASS
        assert trust["signals"]["build_reproducible"]["status"] == TRUST_PASS
        # The sixth signal must be present and driven by the new artefact.
        assert "token_resolution_clean" in trust["signals"]

    def test_build_writes_token_resolution_artefact(self, tmp_path, tmp_project):
        """The builder writes context/ships.token_resolution.json alongside trust."""
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig
        from td_release_packager.token_resolution_artefact import ARTEFACT_FILENAME

        _write(
            tmp_project / "payload/database/DDL/tables/{{SHIPS_ENV}}.T.tbl",
            "CREATE MULTISET TABLE {{SHIPS_ENV}}.T (Id INTEGER) PRIMARY INDEX (Id);\n",
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
            names = zf.namelist()
            artefact_name = next(
                (n for n in names if n.endswith(ARTEFACT_FILENAME)), None
            )
            assert artefact_name, (
                f"{ARTEFACT_FILENAME} missing from package; got: "
                f"{[n for n in names if n.endswith('.json')]}"
            )
            doc = json.loads(zf.read(artefact_name))

        assert doc["schema_version"] == "1.0"
        envs = doc["environments"]
        assert len(envs) == 1
        env = envs[0]
        assert env["env"] == "DEV"
        # SHIPS_ENV is referenced — not unused.
        assert env["unused"] == []
        # No clobbers in a single-token payload.
        assert env["clobbers"] == []
        # The role classifier saw SHIPS_ENV in an identity position.
        assert env["roles"].get("SHIPS_ENV") == "IDENTITY"

    def test_trust_label_ready_with_clean_inspect(self, tmp_path, tmp_project):
        """When ships.decisions.json has a clean inspect run, label should be READY."""
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        # Reference SHIPS_ENV in payload so the token-resolution audit
        # records it as used (otherwise the sixth signal warns about an
        # unused token and downgrades trust to READY_WITH_CAVEATS).
        _write(
            tmp_project / "payload/database/DDL/tables/{{SHIPS_ENV}}.T.tbl",
            "CREATE MULTISET TABLE {{SHIPS_ENV}}.T (Id INTEGER) PRIMARY INDEX (Id);\n",
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
            trust_json_name = next(
                n for n in zf.namelist() if n.endswith("ships.trust.json")
            )
            trust = json.loads(zf.read(trust_json_name))

        assert trust["status"] == STATUS_READY
        assert trust["deploy_allowed"] is True


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
        assert report.status == STATUS_CAVEATS
        assert report.signals["build_reproducible"].status == TRUST_WARN
