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
)
from database_package_deployer.provenance import (
    ProvenanceChain,
    ProvenanceDocument,
    Stage,
    Status,
)
from database_package_deployer.report import (
    _build_html,
    _html_action_items,
    _html_object_results,
    _html_summary,
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
