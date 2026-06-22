"""Tests for the inspect-rule remediation catalogue (#144)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator

from td_release_packager.rules_catalogue import (
    RULES_RESULT_FILENAME,
    RULES_RESULT_REF,
    RULES_SCHEMA_VERSION,
    compute_rules_document,
    load_rules_result,
    remediation_for,
    rule_codes,
    write_rules_result,
)


_REQUIRED_FIELDS = {
    "description",
    "default_severity",
    "safe_fix_available",
    "automation_level",
    "recommended_action",
    "risk",
    "requires_human_review",
}


class TestDocumentShape:
    def test_schema_version_present(self):
        doc = compute_rules_document()
        assert doc["schema_version"] == RULES_SCHEMA_VERSION

    def test_top_level_keys(self):
        doc = compute_rules_document()
        assert set(doc.keys()) >= {"schema_version", "generated_by", "rules"}

    def test_rules_dict_non_empty(self):
        doc = compute_rules_document()
        assert isinstance(doc["rules"], dict)
        assert len(doc["rules"]) > 0

    def test_every_rule_has_all_required_fields(self):
        doc = compute_rules_document()
        for code, meta in doc["rules"].items():
            missing = _REQUIRED_FIELDS - set(meta.keys())
            assert not missing, f"{code} missing fields: {missing}"

    def test_field_enums(self):
        doc = compute_rules_document()
        for code, meta in doc["rules"].items():
            assert meta["default_severity"] in {
                "ERROR",
                "WARNING",
                "INFO",
                "OFF",
            }, code
            assert meta["automation_level"] in {"auto", "guided", "manual"}, code
            assert meta["risk"] in {"low", "medium", "high"}, code
            assert isinstance(meta["safe_fix_available"], bool), code
            assert isinstance(meta["requires_human_review"], bool), code
            assert meta["description"].strip(), code
            assert meta["recommended_action"].strip(), code


class TestRegistryAlignment:
    """The catalogue must cover every rule the validator can emit."""

    def test_catalogue_covers_all_default_rules(self):
        from td_release_packager.validate import DEFAULT_RULES

        missing = set(DEFAULT_RULES.keys()) - set(rule_codes())
        assert not missing, (
            f"DEFAULT_RULES entries without remediation metadata: {missing}"
        )

    def test_catalogue_severities_agree_with_default_rules(self):
        from td_release_packager.validate import DEFAULT_RULES

        doc = compute_rules_document()
        for code, severity in DEFAULT_RULES.items():
            assert doc["rules"][code]["default_severity"] == severity, code


class TestRiskInvariants:
    """High-risk rules must require human review."""

    def test_high_risk_implies_human_review(self):
        doc = compute_rules_document()
        for code, meta in doc["rules"].items():
            if meta["risk"] == "high":
                assert meta["requires_human_review"] is True, code
                assert meta["automation_level"] == "manual", code

    def test_no_safe_fix_implies_not_auto(self):
        """A rule with no safe fix cannot be auto-applied."""
        doc = compute_rules_document()
        for code, meta in doc["rules"].items():
            if not meta["safe_fix_available"]:
                assert meta["automation_level"] != "auto", code


class TestLookups:
    def test_remediation_for_known_code(self):
        meta = remediation_for("db_qualifier")
        assert meta is not None
        assert meta["default_severity"] == "ERROR"

    def test_remediation_for_unknown_code(self):
        assert remediation_for("totally-not-a-rule") is None

    def test_rule_codes_returns_list(self):
        codes = rule_codes()
        assert isinstance(codes, list)
        assert "db_qualifier" in codes
        assert "secret_scan" in codes


class TestRoundTrip:
    def test_write_then_load(self, tmp_path):
        write_rules_result(str(tmp_path))
        path = tmp_path / "context" / RULES_RESULT_FILENAME
        assert path.is_file()
        loaded = load_rules_result(str(tmp_path))
        assert loaded is not None
        assert loaded["schema_version"] == RULES_SCHEMA_VERSION

    def test_load_missing_returns_none(self, tmp_path):
        assert load_rules_result(str(tmp_path)) is None

    def test_ref_constant_matches_filename(self):
        assert RULES_RESULT_REF.endswith(RULES_RESULT_FILENAME)
        assert RULES_RESULT_REF.startswith("context/")


class TestSchemaValidation:
    """The published JSON schema must validate the catalogue document."""

    def test_document_validates_against_schema(self):
        from td_release_packager.context_artifacts import DEFAULT_SCHEMAS

        schema = DEFAULT_SCHEMAS["ships.rules.schema.json"]
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(compute_rules_document())


class TestBuildPackageEmitsRules:
    """End-to-end: a built package must carry the rules catalogue."""

    def test_package_contains_rules_artefact(self, tmp_path):
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        # Minimal project layout
        project = tmp_path / "proj"
        payload = project / "payload" / "database" / "DDL" / "tables"
        payload.mkdir(parents=True)
        (payload / "DOM_T.TBL.tbl").write_text(
            "CREATE TABLE {{CORE_T}}.Customer (id INT);\n",
            encoding="utf-8",
        )
        env_conf = project / "config" / "env" / "DEV.conf"
        env_conf.parent.mkdir(parents=True)
        env_conf.write_text("CORE_T = DEV_CORE_T\n", encoding="utf-8")
        (project / ".ships").mkdir(parents=True, exist_ok=True)
        (project / ".ships" / ".build_counter").write_text("0", encoding="utf-8")

        config = BuildConfig(
            source_dir=str(project),
            environment="DEV",
            package_name="rules_smoke",
            env_config_file=str(env_conf),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (archive_path, _manifest), _companion = build_package(config)

        # Inspect the produced archive
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            assert any(n.endswith("context/ships.rules.json") for n in names), names
            assert any(
                n.endswith("context/schemas/ships.rules.schema.json") for n in names
            ), names

            # Manifest carries rules_ref
            build_entry = next(
                n for n in names if n.endswith("context/ships.build.json")
            )
            build_doc = json.loads(zf.read(build_entry))
            assert build_doc["rules_ref"] == RULES_RESULT_REF

            # Index entry exposes the rules entrypoint
            index_entry = next(
                n for n in names if n.endswith("context/ships.index.json")
            )
            index_doc = json.loads(zf.read(index_entry))
            assert "rules" in index_doc["entrypoints"]
            assert index_doc["entrypoints"]["rules"]["path"] == RULES_RESULT_REF

            # rules_ref is threaded through context / manifest / handoff
            for doc_name in (
                "context/ships.context.json",
                "context/ships.manifest.json",
                "context/ships.handoff.json",
            ):
                entry = next(n for n in names if n.endswith(doc_name))
                doc = json.loads(zf.read(entry))
                assert doc["rules_ref"] == RULES_RESULT_REF, doc_name

            # The rules document itself is well-formed
            rules_entry = next(
                n for n in names if n.endswith("context/ships.rules.json")
            )
            rules_doc = json.loads(zf.read(rules_entry))
            assert rules_doc["schema_version"] == RULES_SCHEMA_VERSION
            assert "db_qualifier" in rules_doc["rules"]
