"""
decision_tree.py — loader for the declarative packaging elicitation model
(issue #378).

``tools/navigator/decision-tree.yaml`` is the single source of truth for the
guided-packaging question set, shared by the HTML wizard, the CLI wizard, and
the AI skill. This module is the Python side: it loads the model, validates its
shape, and evaluates the visibility / warning condition DSL so every consumer
behaves identically.

The condition DSL is DATA, never code (see the YAML header for the grammar).
``evaluate_condition`` is the reference implementation; the HTML wizard mirrors
it in JS. Keeping one grammar with one evaluator per language is the whole point
of the issue — no front end gets to invent its own visibility logic.

Design guarantees:
    - **No code execution.** Conditions are dict literals matched structurally.
    - **Fail closed.** A malformed model (bad YAML, unknown ``kind``, duplicate
      id, unknown condition operator, dangling ``field`` reference) raises
      ``DecisionTreeError`` so a consumer aborts rather than silently dropping a
      question.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

#: Path to the authoritative model, relative to the repository root.
DECISION_TREE_RELPATH = os.path.join("tools", "navigator", "decision-tree.yaml")

#: Question input kinds the model may declare.
VALID_KINDS = {"radio", "text", "checkbox"}

#: Condition operators recognised by ``evaluate_condition``.
_LEAF_OPS = {"eq", "ne", "truthy", "derived"}
_COMBINATOR_OPS = {"all", "any"}


class DecisionTreeError(Exception):
    """Raised when the decision model is malformed or a condition is invalid."""


@dataclass
class Option:
    """A single choice for a ``radio`` question."""

    value: str
    label: str


@dataclass
class Warning_:
    """A heads-up message shown when the answer matches ``when_value_in``."""

    when_value_in: List[str]
    message: str


@dataclass
class Question:
    """One node in the elicitation tree."""

    id: str
    label: str
    kind: str
    hint: str = ""
    default: Optional[str] = None
    options: List[Option] = field(default_factory=list)
    show: Optional[Dict[str, Any]] = None
    warn: List[Warning_] = field(default_factory=list)


@dataclass
class DecisionTree:
    """The full model: an ordered list of questions plus a version stamp."""

    schema_version: str
    questions: List[Question] = field(default_factory=list)

    def by_id(self, qid: str) -> Optional[Question]:
        for q in self.questions:
            if q.id == qid:
                return q
        return None

    @property
    def ids(self) -> List[str]:
        return [q.id for q in self.questions]


# ---------------------------------------------------------------
# Derived values (computed, not answered)
# ---------------------------------------------------------------


def _derive_target_os(answers: Dict[str, Any]) -> str:
    """Mirror the wizard's ``targetOs()``: Windows-shaped project dir → windows.

    A drive letter (``C:``) or a backslash in ``project.dir`` implies a Windows
    path; otherwise POSIX. Empty / unset is treated as POSIX.
    """
    path = str(answers.get("project.dir") or "")
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return "windows"
    if "\\" in path:
        return "windows"
    return "posix"


_DERIVERS = {"target_os": _derive_target_os}


def derived_value(name: str, answers: Dict[str, Any]) -> str:
    """Resolve a derived value by name. Raises on an unknown deriver."""
    fn = _DERIVERS.get(name)
    if fn is None:
        raise DecisionTreeError(f"Unknown derived value: {name!r}")
    return fn(answers)


# ---------------------------------------------------------------
# Condition evaluation (the shared visibility DSL)
# ---------------------------------------------------------------


def evaluate_condition(cond: Optional[Dict[str, Any]], answers: Dict[str, Any]) -> bool:
    """Evaluate a condition node against the current ``answers``.

    A ``None`` condition means "always show". See the YAML header for the
    grammar. Raises ``DecisionTreeError`` on an unknown operator or malformed
    node so model bugs surface loudly rather than silently hiding a question.
    """
    if cond is None:
        return True
    if not isinstance(cond, dict) or len(cond) != 1:
        raise DecisionTreeError(f"Condition must be a single-key mapping: {cond!r}")

    op, body = next(iter(cond.items()))

    if op == "all":
        return all(evaluate_condition(c, answers) for c in body)
    if op == "any":
        return any(evaluate_condition(c, answers) for c in body)
    if op == "truthy":
        return bool(answers.get(body))
    if op == "eq":
        return _answer(answers, body["field"]) == body["value"]
    if op == "ne":
        return _answer(answers, body["field"]) != body["value"]
    if op == "derived":
        actual = derived_value(body["name"], answers)
        compare = body.get("op", "eq")
        if compare == "eq":
            return actual == body["value"]
        if compare == "ne":
            return actual != body["value"]
        raise DecisionTreeError(f"Unknown derived op: {compare!r}")

    raise DecisionTreeError(f"Unknown condition operator: {op!r}")


def _answer(answers: Dict[str, Any], field_id: str) -> Any:
    """Return the answer for ``field_id`` (None when unanswered)."""
    return answers.get(field_id)


def is_visible(question: Question, answers: Dict[str, Any]) -> bool:
    """True when ``question`` should be shown given the current answers."""
    return evaluate_condition(question.show, answers)


def active_warnings(question: Question, answers: Dict[str, Any]) -> List[str]:
    """Return the warning messages triggered by the current answer."""
    value = answers.get(question.id)
    out: List[str] = []
    for w in question.warn:
        if value in w.when_value_in:
            out.append(w.message)
    return out


# ---------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------


def _validate_condition_fields(cond: Optional[Dict[str, Any]], ids: set) -> None:
    """Recursively check every ``field`` reference points at a known question."""
    if cond is None:
        return
    if not isinstance(cond, dict) or len(cond) != 1:
        raise DecisionTreeError(f"Condition must be a single-key mapping: {cond!r}")
    op, body = next(iter(cond.items()))
    if op in _COMBINATOR_OPS:
        for c in body:
            _validate_condition_fields(c, ids)
        return
    if op == "truthy":
        if body not in ids:
            raise DecisionTreeError(f"Condition references unknown field: {body!r}")
        return
    if op in ("eq", "ne"):
        if body.get("field") not in ids:
            raise DecisionTreeError(
                f"Condition references unknown field: {body.get('field')!r}"
            )
        return
    if op == "derived":
        if body.get("name") not in _DERIVERS:
            raise DecisionTreeError(f"Unknown derived value: {body.get('name')!r}")
        return
    raise DecisionTreeError(f"Unknown condition operator: {op!r}")


def _parse_question(raw: Dict[str, Any]) -> Question:
    qid = raw.get("id")
    if not qid or not isinstance(qid, str):
        raise DecisionTreeError(f"Question missing a string id: {raw!r}")
    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        raise DecisionTreeError(f"Question {qid!r} has invalid kind: {kind!r}")
    label = raw.get("label")
    if not label or not isinstance(label, str):
        raise DecisionTreeError(f"Question {qid!r} missing a string label")

    options = [
        Option(value=str(o["value"]), label=str(o["label"]))
        for o in raw.get("options", [])
    ]
    if kind == "radio" and not options:
        raise DecisionTreeError(f"Radio question {qid!r} declares no options")

    warns = [
        Warning_(
            when_value_in=[str(v) for v in w["when_value_in"]],
            message=str(w["message"]),
        )
        for w in raw.get("warn", [])
    ]

    return Question(
        id=qid,
        label=label,
        kind=kind,
        hint=str(raw.get("hint", "")),
        default=raw.get("default"),
        options=options,
        show=raw.get("show"),
        warn=warns,
    )


def parse_decision_tree(data: Dict[str, Any]) -> DecisionTree:
    """Validate a parsed YAML mapping into a ``DecisionTree``."""
    if not isinstance(data, dict):
        raise DecisionTreeError("Decision tree root must be a mapping")
    raw_questions = data.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise DecisionTreeError("Decision tree has no questions")

    questions = [_parse_question(q) for q in raw_questions]

    ids = set()
    for q in questions:
        if q.id in ids:
            raise DecisionTreeError(f"Duplicate question id: {q.id!r}")
        ids.add(q.id)

    # Every condition's field references must resolve to a known question.
    for q in questions:
        _validate_condition_fields(q.show, ids)

    return DecisionTree(
        schema_version=str(data.get("schema_version", "1.0")),
        questions=questions,
    )


def _default_path() -> str:
    """Resolve the bundled decision-tree.yaml relative to the repo root.

    The module lives at ``src/td_release_packager/decision_tree.py``; the model
    lives at ``tools/navigator/decision-tree.yaml`` two levels up from ``src``.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    return os.path.join(repo_root, DECISION_TREE_RELPATH)


def load_decision_tree(path: Optional[str] = None) -> DecisionTree:
    """Load and validate the decision model from ``path`` (or the bundled one)."""
    resolved = path or _default_path()
    if not os.path.isfile(resolved):
        raise DecisionTreeError(f"Decision tree not found: {resolved}")
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise DecisionTreeError(f"Could not read decision tree: {exc}") from exc
    return parse_decision_tree(data)
