"""
test_wizard.py — interactive CLI wizard over the decision model (#381).

Covers:
    - strip_html
    - run_wizard with scripted input: radio by number, text + default,
      checkbox, visibility skipping, warning surfacing, detection seeding
    - the collected answers drive packaging_plan to a coherent plan
"""

from td_release_packager.decision_tree import (
    DecisionTree,
    Option,
    Question,
    Warning_,
)
from td_release_packager.packaging_plan import build_plan
from td_release_packager.wizard import run_wizard, strip_html


class _Input:
    """Scripted stdin: returns queued lines in order."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __call__(self, _prompt=""):
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def _collect_output():
    buf = []
    return buf, lambda s="": buf.append(s)


def _small_tree():
    return DecisionTree(
        schema_version="1.0",
        questions=[
            Question(
                id="mode.style",
                label="Mode?",
                kind="radio",
                options=[Option("quick", "Quick"), Option("detailed", "Detailed")],
            ),
            Question(
                id="source.type",
                label="Source?",
                kind="radio",
                options=[Option("github", "GitHub"), Option("filesystem", "FS")],
            ),
            Question(
                id="source.dir",
                label="Dir",
                kind="text",
                default="/d",
                show={"eq": {"field": "source.type", "value": "filesystem"}},
            ),
            Question(
                id="atomic.eponymous",
                label="Atomic?",
                kind="radio",
                options=[Option("yes", "Yes"), Option("no", "No")],
                warn=[Warning_(when_value_in=["no"], message="will auto-split")],
            ),
            Question(id="process.strict", label="Strict?", kind="checkbox"),
        ],
    )


class TestStripHtml:
    def test_tags_and_entities(self):
        assert strip_html("<b>A</b> &amp; <code>B</code>") == "A & B"

    def test_empty(self):
        assert strip_html("") == ""


class TestRunWizard:
    def test_radio_text_checkbox_flow(self):
        # mode=1(quick), source=2(filesystem), dir=blank->default, atomic=1(yes), strict=y
        inp = _Input(["1", "2", "", "1", "y"])
        _buf, out = _collect_output()
        answers = run_wizard(tree=_small_tree(), input_fn=inp, output_fn=out)
        assert answers["mode.style"] == "quick"
        assert answers["source.type"] == "filesystem"
        assert answers["source.dir"] == "/d"  # default applied
        assert answers["atomic.eponymous"] == "yes"
        assert answers["process.strict"] is True

    def test_hidden_question_skipped(self):
        # source=1(github) hides source.dir; so only 4 prompts consumed.
        inp = _Input(["1", "1", "2", "n"])  # mode, source=github, atomic=no, strict=n
        _buf, out = _collect_output()
        answers = run_wizard(tree=_small_tree(), input_fn=inp, output_fn=out)
        assert "source.dir" not in answers
        assert answers["source.type"] == "github"

    def test_warning_surfaced(self):
        inp = _Input(["1", "2", "/x", "2", "n"])  # atomic=2(no) triggers warn
        buf, out = _collect_output()
        run_wizard(tree=_small_tree(), input_fn=inp, output_fn=out)
        assert any("auto-split" in line for line in buf)

    def test_invalid_then_valid_choice(self):
        inp = _Input(["9", "x", "1", "2", "/x", "1", "n"])
        buf, out = _collect_output()
        answers = run_wizard(tree=_small_tree(), input_fn=inp, output_fn=out)
        assert answers["mode.style"] == "quick"
        assert any("number from the list" in line for line in buf)

    def test_seeded_answer_shown_and_overridable(self):
        # Seed source.type=filesystem; user still answers each visible question.
        inp = _Input(["1", "2", "/x", "1", "n"])
        buf, out = _collect_output()
        answers = run_wizard(
            tree=_small_tree(),
            answers={"source.type": "filesystem"},
            input_fn=inp,
            output_fn=out,
        )
        assert any("detected: filesystem" in line for line in buf)
        assert answers["source.dir"] == "/x"

    def test_answers_drive_plan(self):
        inp = _Input(["1", "2", "/src", "1", "n"])
        _buf, out = _collect_output()
        answers = run_wizard(tree=_small_tree(), input_fn=inp, output_fn=out)
        answers["project.dir"] = "/proj"
        answers["envs"] = "DEV"
        plan = build_plan(answers)
        assert any(line.startswith("ships process") for line in plan.command_lines)


class TestBundledTree:
    def test_runs_against_real_model_quick_path(self):
        # Answer every question with the first option / blank to exercise the
        # real decision-tree.yaml end-to-end without raising.
        # Generous queue of "1"/"" — extra entries are simply unused.
        inp = _Input(["1"] * 40)
        _buf, out = _collect_output()
        answers = run_wizard(input_fn=inp, output_fn=out)
        assert "mode.style" in answers
        plan = build_plan(answers)
        assert plan.command_lines
