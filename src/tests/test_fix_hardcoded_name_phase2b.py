"""``hardcoded_name`` fixer Phase 2b — atomic config updates + interactive review (#541).

Two orthogonal features layered on top of Phase 2a's exceptions +
smart proposals + MCP tools:

* Apply mode now extends ``config/tokenise.conf`` (regex substitution
  rules) and ``config/token_map.conf`` (literal → token map) atomically
  with the payload rewrite. If any config write fails, every payload
  rewrite is rolled back to its pre-fix content.
* :func:`interactive_review` walks an existing plan one proposal at a
  time with y/e/s/S/q actions. Testable via ``input_stream`` /
  ``output_stream`` injection so we can drive scripted sessions
  without a TTY.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from td_release_packager.fixers.hardcoded_name import (
    _EXCEPTIONS_RELATIVE_PATH,
    _PLAN_RELATIVE_PATH,
    fix_hardcoded_name,
    interactive_review,
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


def _drive(inputs: str) -> io.StringIO:
    """Wrap an operator-script string as an input stream."""
    return io.StringIO(inputs)


# ---------------------------------------------------------------
# Atomic config updates
# ---------------------------------------------------------------


class TestConfigUpdates:
    def test_apply_extends_tokenise_conf(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        # First run — propose. Second run — apply.
        fix_hardcoded_name(str(project))
        fix_hardcoded_name(str(project))

        conf = (project / "config/tokenise.conf").read_text(encoding="utf-8")
        assert "regex::^ProdDB$:={{ProdDB}}" in conf

    def test_apply_extends_token_map_conf(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        fix_hardcoded_name(str(project))

        conf = (project / "config/token_map.conf").read_text(encoding="utf-8")
        assert "ProdDB={{ProdDB}}" in conf

    def test_apply_is_idempotent_on_config_files(self, tmp_path):
        """Applying the same plan twice shouldn't duplicate config
        entries. The second run has no plan (Phase 1 consumes it), so
        this test re-seeds the plan file to exercise the idempotency
        of the config writers directly."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))  # propose
        # Save the plan so we can re-run apply cleanly.
        plan_text = _plan_path(project).read_text(encoding="utf-8")
        fix_hardcoded_name(str(project))  # apply (consumes plan)

        # Re-materialise the plan and re-run apply.
        _write(_plan_path(project), plan_text)
        fix_hardcoded_name(str(project))

        tokenise = (project / "config/tokenise.conf").read_text(encoding="utf-8")
        assert tokenise.count("regex::^ProdDB$:={{ProdDB}}") == 1

        token_map = (project / "config/token_map.conf").read_text(encoding="utf-8")
        # Filter to the mapping line; exact-once.
        map_lines = [
            line for line in token_map.splitlines() if line.startswith("ProdDB=")
        ]
        assert len(map_lines) == 1

    def test_apply_preserves_existing_tokenise_conf_content(self, tmp_path):
        """The writer must APPEND — never overwrite the operator's
        existing rules."""
        project = _setup_project(tmp_path)
        (project / "config").mkdir()
        _write(
            project / "config/tokenise.conf",
            "# operator-authored rule below:\nregex::^Existing$:={{PRESERVED}}\n",
        )
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))
        fix_hardcoded_name(str(project))

        conf = (project / "config/tokenise.conf").read_text(encoding="utf-8")
        assert "regex::^Existing$:={{PRESERVED}}" in conf
        assert "regex::^ProdDB$:={{ProdDB}}" in conf

    def test_dry_run_apply_does_not_write_config(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        fix_hardcoded_name(str(project))  # propose (writes plan)
        fix_hardcoded_name(str(project), dry_run=True)  # dry-run apply

        # No config files created under dry_run.
        assert not (project / "config/tokenise.conf").exists()
        assert not (project / "config/token_map.conf").exists()


class TestApplyRollback:
    def test_config_write_failure_rolls_back_payload(self, tmp_path, monkeypatch):
        """If the config writer raises, every payload file rewritten in
        the current apply pass must be restored to its pre-fix content."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/V.viw",
            "REPLACE VIEW ProdDB.V AS SELECT 1;\n",
        )
        original = f.read_text(encoding="utf-8")

        fix_hardcoded_name(str(project))  # propose

        # Force the tokenise.conf writer to fail.
        from td_release_packager.fixers import hardcoded_name as hn

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(hn, "_extend_tokenise_conf", _boom)

        result = fix_hardcoded_name(str(project))  # apply

        # Payload rolled back.
        assert f.read_text(encoding="utf-8") == original
        # No files_changed reported on the result.
        assert not result.files_changed
        # Error entry surfaces the config-write failure.
        assert any("config" in e.get("file", "") for e in result.errors)


# ---------------------------------------------------------------
# Interactive review helper
# ---------------------------------------------------------------


def _seed_plan(project: Path, tokens: dict[str, str]) -> None:
    """Write a plan file with one proposal per (literal, token) pair."""
    plan = {
        "schema_version": 1,
        "proposals": [
            {
                "literal": literal,
                "token": token,
                "occurrences": [
                    {"file": "payload/database/DDL/views/A.viw", "line": 1}
                ],
            }
            for literal, token in tokens.items()
        ],
    }
    _write(_plan_path(project), json.dumps(plan))


class TestInteractiveReview:
    def test_accept_keeps_proposal_as_is(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"ProdDB": "{{ProdDB}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("y\n"),
            output_stream=io.StringIO(),
        )
        assert result.accepted == 1
        assert result.edited == 0
        assert result.skipped == 0

        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        assert plan["proposals"][0]["token"] == "{{ProdDB}}"

    def test_edit_replaces_token(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"ProdDB": "{{ProdDB}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("e\n{{DB_PREFIX}}\n"),
            output_stream=io.StringIO(),
        )
        assert result.edited == 1
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        assert plan["proposals"][0]["token"] == "{{DB_PREFIX}}"

    def test_edit_rejects_non_token_input_and_reprompts(self, tmp_path):
        """A replacement without ``{{...}}`` must be rejected — this
        stops an operator error from silently de-tokenising the plan."""
        project = _setup_project(tmp_path)
        _seed_plan(project, {"ProdDB": "{{ProdDB}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("e\nSTAGING\n{{PROD}}\n"),
            output_stream=io.StringIO(),
        )
        assert result.edited == 1
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        assert plan["proposals"][0]["token"] == "{{PROD}}"

    def test_skip_removes_proposal(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"ProdDB": "{{ProdDB}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("s\n"),
            output_stream=io.StringIO(),
        )
        assert result.skipped == 1
        # Plan is now empty and removed.
        assert not _plan_path(project).exists()

    def test_skip_all_adds_literal_to_exceptions(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"LegacyDB": "{{LegacyDB}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("S\n"),
            output_stream=io.StringIO(),
        )
        assert result.skipped == 1
        assert "LegacyDB" in result.skipped_all

        exceptions = json.loads(_exceptions_path(project).read_text(encoding="utf-8"))
        assert "LegacyDB" in exceptions["exclude"]

    def test_quit_stops_processing_but_keeps_remaining_proposals(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"First": "{{First}}", "Second": "{{Second}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("q\n"),
            output_stream=io.StringIO(),
        )
        assert result.quit_early is True

        # Both proposals remain in the plan — nothing was decided.
        plan = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        literals = {p["literal"] for p in plan["proposals"]}
        assert literals == {"First", "Second"}

    def test_unrecognised_choice_reprompts(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"ProdDB": "{{ProdDB}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("z\ny\n"),
            output_stream=io.StringIO(),
        )
        assert result.accepted == 1

    def test_no_plan_file_prints_message_and_returns_empty(self, tmp_path):
        project = _setup_project(tmp_path)
        out = io.StringIO()
        result = interactive_review(
            str(project),
            input_stream=_drive(""),
            output_stream=out,
        )
        assert result.accepted == 0
        assert "no plan file" in out.getvalue()

    def test_review_result_to_dict_matches_counters(self, tmp_path):
        project = _setup_project(tmp_path)
        _seed_plan(project, {"A": "{{A}}", "B": "{{B}}"})

        result = interactive_review(
            str(project),
            input_stream=_drive("y\ns\n"),
            output_stream=io.StringIO(),
        )
        summary = result.to_dict()
        assert summary["accepted"] == 1
        assert summary["skipped"] == 1
        assert summary["edited"] == 0
        assert summary["quit_early"] is False
