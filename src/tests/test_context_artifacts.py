import json

from td_release_packager.context_artifacts import write_context_artifacts
from td_release_packager.models import BuildConfig, BuildManifest


def test_write_context_artifacts_emits_agent_context_contract(tmp_path):
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
        "ships.index.json",
        "ships.manifest.json",
    ]

    index = json.loads((tmp_path / "ships.index.json").read_text())
    context = json.loads((tmp_path / "ships.context.json").read_text())
    agent_manifest = json.loads((tmp_path / "ships.manifest.json").read_text())
    handoff = json.loads((tmp_path / "ships.handoff.json").read_text())

    assert index["read_first"] == "ships.index.json"
    assert index["recommended_read_order"][:4] == [
        "index",
        "handoff",
        "context",
        "build",
    ]
    assert index["entrypoints"]["integrity"]["path"] == "ships.integrity.json"
    assert "tamper-evidence" in index["entrypoints"]["integrity"]["description"]
    assert index["entrypoints"]["decisions"]["path"] == "ships.decisions.json"
    assert index["agent_instructions"]["before_action"][0].startswith(
        "Read ships.index.json"
    )

    assert context["current_state"] == "package-built-awaiting-deployment"
    assert context["source_of_truth"]["source_commit"] == "abc123"
    assert context["references"]["index"] == "ships.index.json"
    assert agent_manifest["tokens"]["values_redacted"] is True
    assert agent_manifest["tokens"]["token_names"] == ["CORE_T", "CORE_V"]
    assert handoff["preconditions"]["tls_required"] is True
    assert handoff["required_actions"][0].startswith("Read ships.index.json")
