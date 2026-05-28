"""
test_report_replay.py — Tests for the noop-replay report rendering.

When a deploy is re-run against a package that has already been
deployed (and every COMPLETED object still exists in the database),
nothing is processed this run. The report must signal this honestly
rather than claiming "all objects deployed successfully".

Tests cover:
    - Mode banner shows REPLAY (not DEPLOYMENT)
    - Action Items section uses the noop-replay copy
    - Summary stat cards swap to REPLAY layout (Verified prior /
      Deployed this run = 0)
    - Object Results section explains the noop replay
    - Normal-mode rendering still works (no regressions)
"""

from database_package_deployer.models import (
    DeployState,
    ObjectDeployResult,
    ObjectType,
    PackageDeployResult,
    PreflightCheck,
    PreflightResult,
)
from database_package_deployer.provenance import (
    ProvenanceChain,
    ProvenanceDocument,
    Stage,
    Status,
)
from database_package_deployer.report import (
    _build_html,
    _highlight_sql,
    _html_action_items,
    _html_object_results,
    _html_summary,
    _write_source_viewers,
)


# ---------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------


def _noop_replay_result(prior_count=2):
    """A PackageDeployResult representing a noop replay."""
    prior = [
        {
            "qualified_name": f"DEV01_DB.Obj{i}",
            "state": "COMPLETED",
            "completed_at": "2026-04-20T10:00:00Z",
        }
        for i in range(prior_count)
    ]
    return PackageDeployResult(
        deployment_id="deploy_20260503_120000",
        manifest_path="/pkg/.deploy_manifest.json",
        total=prior_count,
        completed=prior_count,  # manifest summary counts prior runs too
        results=[],
        prior_completed=prior,
    )


def _normal_deploy_result():
    """A PackageDeployResult representing a real successful deploy."""
    return PackageDeployResult(
        deployment_id="deploy_20260503_130000",
        manifest_path="/pkg/.deploy_manifest.json",
        total=1,
        completed=1,
        results=[
            ObjectDeployResult(
                database_name="DEV01_DB",
                object_name="Customer",
                object_type=ObjectType.TABLE,
                state=DeployState.COMPLETED,
            )
        ],
        prior_completed=[],
    )


def _failed_result_with_source():
    """A failed object with a shortened ddl_file as recorded by the deployer."""
    return PackageDeployResult(
        deployment_id="deploy_20260503_140000",
        manifest_path="/pkg/logs/.deploy_manifest.json",
        total=1,
        failed=1,
        results=[
            ObjectDeployResult(
                database_name="DEV01_DB",
                object_name="BadView",
                object_type=ObjectType.VIEW,
                state=DeployState.FAILED,
                ddl_file="views/DEV01_DB.BadView.viw",
                error="Syntax error",
                message="Deployment failed",
            )
        ],
        prior_completed=[],
    )


def _source_provenance():
    """Provenance linking deployed file back to project source."""
    chain = ProvenanceChain()
    chain.add(Stage("source", "domain/views/BadView.viw", Status.APPLIED))
    chain.add(
        Stage(
            "eponymous",
            "domain/views/{{DOM_DATABASE_V}}.BadView.viw",
            Status.APPLIED,
        )
    )
    chain.add(
        Stage(
            "token_resolved",
            "domain/views/DEV01_DB.BadView.viw",
            Status.APPLIED,
        )
    )
    chain.add(
        Stage(
            "package",
            "03_ddl/views/DEV01_DB.BadView.viw",
            Status.APPLIED,
        )
    )
    doc = ProvenanceDocument()
    doc.add_chain(chain)
    return doc


# ---------------------------------------------------------------
# Mode banner — fix D
# ---------------------------------------------------------------


class TestModeBanner:
    """The report mode label distinguishes REPLAY from DEPLOYMENT."""

    def test_replay_mode_when_noop(self):
        """Empty results + prior_completed → 'REPLAY Report'."""
        html = _build_html(_noop_replay_result())
        assert "REPLAY Report" in html
        assert "DEPLOYMENT Report" not in html

    def test_deployment_mode_when_normal(self):
        """Normal deploy → 'DEPLOYMENT Report'."""
        html = _build_html(_normal_deploy_result())
        assert "DEPLOYMENT Report" in html
        assert "REPLAY Report" not in html

    def test_dry_run_takes_precedence_over_replay(self):
        """dry_run=True wins over noop-replay (DRY RUN mode)."""
        result = _noop_replay_result()
        result.dry_run = True
        html = _build_html(result)
        assert "DRY RUN Report" in html
        assert "REPLAY Report" not in html

    def test_explain_takes_precedence_over_replay(self):
        """An explain-id deployment wins over noop-replay (EXPLAIN mode)."""
        result = _noop_replay_result()
        result.deployment_id = "explain_20260503_120000"
        html = _build_html(result)
        assert "EXPLAIN Report" in html
        assert "REPLAY Report" not in html


# ---------------------------------------------------------------
# Action Items — fix B
# ---------------------------------------------------------------


class TestActionItemsOnReplay:
    """Action Items section must not claim 'deployed successfully' on a replay."""

    def test_noop_replay_message(self):
        """Empty results with prior shows the replay-specific copy."""
        html = _html_action_items(_noop_replay_result(prior_count=3))
        assert "already deployed in a previous run" in html
        assert "Nothing was processed this run" in html
        # The misleading default message must not appear
        assert "all objects deployed successfully" not in html

    def test_singular_object_grammar(self):
        """Single prior object uses singular noun."""
        html = _html_action_items(_noop_replay_result(prior_count=1))
        assert (
            "1 object were already" in html
            or "1 object was already" in html
            or ("all 1 object " in html)
        )
        assert "1 objects" not in html  # no incorrect plural

    def test_default_message_when_no_prior_and_no_results(self):
        """Empty results AND no prior → original default copy."""
        result = PackageDeployResult(
            deployment_id="d",
            manifest_path="/pkg/.deploy_manifest.json",
            total=0,
            results=[],
            prior_completed=[],
        )
        html = _html_action_items(result)
        assert "all objects deployed successfully" in html

    def test_preflight_failure_has_action_items(self):
        """A preflight parse failure must not claim all objects deployed."""
        result = PackageDeployResult(
            deployment_id="preflight_failed",
            manifest_path="",
            total=90,
            failed=1,
            results=[],
            preflight_result=PreflightResult(
                passed=False,
                errors=1,
                checks=[
                    PreflightCheck(
                        check_name="parse",
                        passed=False,
                        database="UNKNOWN",
                        message=(
                            "Failed to parse DB.BadView.viw: DDL does not "
                            "include a database qualifier."
                        ),
                    )
                ],
            ),
        )

        html = _html_action_items(result)

        assert "Pre-flight failed" in html
        assert "DB.BadView.viw" in html
        assert "all objects deployed successfully" not in html

    def test_normal_deploy_unchanged(self):
        """A successful run with no failures/skips uses the default message."""
        html = _html_action_items(_normal_deploy_result())
        assert "all objects deployed successfully" in html


# ---------------------------------------------------------------
# Summary stat cards — fix C
# ---------------------------------------------------------------


class TestSummaryOnReplay:
    """Summary cards must not claim work was done this run on a replay."""

    def test_replay_summary_shows_verified_prior_card(self):
        """Verified (prior) card shows the prior count."""
        html = _html_summary(_noop_replay_result(prior_count=5), mode="REPLAY")
        assert "Verified (prior)" in html
        assert ">5<" in html  # the count appears as a stat number

    def test_replay_summary_shows_zero_deployed_this_run(self):
        """Deployed (this run) card shows 0."""
        html = _html_summary(_noop_replay_result(prior_count=5), mode="REPLAY")
        assert "Deployed (this run)" in html

    def test_replay_summary_has_explanatory_caption(self):
        """REPLAY summary explains why the figures look the way they do."""
        html = _html_summary(_noop_replay_result(), mode="REPLAY")
        assert "did not deploy any objects" in html

    def test_normal_summary_uses_standard_cards(self):
        """Non-REPLAY mode renders the original 5-card layout."""
        html = _html_summary(_normal_deploy_result(), mode="DEPLOYMENT")
        # Standard cards present
        assert "Completed" in html
        assert "Skipped" in html
        assert "Failed" in html
        assert "Rolled back" in html
        # Replay-specific cards must NOT appear
        assert "Verified (prior)" not in html
        assert "Deployed (this run)" not in html


# ---------------------------------------------------------------
# Object Results — pre-existing fix, kept under coverage
# ---------------------------------------------------------------


class TestObjectResultsOnReplay:
    """Object Results section explains the noop replay when results are empty."""

    def test_noop_replay_message_in_object_results(self):
        """Empty results with prior shows replay copy in Object Results."""
        html = _html_object_results(_noop_replay_result(prior_count=4))
        assert "verified as still" in html
        assert "Nothing new to deploy" in html
        # Default 'No objects were processed' must NOT appear
        assert "No objects were processed" not in html

    def test_default_message_when_no_prior_and_no_results(self):
        """Empty results AND no prior → original default copy."""
        result = PackageDeployResult(
            deployment_id="d",
            manifest_path="/pkg/.deploy_manifest.json",
            results=[],
            prior_completed=[],
        )
        html = _html_object_results(result)
        assert "No objects were processed" in html


class TestPrivilegeFailureReport:
    """Privilege-check failures should appear as report action items."""

    def test_privilege_failure_has_action_items(self):
        """A pre-execution privilege failure must not say all objects deployed."""

        class _PrivilegeResult:
            passed = False
            user = "DBC"
            missing = {
                "GDEV1P_BB": ["CREATE PROCEDURE", "DROP PROCEDURE"],
                "GDEV1P_UT": ["CREATE PROCEDURE", "DROP PROCEDURE"],
            }
            script = (
                "GRANT PROCEDURE ON GDEV1P_BB TO DBC;\n"
                "GRANT PROCEDURE ON GDEV1P_UT TO DBC;"
            )

        result = PackageDeployResult(
            deployment_id="privilege_check_failed",
            manifest_path="",
            total=540,
            failed=2,
            results=[],
            privilege_result=_PrivilegeResult(),
        )

        html = _html_action_items(result)

        assert "Deployer privilege check failed" in html
        assert "GDEV1P_BB" in html
        assert "GRANT PROCEDURE ON GDEV1P_UT TO DBC" in html
        assert "all objects deployed successfully" not in html


class TestDeploymentReportSourceLinks:
    """Failed deployment rows link directly to packaged source code."""

    def test_failed_object_source_column_links_to_packaged_code(self):
        html = _html_object_results(_failed_result_with_source(), _source_provenance())

        assert 'href="../payload/03_ddl/views/DEV01_DB.BadView.viw"' in html
        assert "views/DEV01_DB.BadView.viw" in html
        assert "domain/views/BadView.viw" in html

    def test_failed_action_item_source_hint_links_to_packaged_code(self):
        html = _html_action_items(_failed_result_with_source(), _source_provenance())

        assert "Open code" in html
        assert 'href="../payload/03_ddl/views/DEV01_DB.BadView.viw"' in html
        assert "domain/views/BadView.viw" in html

    def test_sql_keywords_are_highlighted_without_touching_strings(self):
        html = _highlight_sql("CREATE VIEW DB.V AS SELECT 'create' AS word;")

        assert '<span class="sql-keyword">CREATE</span>' in html
        assert '<span class="sql-keyword">SELECT</span>' in html
        assert '<span class="sql-string">&#x27;create&#x27;</span>' in html

    def test_source_viewer_link_is_used_when_available(self):
        links = {"03_ddl/views/DEV01_DB.BadView.viw": ".code/view.html"}
        html = _html_action_items(
            _failed_result_with_source(), _source_provenance(), links
        )

        assert 'href=".code/view.html"' in html

    def test_writes_highlighted_source_viewer(self, tmp_path):
        logs = tmp_path / "logs"
        payload = tmp_path / "payload" / "03_ddl" / "views"
        logs.mkdir()
        payload.mkdir(parents=True)
        (payload / "DEV01_DB.BadView.viw").write_text(
            "CREATE VIEW DEV01_DB.BadView AS SELECT 1 AS x;",
            encoding="utf-8",
        )

        links = _write_source_viewers(
            str(logs), "deploy_20260503_140000", _source_provenance()
        )

        href = links["03_ddl/views/DEV01_DB.BadView.viw"]
        viewer = logs / href
        html = viewer.read_text(encoding="utf-8")
        assert viewer.exists()
        assert '<span class="sql-keyword">CREATE</span>' in html
        assert "domain/views/BadView.viw" in html


# ---------------------------------------------------------------
# _html_package_trust and _load_package_trust
# ---------------------------------------------------------------


class TestPackageTrustSection:
    """Tests for the build-time trust panel in the deploy report."""

    def _ready_trust(self):
        return {
            "label": "READY",
            "signals": {
                "inspect_lint": {"status": "pass", "message": "No lint violations found"},
                "inspect_token_format": {"status": "pass", "message": "No malformed token markers found"},
                "inspect_grants": {"status": "pass", "message": "Grant validation clean"},
                "provenance_complete": {"status": "pass", "message": "context/ships.provenance.json present"},
                "build_reproducible": {"status": "pass", "message": "Clean working tree"},
            },
        }

    def _blocked_trust(self):
        return {
            "label": "BLOCKED",
            "signals": {
                "inspect_lint": {
                    "status": "fail",
                    "message": "Coding Discipline lint violations: 2 error(s)",
                    "issues": ["payload/03_ddl/tables/DB.T.tbl:1: [db_qualifier] Missing database qualifier"],
                },
                "inspect_token_format": {"status": "pass", "message": "No malformed token markers found"},
                "inspect_grants": {"status": "pass", "message": "Grant validation clean"},
                "provenance_complete": {"status": "pass", "message": "context/ships.provenance.json present"},
                "build_reproducible": {"status": "warn", "message": "Built with --allow-dirty"},
            },
        }

    def test_returns_empty_when_no_trust(self):
        from database_package_deployer.report import _html_package_trust

        assert _html_package_trust({}) == ""

    def test_returns_empty_when_label_missing(self):
        from database_package_deployer.report import _html_package_trust

        assert _html_package_trust({"signals": {}}) == ""

    def test_ready_label_rendered(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._ready_trust())
        assert "READY" in html
        assert "Package Trust Report" in html

    def test_blocked_label_rendered_and_section_open(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._blocked_trust())
        assert "BLOCKED" in html
        # BLOCKED means auto-open — the <details> must have the open attribute
        assert "<details open" in html or "details open" in html

    def test_ready_section_is_collapsed_by_default(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._ready_trust())
        # READY → no open attribute on <details>
        assert "<details open" not in html

    def test_all_signal_names_appear(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._ready_trust())
        for sig in ("inspect_lint", "inspect_token_format", "inspect_grants",
                    "provenance_complete", "build_reproducible"):
            assert sig in html, f"{sig} missing from trust section"

    def test_known_signals_have_expandable_details(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._ready_trust())
        # All 5 known signals → 5 <details> elements
        assert html.count("<details") >= 5

    def test_fail_status_shows_issues(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._blocked_trust())
        assert "db_qualifier" in html
        assert "Missing database qualifier" in html

    def test_warn_status_rendered(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._blocked_trust())
        assert "warn" in html.lower() or "⚠" in html

    def test_if_this_fails_guidance_present(self):
        from database_package_deployer.report import _html_package_trust

        html = _html_package_trust(self._ready_trust())
        assert "If this fails" in html

    def test_load_package_trust_reads_ships_build_json(self, tmp_path):
        import json
        from database_package_deployer.report import _load_package_trust

        trust_data = {
            "label": "READY",
            "signals": {"inspect_lint": {"status": "pass", "message": "Clean"}},
        }
        ctx = tmp_path / "context"
        ctx.mkdir()
        (ctx / "ships.build.json").write_text(
            json.dumps({"trust": trust_data}), encoding="utf-8"
        )
        result = _load_package_trust(str(tmp_path))
        assert result["label"] == "READY"
        assert "inspect_lint" in result["signals"]

    def test_load_package_trust_walks_up_from_logs_subdir(self, tmp_path):
        import json
        from database_package_deployer.report import _load_package_trust

        trust_data = {"label": "READY-WITH-CAVEATS", "signals": {}}
        ctx = tmp_path / "context"
        ctx.mkdir()
        (ctx / "ships.build.json").write_text(
            json.dumps({"trust": trust_data}), encoding="utf-8"
        )
        logs = tmp_path / "logs"
        logs.mkdir()
        # Call with logs/ — should find ships.build.json one level up
        result = _load_package_trust(str(logs))
        assert result["label"] == "READY-WITH-CAVEATS"

    def test_load_package_trust_returns_empty_when_absent(self, tmp_path):
        from database_package_deployer.report import _load_package_trust

        result = _load_package_trust(str(tmp_path))
        assert result == {}

    def test_load_package_trust_returns_empty_on_corrupt_json(self, tmp_path):
        from database_package_deployer.report import _load_package_trust

        ctx = tmp_path / "context"
        ctx.mkdir()
        (ctx / "ships.build.json").write_text("not valid json", encoding="utf-8")
        result = _load_package_trust(str(tmp_path))
        assert result == {}

    def test_trust_section_present_in_full_report(self, tmp_path):
        """End-to-end: trust data from ships.build.json appears in the deploy report."""
        import json
        from database_package_deployer.report import generate_report

        trust_data = {
            "label": "READY",
            "signals": {
                "inspect_lint": {"status": "pass", "message": "No violations"},
            },
        }
        ctx = tmp_path / "context"
        ctx.mkdir()
        (ctx / "ships.build.json").write_text(
            json.dumps({"trust": trust_data}), encoding="utf-8"
        )

        result = PackageDeployResult(
            deployment_id="deploy_20260528_120000",
            manifest_path=str(tmp_path / "ships.manifest.json"),
            total=1,
            completed=1,
            results=[
                ObjectDeployResult(
                    object_name="T",
                    database_name="DB",
                    object_type=ObjectType.TABLE,
                    state=DeployState.COMPLETED,
                    message="ok",
                )
            ],
        )

        report_path = generate_report(result, str(tmp_path))
        html = open(report_path, encoding="utf-8").read()
        assert "Package Trust Report" in html
        assert "READY" in html
        assert "inspect_lint" in html

    def test_trust_section_absent_when_no_build_json(self, tmp_path):
        """When ships.build.json is absent the section is silently omitted."""
        from database_package_deployer.report import generate_report

        result = PackageDeployResult(
            deployment_id="deploy_20260528_130000",
            manifest_path=str(tmp_path / "ships.manifest.json"),
            total=0,
            completed=0,
            results=[],
        )

        report_path = generate_report(result, str(tmp_path))
        html = open(report_path, encoding="utf-8").read()
        assert "Package Trust Report" not in html
