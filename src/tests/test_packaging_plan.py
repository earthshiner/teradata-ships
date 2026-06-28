"""
test_packaging_plan.py — detect-and-recommend packaging plan (#379).

Covers:
    - plan_detect: tokenised / atomic detection from a source tree
    - packaging_plan: command sequencing for quick + detailed modes,
      scaffold gating, env fan-out, follow-ups, plan.json shape
"""

from pathlib import Path

from td_release_packager.packaging_plan import (
    build_plan,
    build_plan_json,
    parse_envs,
)
from td_release_packager.plan_detect import detect_answers, merge_answers


# ---------------------------------------------------------------
# Detection
# ---------------------------------------------------------------


def _seed_source(tmp_path: Path, *, tokenised: bool, compound: bool) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    db = "{{DB_PREFIX}}_STD" if tokenised else "OMR_STD"
    (src / "table1.sql").write_text(
        f"CREATE MULTISET TABLE {db}.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    if compound:
        (src / "two.sql").write_text(
            f"CREATE MULTISET TABLE {db}.A (Id INTEGER);\n"
            f"CREATE MULTISET TABLE {db}.B (Id INTEGER);\n",
            encoding="utf-8",
        )
    return src


class TestDetect:
    def test_detects_tokenised(self, tmp_path):
        src = _seed_source(tmp_path, tokenised=True, compound=False)
        det = detect_answers(str(src))
        assert det.answers["tokens.already"] == "yes"
        assert det.answers["source.type"] == "filesystem"

    def test_detects_not_tokenised(self, tmp_path):
        src = _seed_source(tmp_path, tokenised=False, compound=False)
        det = detect_answers(str(src))
        assert det.answers["tokens.already"] == "no"

    def test_detects_compound_not_atomic(self, tmp_path):
        src = _seed_source(tmp_path, tokenised=False, compound=True)
        det = detect_answers(str(src))
        assert det.answers["atomic.eponymous"] == "no"

    def test_detects_atomic(self, tmp_path):
        src = _seed_source(tmp_path, tokenised=False, compound=False)
        det = detect_answers(str(src))
        assert det.answers["atomic.eponymous"] == "yes"

    def test_missing_source(self, tmp_path):
        det = detect_answers(str(tmp_path / "nope"))
        assert det.answers == {}
        assert det.findings

    def test_merge_overrides_win(self):
        merged = merge_answers(
            {"tokens.already": "no", "package.name": "x"},
            {"package.name": "y", "envs": None},
        )
        assert merged["package.name"] == "y"
        assert merged["tokens.already"] == "no"


# ---------------------------------------------------------------
# parse_envs
# ---------------------------------------------------------------


class TestParseEnvs:
    def test_comma_and_space(self):
        assert parse_envs("dev, tst  prd") == ["DEV", "TST", "PRD"]

    def test_dedup(self):
        assert parse_envs("DEV,DEV,TST") == ["DEV", "TST"]

    def test_empty(self):
        assert parse_envs("") == []
        assert parse_envs(None) == []


# ---------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------


def _answers(**over):
    base = {
        "source.type": "filesystem",
        "source.dir": "/src/omr",
        "tokens.already": "no",
        "tokens.model": "prefix",
        "tokens.prefix": "OMR",
        "atomic.eponymous": "yes",
        "project.dir": "/proj/omr",
        "package.name": "create_objects",
        "envs": "DEV,TST",
        "mode.style": "quick",
    }
    base.update(over)
    return base


class TestQuickPlan:
    def test_scaffold_then_process_per_env(self):
        plan = build_plan(_answers())
        lines = plan.command_lines
        assert any(line.startswith("ships scaffold") for line in lines)
        process = [line for line in lines if line.startswith("ships process")]
        assert len(process) == 2  # one per env
        assert any("--env DEV" in line for line in process)
        assert any("--env TST" in line for line in process)
        assert any("--prefix-token OMR=DB_PREFIX" in line for line in process)

    def test_scaffolded_skips_scaffold(self):
        plan = build_plan(_answers(**{"project.scaffolded": True}))
        assert not any(line.startswith("ships scaffold") for line in plan.command_lines)

    def test_strict_flag(self):
        plan = build_plan(_answers(**{"process.strict": True}))
        assert any("--strict" in line for line in plan.command_lines)

    def test_scan_followup(self):
        plan = build_plan(_answers(**{"scan.enabled": "yes"}))
        assert any("ships scan" in line for line in plan.command_lines)

    def test_no_env_placeholder_note(self):
        plan = build_plan(_answers(envs=""))
        assert any("placeholder" in n.lower() for n in plan.notes)


class TestDetailedPlan:
    def test_steps_expanded(self):
        plan = build_plan(_answers(**{"mode.style": "detailed"}))
        lines = plan.command_lines
        assert any(line.startswith("ships harvest") for line in lines)
        assert any(line.startswith("ships inspect") for line in lines)
        # harvest/inspect run once; package runs per env.
        assert len([line for line in lines if line.startswith("ships harvest")]) == 1
        assert len([line for line in lines if line.startswith("ships package")]) == 2

    def test_github_detailed_warns(self):
        plan = build_plan(
            _answers(**{"mode.style": "detailed", "source.type": "github"})
        )
        assert any("source-github" in n for n in plan.notes)


class TestPlanJson:
    def test_shape(self):
        pj = build_plan_json(_answers())
        assert pj["mode"] == "quick"
        assert pj["source"] == {"type": "filesystem", "dir": "/src/omr"}
        assert pj["tokens"]["prefix"] == "OMR"
        assert pj["envs"] == ["DEV", "TST"]
        assert pj["package"]["name"] == "create_objects"

    def test_github_source(self):
        pj = build_plan_json(
            _answers(
                **{
                    "source.type": "github",
                    "source.owner_repo": "acme/omr",
                    "source.ref": "v1",
                }
            )
        )
        assert pj["source"]["type"] == "github"
        assert pj["source"]["owner_repo"] == "acme/omr"
        assert pj["source"]["ref"] == "v1"
