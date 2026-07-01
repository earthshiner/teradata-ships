"""``hardcoded_name`` fixer (#527 — Phase 1 MVP).

Covers the plan-file workflow:

* First run (no plan on disk) — the fixer walks the payload, proposes
  a ``{{literal}}`` token for each hardcoded qualifier, and writes
  ``.ships/hardcoded_name.plan.json``. No payload writes.
* Second run (plan on disk) — the fixer reads the plan and rewrites
  every ``literal`` in payload files to the paired ``token``, then
  deletes the plan so the next run starts a fresh proposal cycle.

Interactive TTY / MCP / config-file updates / smart proposals via
``config/tokenise.conf`` are Phase 2 follow-ups.
"""

from __future__ import annotations

import json
from pathlib import Path

from td_release_packager.fixers import FIX_REGISTRY
from td_release_packager.fixers.hardcoded_name import (
    _PLAN_RELATIVE_PATH,
    fix_hardcoded_name,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "views").mkdir(parents=True)
    return project


def _plan_path(project: Path) -> Path:
    return project / _PLAN_RELATIVE_PATH


# ---------------------------------------------------------------
# Registry entry
# ---------------------------------------------------------------


class TestRegistryEntry:
    def test_hardcoded_name_is_registered(self):
        assert "hardcoded_name" in FIX_REGISTRY

    def test_hardcoded_name_is_opt_in(self):
        """Opt-in because token naming is judgement-laden — the fixer
        never runs under a bare ``ships fix`` invocation."""
        assert FIX_REGISTRY["hardcoded_name"].default_on is False

    def test_hardcoded_name_writes_to_payload(self):
        assert FIX_REGISTRY["hardcoded_name"].write_scope == "payload"


# ---------------------------------------------------------------
# Propose mode
# ---------------------------------------------------------------


class TestProposeMode:
    def test_first_run_writes_plan_file_no_payload_writes(self, tmp_path):
        project = _setup_project(tmp_path)
        view = _write(
            project / "payload/database/DDL/views/Prod.V.viw",
            "REPLACE VIEW ProdDB.CustomerV AS SELECT c.Id FROM ProdDB.Customer c;\n",
        )
        original = view.read_text(encoding="utf-8")

        result = fix_hardcoded_name(str(project))

        # Payload untouched.
        assert view.read_text(encoding="utf-8") == original
        # Plan on disk.
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        assert plan["schema_version"] == 1
        proposals = plan["proposals"]
        assert len(proposals) == 1
        assert proposals[0]["literal"] == "ProdDB"
        assert proposals[0]["token"] == "{{ProdDB}}"
        # Result reports propose mode.
        assert result.totals.get("proposals") == 1
        assert result.totals.get("substitutions") is None

    def test_unique_literals_deduplicated_across_files(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/One.viw",
            "REPLACE VIEW ProdDB.A AS SELECT 1 FROM ProdDB.T;\n",
        )
        _write(
            project / "payload/database/DDL/views/Two.viw",
            "REPLACE VIEW ProdDB.B AS SELECT 2 FROM ProdDB.T;\n",
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        # One proposal per unique literal (ProdDB), regardless of file count.
        assert [p["literal"] for p in plan["proposals"]] == ["ProdDB"]
        # And every occurrence is listed under it.
        occurrences = plan["proposals"][0]["occurrences"]
        assert len(occurrences) >= 2  # two files at minimum

    def test_system_databases_are_not_proposed(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/S.viw",
            "REPLACE VIEW ProdDB.V AS SELECT * FROM DBC.SessionInfo;\n",
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        literals = {p["literal"] for p in plan["proposals"]}
        assert "DBC" not in literals
        assert "ProdDB" in literals

    def test_already_tokenised_refs_are_not_proposed(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/T.viw",
            "REPLACE VIEW {{DB_V}}.V AS SELECT * FROM {{DB_T}}.T;\n",
        )
        fix_hardcoded_name(str(project))
        # No proposals → no plan file created.
        assert not _plan_path(project).exists()

    def test_dry_run_does_not_write_plan(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Dry.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        result = fix_hardcoded_name(str(project), dry_run=True)
        assert not _plan_path(project).exists()
        # But the count of what would be proposed is still reported.
        assert result.totals.get("proposals") == 1


# ---------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------


class TestApplyMode:
    def test_second_run_reads_plan_and_rewrites_payload(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT c.Id FROM ProdDB.Customer c;\n",
        )
        # Two-pass workflow.
        fix_hardcoded_name(str(project))  # writes plan
        assert _plan_path(project).exists()
        second = fix_hardcoded_name(str(project))  # applies

        text = f.read_text(encoding="utf-8")
        # No bare ``ProdDB.`` qualifier — every reference should now be
        # tokenised. (``{{ProdDB}}`` contains the substring "ProdDB" so
        # the assertion is on the *bare* form, not the substring.)
        assert "ProdDB." not in text.replace("{{ProdDB}}", "")
        assert "{{ProdDB}}.V" in text
        assert "{{ProdDB}}.Customer" in text
        assert second.totals.get("substitutions", 0) >= 2
        # Plan is consumed so a subsequent run starts fresh.
        assert not _plan_path(project).exists()

    def test_edited_plan_is_respected(self, tmp_path):
        """Operators can rename a token in the plan before applying."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/E.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1 FROM ProdDB.T;\n",
        )
        fix_hardcoded_name(str(project))  # writes plan

        # Operator edits the plan to rename the token.
        plan_file = _plan_path(project)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        plan["proposals"][0]["token"] = "{{DB_PREFIX}}"
        plan_file.write_text(json.dumps(plan), encoding="utf-8", newline="")

        fix_hardcoded_name(str(project))  # applies

        text = f.read_text(encoding="utf-8")
        assert "{{DB_PREFIX}}.V" in text
        assert "{{DB_PREFIX}}.T" in text

    def test_dropped_plan_entry_is_skipped(self, tmp_path):
        """If the operator deletes a proposal from the plan, the fixer
        leaves that literal alone."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/D.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1 FROM OtherDB.T;\n",
        )
        original = f.read_text(encoding="utf-8")

        fix_hardcoded_name(str(project))  # writes plan
        plan_file = _plan_path(project)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        # Drop the OtherDB proposal.
        plan["proposals"] = [p for p in plan["proposals"] if p["literal"] != "OtherDB"]
        plan_file.write_text(json.dumps(plan), encoding="utf-8", newline="")

        fix_hardcoded_name(str(project))  # applies

        text = f.read_text(encoding="utf-8")
        # ProdDB rewritten as expected.
        assert "{{ProdDB}}.V" in text
        # OtherDB left alone.
        assert "OtherDB.T" in text
        # Sanity: only the ProdDB substitution happened.
        assert text.count("OtherDB") == original.count("OtherDB")

    def test_dry_run_apply_does_not_write_and_keeps_plan(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Dry.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        original = f.read_text(encoding="utf-8")
        fix_hardcoded_name(str(project))  # writes plan
        assert _plan_path(project).exists()

        fix_hardcoded_name(str(project), dry_run=True)

        # Payload untouched.
        assert f.read_text(encoding="utf-8") == original
        # Plan still on disk — dry-run doesn't consume it.
        assert _plan_path(project).exists()


# ---------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------


class TestEdgeCases:
    def test_refs_in_comments_are_ignored(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Cm.viw",
            "-- reads from ProdDB.OldTable historically\n"
            "REPLACE VIEW {{DB}}.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        # Only the tokenised ref exists in real code; ProdDB is
        # comment-only so nothing is proposed.
        assert not _plan_path(project).exists()

    def test_refs_in_string_literals_are_ignored(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Str.viw",
            "REPLACE VIEW {{DB}}.V AS SELECT 'ProdDB.X' AS msg;\n",
        )
        fix_hardcoded_name(str(project))
        assert not _plan_path(project).exists()

    def test_malformed_plan_is_ignored(self, tmp_path):
        """A corrupt plan file must not crash the fixer — it should
        treat it as absent and re-enter propose mode."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/M.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        # Write garbage to the plan file location.
        (project / ".ships").mkdir(exist_ok=True)
        (project / ".ships" / "hardcoded_name.plan.json").write_text(
            "not json", encoding="utf-8", newline=""
        )

        fix_hardcoded_name(str(project))
        # Plan file was overwritten with a real proposal.
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        assert plan["schema_version"] == 1
        assert len(plan["proposals"]) == 1
