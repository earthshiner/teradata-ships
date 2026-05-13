import json

from td_release_packager.context_artifacts import write_context_artifacts
from td_release_packager.models import BuildConfig, BuildManifest


def test_write_context_artifacts_emits_three_agent_files(tmp_path):
    manifest = BuildManifest(
        build_number="0007",
        environment="DEV",
        package_name="customer_risk",
        package_filename="DEV_customer_risk_BUILD_0007.zip",
        timestamp="2026-05-13T00:00:00+00:00",
        source_commit="abc123",
        token_count=2,
        file_count=4,
        phase_inventory={"03_ddl": 4},
        tokens_resolved={"CORE_T": "DEV_CORE_T", "CORE_V": "DEV_CORE_V"},
        trust={"label": "READY"},
        require_tls=True,
    )
    config = BuildConfig(
        source_dir="/repo/teradata-ships-demo",
        environment="DEV",
        package_name="customer_risk",
        env_config_file="config/env/DEV.conf",
    )

    written = write_context_artifacts(str(tmp_path), manifest, config)

    assert sorted(written) == [
        "ships.context.json",
        "ships.handoff.json",
        "ships.manifest.json",
    ]

    context = json.loads((tmp_path / "ships.context.json").read_text())
    agent_manifest = json.loads((tmp_path / "ships.manifest.json").read_text())
    handoff = json.loads((tmp_path / "ships.handoff.json").read_text())

    assert context["current_state"] == "package-built-awaiting-deployment"
    assert context["source_of_truth"]["source_commit"] == "abc123"
    assert agent_manifest["tokens"]["values_redacted"] is True
    assert agent_manifest["tokens"]["token_names"] == ["CORE_T", "CORE_V"]
    assert handoff["preconditions"]["tls_required"] is True
    assert "BUILD.json" in handoff["references"].values()
