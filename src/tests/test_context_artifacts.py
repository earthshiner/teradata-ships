import json

from jsonschema import Draft202012Validator

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
        trust={"trust_ref": "context/ships.trust.json"},
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
        "prompts/README.md",
        "prompts/agent_operating_instructions.prompt.md",
        "prompts/deployment_agent.prompt.md",
        "prompts/evidence_agent.prompt.md",
        "prompts/remediation_agent.prompt.md",
        "prompts/verification_agent.prompt.md",
        "schemas/ships.actions.schema.json",
        "schemas/ships.build.schema.json",
        "schemas/ships.context.schema.json",
        "schemas/ships.handoff.schema.json",
        "schemas/ships.index.schema.json",
        "schemas/ships.integrity.schema.json",
        "schemas/ships.manifest.schema.json",
        "schemas/ships.provenance.schema.json",
        "schemas/ships.trust.schema.json",
        "ships.context.json",
        "ships.handoff.json",
        "ships.index.json",
        "ships.manifest.json",
    ]

    index = json.loads((tmp_path / "context" / "ships.index.json").read_text())
    context = json.loads((tmp_path / "context" / "ships.context.json").read_text())
    agent_manifest = json.loads(
        (tmp_path / "context" / "ships.manifest.json").read_text()
    )
    handoff = json.loads((tmp_path / "context" / "ships.handoff.json").read_text())

    assert index["read_first"] == "context/ships.index.json"
    assert index["recommended_read_order"][:4] == [
        "index",
        "handoff",
        "context",
        "build",
    ]
    assert index["entrypoints"]["integrity"]["path"] == "context/ships.integrity.json"
    assert "tamper-evidence" in index["entrypoints"]["integrity"]["description"]
    assert index["entrypoints"]["stage_results"]["path"] == "context/stages/"
    assert (
        "context/stages/process.result.json"
        in index["entrypoints"]["stage_results"]["contains"]
    )
    assert index["entrypoints"]["decisions"]["path"] == "ships.decisions.json"
    assert index["entrypoints"]["decisions"]["package_local"] is False
    assert index["entrypoints"]["prompts"]["path"] == "context/prompts/"
    assert (
        "context/prompts/deployment_agent.prompt.md"
        in index["entrypoints"]["prompts"]["contains"]
    )
    assert index["entrypoints"]["schemas"]["path"] == "context/schemas/"
    assert (
        "context/schemas/ships.index.schema.json"
        in index["entrypoints"]["schemas"]["contains"]
    )
    assert "stage_results" in index["recommended_read_order"]
    assert "prompts" in index["recommended_read_order"]
    assert "schemas" in index["recommended_read_order"]
    assert index["agent_instructions"]["before_action"][0].startswith(
        "Read context/ships.index.json"
    )
    assert index["agent_policy"]["do_not_infer_missing_tokens"] is True
    assert index["agent_policy"]["do_not_modify_payload"] is True
    assert index["agent_policy"]["do_not_deploy_if_blocked"] is True
    assert index["agent_policy"]["do_not_ignore_failed_integrity"] is True
    assert index["agent_policy"]["payload_modification_allowed"] is False
    assert "preflight_error" in index["agent_policy"]["stop_conditions"]
    assert (
        "tls_policy_not_satisfied"
        in index["agent_policy"]["ask_for_human_approval_when"]
    )

    assert context["current_state"] == "package-built-awaiting-deployment"
    assert context["source_of_truth"]["source_commit"] == "abc123"
    assert context["references"]["index"] == "context/ships.index.json"
    assert context["agent_policy"]["do_not_modify_payload"] is True
    assert agent_manifest["agent_policy"]["do_not_ignore_failed_integrity"] is True
    assert agent_manifest["tokens"]["values_redacted"] is True
    assert agent_manifest["tokens"]["token_names"] == ["CORE_T", "CORE_V"]
    assert handoff["preconditions"]["tls_required"] is True
    assert handoff["agent_policy"]["deployment_allowed_when_trust_blocked"] is False
    assert handoff["required_actions"][0].startswith("Read context/ships.index.json")

    prompts_dir = tmp_path / "context" / "prompts"
    assert (prompts_dir / "README.md").is_file()
    agent_prompt = prompts_dir / "agent_operating_instructions.prompt.md"
    deploy_prompt = prompts_dir / "deployment_agent.prompt.md"
    remediation_prompt = prompts_dir / "remediation_agent.prompt.md"
    assert "Do not deploy if package trust is BLOCKED" in agent_prompt.read_text()
    assert "Your task is to deploy a SHIPS package" in deploy_prompt.read_text()
    assert "View column lists must not be invented" in remediation_prompt.read_text()

    schemas_dir = tmp_path / "context" / "schemas"
    index_schema = json.loads((schemas_dir / "ships.index.schema.json").read_text())
    assert index_schema["required"] == [
        "schema",
        "schema_version",
        "package_type",
        "read_first",
        "entrypoints",
        "recommended_read_order",
        "agent_policy",
    ]
    assert index["$schema"] == "./schemas/ships.index.schema.json"

    for document_name, document in {
        "ships.index": index,
        "ships.context": context,
        "ships.manifest": agent_manifest,
        "ships.handoff": handoff,
    }.items():
        schema_file = schemas_dir / f"{document_name}.schema.json"
        schema = json.loads(schema_file.read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
        assert schema["description"]
        assert schema["examples"]
