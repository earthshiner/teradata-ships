"""``hardcoded_name`` fixer Phase 2a — exceptions file, smart proposals, MCP tools (#541).

Extends Phase 1's plan-file workflow with three new inputs and two new
agent-facing tools:

* ``.ships/hardcoded_name.exceptions.json`` — persistent operator-declared
  "always skip this literal" list, merged with the built-in system-database
  set at scan time.
* ``config/tokenise.conf`` — when present, its substitution rules drive
  smart token proposals (``ProdDB`` becomes ``{{DB_PREFIX}}`` rather than
  ``{{ProdDB}}``). Fallback is verbatim wrap when no rule matches.
* MCP tools ``ships_propose_hardcoded_name_plan`` and
  ``ships_apply_hardcoded_name_plan`` — expose the plan/apply cycle to
  agents without going through the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

from td_release_packager.fixers.hardcoded_name import (
    _EXCEPTIONS_RELATIVE_PATH,
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


def _exceptions_path(project: Path) -> Path:
    return project / _EXCEPTIONS_RELATIVE_PATH


# ---------------------------------------------------------------
# Exceptions file
# ---------------------------------------------------------------


class TestExceptionsFile:
    def test_literal_in_exceptions_is_not_proposed(self, tmp_path):
        """A literal listed in ``.ships/hardcoded_name.exceptions.json``
        must be skipped by the discovery walk — no proposal, no plan
        entry, no rewrite."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW LegacyDB.V AS SELECT 1 FROM Prod_STD_T.T;\n",
        )
        # Operator has previously declared LegacyDB as always-skip.
        _write(
            _exceptions_path(project),
            json.dumps(
                {"schema_version": 1, "exclude": ["LegacyDB"]},
            ),
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        literals = {p["literal"] for p in plan["proposals"]}
        # LegacyDB skipped, Prod_STD_T still proposed.
        assert "LegacyDB" not in literals
        assert "Prod_STD_T" in literals

    def test_exceptions_persist_across_runs(self, tmp_path):
        """The exceptions file is on disk, so a second run consulting
        the same file must skip the same literals — nothing about the
        exceptions state should be tied to a single fixer invocation."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW LegacyDB.V AS SELECT 1;\n",
        )
        _write(
            _exceptions_path(project),
            json.dumps({"schema_version": 1, "exclude": ["LegacyDB"]}),
        )
        # First run — should propose nothing (LegacyDB excluded, no
        # other literals in this file).
        fix_hardcoded_name(str(project))
        # No plan written because there were zero proposals.
        assert not _plan_path(project).exists()
        # Second run — same story.
        fix_hardcoded_name(str(project))
        assert not _plan_path(project).exists()

    def test_malformed_exceptions_file_is_treated_as_empty(self, tmp_path):
        """A corrupt exceptions file must not crash the fixer — it
        should behave as if the file was absent."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        _write(_exceptions_path(project), "not json at all")
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        # ProdDB was NOT in a well-formed exception list, so it must
        # appear in the proposals.
        assert "ProdDB" in {p["literal"] for p in plan["proposals"]}


# ---------------------------------------------------------------
# Smart proposals via config/tokenise.conf
# ---------------------------------------------------------------


class TestSmartProposals:
    def test_tokenise_conf_regex_rule_drives_token_name(self, tmp_path):
        """When a regex rule in tokenise.conf matches the literal,
        its transformed output becomes the proposed token — instead
        of the verbatim wrap. Real projects rarely want
        ``{{Prod_STD_T}}``; they want ``{{DB_PREFIX}}_STD_T`` or
        similar produced by the tokenise.conf rules."""
        project = _setup_project(tmp_path)
        (project / "config").mkdir()
        _write(
            project / "config/tokenise.conf",
            "regex::^Prod$:={{DB_PREFIX}}\n",
        )
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW Prod.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        prop = next(p for p in plan["proposals"] if p["literal"] == "Prod")
        assert prop["token"] == "{{DB_PREFIX}}"

    def test_no_matching_rule_falls_back_to_verbatim_wrap(self, tmp_path):
        """A tokenise.conf that doesn't match a given literal should
        leave the proposal at the verbatim ``{{literal}}`` wrap — the
        fallback should never fire silently for a literal a rule was
        expected to transform."""
        project = _setup_project(tmp_path)
        (project / "config").mkdir()
        _write(
            project / "config/tokenise.conf",
            "regex::^Prod$:={{DB_PREFIX}}\n",  # only matches "Prod"
        )
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW OtherDB.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        prop = next(p for p in plan["proposals"] if p["literal"] == "OtherDB")
        assert prop["token"] == "{{OtherDB}}"

    def test_rule_producing_non_token_output_falls_back(self, tmp_path):
        """A tokenise.conf rule that transforms the literal into a
        result WITHOUT ``{{...}}`` markers is not a real tokenisation
        — the fixer must not accept it. Prevents accidental
        de-tokenisation via a stray rewrite rule."""
        project = _setup_project(tmp_path)
        (project / "config").mkdir()
        # This rule rewrites "Prod" to "STAGING" — an operator error;
        # a real tokenise.conf rule would produce ``{{TOKEN}}``.
        _write(
            project / "config/tokenise.conf",
            "regex::^Prod$:=STAGING\n",
        )
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW Prod.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        prop = next(p for p in plan["proposals"] if p["literal"] == "Prod")
        # Rule didn't produce a token-shaped output, so fall back.
        assert prop["token"] == "{{Prod}}"

    def test_no_tokenise_conf_uses_verbatim_wrap(self, tmp_path):
        """Baseline — Phase 1 behaviour continues to work when the
        project has no tokenise.conf."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        prop = next(p for p in plan["proposals"] if p["literal"] == "ProdDB")
        assert prop["token"] == "{{ProdDB}}"


# ---------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------


class TestMcpTools:
    def test_propose_returns_a_plan_without_writing(self, tmp_path):
        """The propose MCP tool must be side-effect-free: no plan file
        on disk after calling it, and the returned envelope carries the
        full plan for an agent to hand to a human for review."""
        from ships_mcp import ships_propose_hardcoded_name_plan

        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        envelope = ships_propose_hardcoded_name_plan(project=str(project))
        assert envelope["success"] is True
        assert envelope["plan"]["schema_version"] == 1
        proposals = envelope["plan"]["proposals"]
        assert any(p["literal"] == "ProdDB" for p in proposals)
        # No plan file on disk — the propose tool is read-only.
        assert not _plan_path(project).exists()

    def test_apply_reads_provided_plan_and_rewrites_payload(self, tmp_path):
        """The apply MCP tool must take an operator-approved plan and
        write to the payload as if the fixer had been driven manually."""
        from ships_mcp import ships_apply_hardcoded_name_plan

        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        plan = {
            "schema_version": 1,
            "proposals": [
                {
                    "literal": "ProdDB",
                    "token": "{{PROD_DB}}",
                    "occurrences": [],
                }
            ],
        }
        envelope = ships_apply_hardcoded_name_plan(project=str(project), plan=plan)
        assert envelope["success"] is True
        text = f.read_text(encoding="utf-8")
        assert "{{PROD_DB}}.V" in text
        assert "ProdDB.V" not in text

    def test_apply_dry_run_reports_without_writing(self, tmp_path):
        """Dry-run through the MCP tool must be strictly read-only,
        even though the tool has to synthesise a plan file for the
        fixer to consume."""
        from ships_mcp import ships_apply_hardcoded_name_plan

        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        original = f.read_text(encoding="utf-8")
        envelope = ships_apply_hardcoded_name_plan(
            project=str(project),
            plan={
                "schema_version": 1,
                "proposals": [
                    {"literal": "ProdDB", "token": "{{PROD}}", "occurrences": []}
                ],
            },
            dry_run=True,
        )
        assert envelope["success"] is True
        assert envelope["dry_run"] is True
        # Payload untouched.
        assert f.read_text(encoding="utf-8") == original
        # Plan file should not remain behind after a dry-run either.
        assert not _plan_path(project).exists()

    def test_apply_rejects_malformed_plan(self, tmp_path):
        from ships_mcp import ships_apply_hardcoded_name_plan

        project = _setup_project(tmp_path)
        envelope = ships_apply_hardcoded_name_plan(
            project=str(project), plan={"not_a_plan": True}
        )
        assert envelope["success"] is False
        assert "proposals" in envelope["error"]
