"""
packaging_plan.py — turn packaging answers into an ordered command plan
(issues #379 / #381).

Given a set of answers keyed by the ``decision-tree.yaml`` question ids, build
the recommended ``ships`` command sequence, a per-step rationale, and a
machine-readable ``plan.json`` — the same artefacts the HTML Navigator emits,
so the CLI ``ships plan`` (#379) and ``ships wizard`` (#381) produce identical
recommendations from identical answers.

This is the Python port of the wizard's ``renderScript`` / ``buildPlanJson``.
It emits concrete commands (real project / env / package values substituted)
rather than the shell-variable form the HTML uses, because the CLI consumer
runs them directly. Config-skeleton *files* are authored by the ``scaffold`` and
``bootstrap-env-config`` commands the plan recommends — SHIPS already authors
those correctly, so the plan points at them rather than duplicating that logic.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Plan:
    """A recommended packaging plan."""

    commands: List[List[str]] = field(default_factory=list)  # each: full argv
    rationale: List[Dict[str, str]] = field(default_factory=list)
    plan_json: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def command_lines(self) -> List[str]:
        """Render each command as a single shell-ready line."""
        return [" ".join(_quote(a) for a in argv) for argv in self.commands]


def _quote(arg: str) -> str:
    """Quote an argv token only when it contains shell-significant chars."""
    if arg and re.fullmatch(r"[A-Za-z0-9_./:=,@-]+", arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def format_plan(plan: "Plan") -> str:
    """Render a plan as a human-readable block (shared by `plan` + `wizard`)."""
    lines: List[str] = []
    if plan.notes:
        lines.append("Notes:")
        lines += [f"  ! {n}" for n in plan.notes]
        lines.append("")
    lines.append("Recommended commands:")
    lines += [f"  {line}" for line in plan.command_lines]
    lines.append("")
    lines.append("Why each step:")
    lines += [f"  [{r['step']}] {r['why']}" for r in plan.rationale]
    return "\n".join(lines)


def parse_envs(raw: Optional[str]) -> List[str]:
    """Split a comma/space-separated environment list, upper-cased + de-duped."""
    if not raw:
        return []
    parts = re.split(r"[,\s]+", raw.strip())
    out: List[str] = []
    for p in parts:
        token = p.strip().upper()
        if token and token not in out:
            out.append(token)
    return out


def _val(answers: Dict[str, Any], key: str, default: Any = None) -> Any:
    v = answers.get(key)
    return v if v not in (None, "") else default


def build_plan_json(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Port of the wizard's ``buildPlanJson`` — the canonical answers snapshot."""
    source_type = _val(answers, "source.type", "filesystem")
    if source_type == "github":
        source = {
            "type": "github",
            "owner_repo": _val(answers, "source.owner_repo"),
            "ref": _val(answers, "source.ref", "main"),
        }
    else:
        source = {"type": "filesystem", "dir": _val(answers, "source.dir")}

    return {
        "mode": _val(answers, "mode.style", "quick"),
        "source": source,
        "tokens": {
            "already": _val(answers, "tokens.already"),
            "model": _val(answers, "tokens.model"),
            "prefix": _val(answers, "tokens.prefix"),
        },
        "atomic": {"eponymous": _val(answers, "atomic.eponymous")},
        "generate": {"enabled": _val(answers, "generate.enabled", "unsure")},
        "analyse": {
            "enabled": _val(answers, "analyse.enabled", "yes"),
            "graph": _val(answers, "analyse.graph", "no"),
            "namespace": _val(
                answers, "analyse.namespace", "teradata://ships-analysis"
            ),
            "projectName": _val(answers, "analyse.projectName", "ships-project"),
        },
        "scan": {"enabled": _val(answers, "scan.enabled", "no")},
        "envs": parse_envs(answers.get("envs")),
        "externalParents": parse_envs(answers.get("env.externalParents")),
        "project": {
            "dir": _val(answers, "project.dir"),
            "scaffolded": bool(answers.get("project.scaffolded")),
        },
        "package": {"name": _val(answers, "package.name", "create_objects")},
        "strict": bool(answers.get("process.strict")),
    }


def build_plan(answers: Dict[str, Any]) -> Plan:
    """Build the recommended command plan from ``answers``.

    Mirrors the Navigator's ``renderScript`` step ordering: optional scaffold →
    per-env (bootstrap → quick ``process`` OR detailed harvest/generate/inspect/
    scan/analyse/package) → quick-mode follow-ups (scan / graph export).
    """
    pj = build_plan_json(answers)

    mode = pj["mode"]
    source_type = pj["source"]["type"]
    tokens_already = pj["tokens"]["already"] == "yes"
    token_model = pj["tokens"]["model"]
    prefix = pj["tokens"]["prefix"] or "<PREFIX>"
    want_generate = pj["generate"]["enabled"] != "no"
    want_analyse = pj["analyse"]["enabled"] != "no"
    want_graph = want_analyse and pj["analyse"]["graph"] == "yes"
    want_scan = pj["scan"]["enabled"] == "yes"
    want_strict = pj["strict"]
    needs_scaffold = not pj["project"]["scaffolded"]

    project = pj["project"]["dir"] or "<PROJECT>"
    name = os.path.basename(project.rstrip("/\\")) or "<NAME>"
    output_root = os.path.dirname(project.rstrip("/\\")) or "<OUTPUT_ROOT>"
    pkg_name = pj["package"]["name"]
    # Emit forward-slash paths so a copied command line never mixes separators;
    # both Windows and POSIX shells accept '/'.
    base = project.rstrip("/\\")
    releases = f"{base}/releases"
    graphs_dir = f"{base}/graphs"
    source_dir = pj["source"].get("dir") or "<SOURCE>"
    owner_repo = pj["source"].get("owner_repo") or "<OWNER/REPO>"
    ref = pj["source"].get("ref") or "main"
    namespace = pj["analyse"]["namespace"]
    project_name = pj["analyse"]["projectName"]

    envs = pj["envs"] or ["<ENV>"]

    commands: List[List[str]] = []
    rationale: List[Dict[str, str]] = []
    notes: List[str] = []

    if not pj["envs"]:
        notes.append(
            "No target environments given — using a <ENV> placeholder. Pass "
            "--env DEV,TST to fill it in."
        )
    if mode == "detailed" and source_type == "github":
        notes.append(
            "Detailed mode emits `ships harvest`, which does NOT accept "
            "--source-github. Switch to quick mode, or git-clone the repo first "
            "and use a filesystem source."
        )

    # -- prelude: scaffold --
    if needs_scaffold:
        sc = ["ships", "scaffold", "--name", name, "--output", output_root]
        if pj["envs"]:
            sc += ["--environments", ",".join(envs)]
        commands.append(sc)
        rationale.append(
            {
                "step": "scaffold",
                "why": "Creates the SHIPS project layout (config/, payload/, db/) "
                "with starter env files and a default inspect.conf.",
            }
        )

    # -- per-environment --
    for idx, env in enumerate(envs):
        env_config = f"{base}/config/env/{env}.conf"

        if tokens_already:
            commands.append(
                ["ships", "bootstrap-env-config", "--source", project, "--env", env]
            )

        if mode == "quick":
            pr = ["ships", "process", "--project", project]
            if source_type == "github":
                pr += ["--source-github", owner_repo, "--source-ref", ref]
            else:
                pr += ["--source", source_dir]
            if not tokens_already and token_model == "prefix":
                pr += ["--prefix-token", f"{prefix}=DB_PREFIX"]
            if not want_generate:
                pr += ["--skip-generate"]
            pr += ["--env", env, "--env-config", env_config, "--name", pkg_name]
            pr += ["--output", releases]
            if want_strict:
                pr += ["--strict"]
            commands.append(pr)
        else:
            # Detailed: env-agnostic steps run once (first env); package per env.
            if idx == 0:
                h = ["ships", "harvest", "--source", source_dir, "--project", project]
                if not tokens_already and token_model == "prefix":
                    h += ["--prefix-token", f"{prefix}=DB_PREFIX"]
                commands.append(h)
                if want_generate:
                    commands.append(["ships", "generate", "--project", project])
                commands.append(["ships", "inspect", "--project", project])
                if want_scan:
                    commands.append(
                        [
                            "ships",
                            "scan",
                            "--project",
                            project,
                            "--all-envs",
                            "--fail-on-orphan",
                        ]
                    )
                if want_analyse:
                    az = ["ships", "analyse", "--project", project]
                    if want_graph:
                        az += [
                            "--graph",
                            graphs_dir,
                            "--namespace",
                            namespace,
                            "--project-name",
                            project_name,
                        ]
                    commands.append(az)
            commands.append(
                [
                    "ships",
                    "package",
                    "--project",
                    project,
                    "--env",
                    env,
                    "--name",
                    pkg_name,
                    "--env-config",
                    env_config,
                    "--output",
                    releases,
                ]
            )

    # -- quick-mode follow-ups (sit outside `process`) --
    if mode == "quick":
        if want_scan:
            commands.append(
                [
                    "ships",
                    "scan",
                    "--project",
                    project,
                    "--all-envs",
                    "--fail-on-orphan",
                ]
            )
        if want_graph:
            commands.append(
                [
                    "ships",
                    "analyse",
                    "--project",
                    project,
                    "--graph",
                    graphs_dir,
                    "--namespace",
                    namespace,
                    "--project-name",
                    project_name,
                ]
            )

    # -- rationale (mirrors the script order) --
    if tokens_already:
        rationale.append(
            {
                "step": "bootstrap-env-config",
                "why": "Source already uses {{TOKEN}} placeholders. Seeds a per-env "
                ".conf from the tokens actually referenced in the payload — safer "
                "than guessing the token list.",
            }
        )
    if mode == "quick":
        gen = "generate -> " if want_generate else ""
        ana = "analyse -> " if want_analyse else ""
        strict = (
            "Strict mode aborts on the first failing stage."
            if want_strict
            else "Developer mode continues past warnings and summarises at the end."
        )
        rationale.append(
            {
                "step": "process",
                "why": f"Runs harvest -> {gen}inspect -> {ana}package as one "
                f"orchestrated call per environment, recording every stage "
                f"decision in ships.decisions.json. {strict}",
            }
        )
        if want_scan or want_graph:
            rationale.append(
                {
                    "step": "follow-ups",
                    "why": "Orphan-token scanning and graph export sit outside "
                    "`process` — run them on demand after the pipeline succeeds.",
                }
            )
    else:
        rationale.append(
            {
                "step": "harvest",
                "why": _harvest_why(tokens_already, token_model, prefix),
            }
        )
        if want_generate:
            rationale.append(
                {
                    "step": "generate",
                    "why": "Builds view-layer DDL from the harvested base tables, "
                    "following the object-placement standard.",
                }
            )
        rationale.append(
            {
                "step": "inspect",
                "why": "Runs Step 0 lint + Step 0b token-coverage. Auto-loads "
                "config/inspect.conf. Never pass an env file behind --config.",
            }
        )
        if want_scan:
            rationale.append(
                {
                    "step": "scan",
                    "why": "Validates every {{TOKEN}} against every env .conf and "
                    "fails on an orphan (defined but never referenced).",
                }
            )
        if want_analyse:
            rationale.append(
                {
                    "step": "analyse",
                    "why": (
                        "Computes deploy waves AND exports dependency graph files "
                        "(incl. OpenLineage JSON)."
                        if want_graph
                        else "Computes deploy waves from cross-object dependencies "
                        "and emits _waves.txt so DDL deploys in the right order."
                    ),
                }
            )
        rationale.append(
            {
                "step": "package",
                "why": "Builds the deployable release per environment, resolving "
                "every {{TOKEN}} from that env's .conf file.",
            }
        )

    return Plan(commands=commands, rationale=rationale, plan_json=pj, notes=notes)


def _harvest_why(tokens_already: bool, token_model: Optional[str], prefix: str) -> str:
    if tokens_already:
        return "Source is already tokenised; harvest copies into payload/ as-is."
    if token_model == "prefix":
        return (
            f"Applies the prefix-token rule: '{prefix}_' becomes '{{{{DB_PREFIX}}}}_' "
            "in both file contents and filenames (eponymy preserved)."
        )
    if token_model == "per_database":
        return (
            "Reads config/tokenise.conf and produces per-database tokens like "
            "{{DOM_STD_T}}, applied to content and filenames."
        )
    return "Cleans payload/ and copies the source in. Pick a tokenisation mode first."
