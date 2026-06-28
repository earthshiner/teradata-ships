"""
wizard.py — interactive terminal front end over the decision model (issue #381).

Walks the questions in ``decision-tree.yaml`` one at a time, honouring the same
``show`` visibility and ``warn`` rules as the HTML Navigator, collects the
answers, and hands them to ``packaging_plan`` to emit the recommended command
sequence. Being a plain stdin/stdout loop, it works over SSH where the offline
HTML wizard can't reach.

The loop is pure: ``run_wizard`` takes injectable ``input_fn`` / ``output_fn``
so it is fully testable without a TTY. The CLI (`ships wizard`) is a thin wrapper
that wires real ``input`` / ``print`` and optionally seeds answers from source
detection (#379).
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Callable, Dict, Optional

from td_release_packager.decision_tree import (
    DecisionTree,
    Question,
    active_warnings,
    is_visible,
    load_decision_tree,
)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Reduce the model's HTML label/hint to plain terminal text."""
    if not text:
        return ""
    return _html.unescape(_TAG_RE.sub("", text)).strip()


def _prompt_radio(
    q: Question,
    answers: Dict[str, Any],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Optional[str]:
    for i, opt in enumerate(q.options, start=1):
        output_fn(f"    {i}. {strip_html(opt.label)}")
    default_idx = None
    if q.default is not None:
        for i, opt in enumerate(q.options, start=1):
            if opt.value == q.default:
                default_idx = i
                break
    suffix = f" [{default_idx}]" if default_idx else ""
    while True:
        raw = input_fn(f"  choose 1-{len(q.options)}{suffix}: ").strip()
        if not raw and default_idx:
            return q.options[default_idx - 1].value
        if raw.isdigit() and 1 <= int(raw) <= len(q.options):
            return q.options[int(raw) - 1].value
        output_fn("    (please enter a number from the list)")


def _prompt_text(
    q: Question,
    input_fn: Callable[[str], str],
) -> Optional[str]:
    default = q.default or ""
    suffix = f" [{default}]" if default else ""
    raw = input_fn(f"  value{suffix}: ").strip()
    if not raw:
        return default or None
    return raw


def _prompt_checkbox(q: Question, input_fn: Callable[[str], str]) -> bool:
    raw = input_fn("  [y/N]: ").strip().lower()
    return raw in ("y", "yes", "true", "1")


def run_wizard(
    tree: Optional[DecisionTree] = None,
    answers: Optional[Dict[str, Any]] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run the interactive question loop and return the collected answers.

    ``answers`` may pre-seed values (e.g. from source detection); a pre-seeded
    answer is still shown so the user can confirm or change it. Hidden questions
    (failing their ``show`` condition under the current answers) are skipped.
    """
    tree = tree or load_decision_tree()
    answers = dict(answers or {})

    for q in tree.questions:
        if not is_visible(q, answers):
            continue
        output_fn("")
        output_fn(strip_html(q.label))
        if q.hint:
            output_fn(f"  ({strip_html(q.hint)})")
        seeded = answers.get(q.id)
        if seeded not in (None, ""):
            output_fn(f"  detected: {seeded}")

        if q.kind == "radio":
            value = _prompt_radio(q, answers, input_fn, output_fn)
        elif q.kind == "checkbox":
            value = _prompt_checkbox(q, input_fn)
        else:
            value = _prompt_text(q, input_fn)

        answers[q.id] = value

        for msg in active_warnings(q, answers):
            output_fn(f"  ! {strip_html(msg)}")

    return answers
