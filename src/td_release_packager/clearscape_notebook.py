"""Clearscape Experience notebook renderer.

Emits a self-contained Jupyter notebook (.ipynb) that deploys a SHIPS
package to a Teradata Clearscape Experience instance — a free trial
sandbox used for customer-facing and internal demos.

The notebook is a *deliverable artefact*, not a SHIPS pipeline status
report (those live under :mod:`td_release_packager.reporting`). Two
properties make it a good fit for the Clearscape demo channel:

* **Inline DDL.** Every CREATE statement is embedded in a code cell;
  the notebook needs no network egress beyond the Teradata connection
  itself. Customers can read the DDL as part of the demo narrative.
* **Wave-aligned cells.** One code cell per analysed wave, preceded by
  a markdown header naming the wave and its objects. Cells are
  individually re-runnable, which mirrors how a presenter walks an
  audience through a deployment.

The renderer is intentionally **non-production**. No preflight, no
rollback, no trust report — those belong to the standard ``ships
deploy`` pipeline. This module exists to make ``ships package`` produce
a notebook customers can hand to a Clearscape sandbox and have the
data product appear at the end.

Public entry points
-------------------

* :func:`render_notebook` — build the notebook dict from an
  :class:`AnalysisResult` and a resolved env-config mapping.
* :func:`write_notebook` — serialise a notebook dict to disk as JSON.

Both are decoupled so callers can pipeline render → validate → write
without going through the filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Sequence

from td_release_packager.analyser import AnalysisResult, IndexedObject
from td_release_packager.token_engine import substitute_tokens


NBFORMAT_MAJOR = 4
NBFORMAT_MINOR = 5


def render_notebook(
    analysis: AnalysisResult,
    *,
    package_name: str,
    env_values: Dict[str, str],
    env_name: str = "DEV",
) -> dict:
    """Build a notebook dict for the Clearscape demo target.

    Args:
        analysis: Output of :func:`analyse_project`. Provides the wave
            ordering and the indexed objects (with their tokenised
            DDL text).
        package_name: Display name for the data product. Appears in
            the title cell and verification messages.
        env_values: Resolved environment config — the dict returned
            by :func:`read_env_config`. Used to substitute ``{{TOKEN}}``
            placeholders in the DDL before it lands in the notebook.
        env_name: Logical environment label (e.g. ``DEV``). Stamped
            into the title cell.

    Returns:
        A notebook dict in nbformat 4.5 shape, ready for
        :func:`write_notebook` or :func:`json.dumps`.
    """
    cells: List[dict] = []
    databases = _collect_databases(analysis, env_values)

    cells.append(_markdown_cell(_intro_markdown(package_name, env_name, analysis)))
    cells.append(_code_cell(_install_source()))
    cells.append(_code_cell(_connect_source()))

    for wave_index, wave in enumerate(analysis.waves, start=1):
        cells.append(_markdown_cell(_wave_markdown(wave_index, wave, analysis.objects)))
        cells.append(
            _code_cell(_wave_code(wave_index, wave, analysis.objects, env_values))
        )

    cells.append(_markdown_cell(_verification_markdown()))
    cells.append(_code_cell(_verification_code(databases)))

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
            "ships": {
                "target": "clearscape-notebook",
                "package_name": package_name,
                "env": env_name,
                "wave_count": len(analysis.waves),
                "object_count": len(analysis.objects),
            },
        },
        "nbformat": NBFORMAT_MAJOR,
        "nbformat_minor": NBFORMAT_MINOR,
    }


def write_notebook(notebook: dict, path: Path | str) -> Path:
    """Serialise a notebook dict to disk as UTF-8 JSON.

    Returns the written path so callers can chain on it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(notebook, indent=2), encoding="utf-8")
    return target


# ---------------------------------------------------------------
# Cell builders
# ---------------------------------------------------------------


def _markdown_cell(source: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": _split_lines(source),
    }


def _code_cell(source: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _split_lines(source),
    }


def _split_lines(source: str) -> List[str]:
    """Split source into nbformat-style line list (trailing \\n on all but last)."""
    if not source:
        return []
    lines = source.splitlines(keepends=True)
    return lines


# ---------------------------------------------------------------
# Cell content
# ---------------------------------------------------------------


def _intro_markdown(package_name: str, env_name: str, analysis: AnalysisResult) -> str:
    return (
        f"# {package_name} — Clearscape Deployment Notebook\n"
        f"\n"
        f"Generated by SHIPS for the **{env_name}** environment.\n"
        f"\n"
        f"This notebook deploys the **{package_name}** data product to a\n"
        f"Teradata Clearscape Experience instance. All DDL is inlined —\n"
        f"no network access is needed beyond the Teradata connection itself.\n"
        f"\n"
        f"**Package summary**\n"
        f"\n"
        f"- Objects: {len(analysis.objects)}\n"
        f"- Waves:   {len(analysis.waves)}\n"
        f"\n"
        f"**How to run**\n"
        f"\n"
        f"1. Run the install cell (once per sandbox).\n"
        f"2. Run the connection cell and enter your Clearscape credentials.\n"
        f"3. Run each wave cell in order. Cells are re-runnable; objects use\n"
        f"   `CREATE OR REPLACE` semantics where Teradata supports it.\n"
        f"4. Run the verification cell to confirm objects landed.\n"
    )


def _install_source() -> str:
    return (
        "# Install the Teradata SQL driver (one-off per sandbox).\n"
        "%pip install --quiet teradatasql\n"
    )


def _connect_source() -> str:
    return (
        "# Open a connection to your Clearscape instance.\n"
        "import getpass\n"
        "import teradatasql\n"
        "\n"
        'host = input("Clearscape host (e.g. xyz.clearscape.teradata.com): ")\n'
        'user = input("Username: ")\n'
        'password = getpass.getpass("Password: ")\n'
        "\n"
        "connection = teradatasql.connect(host=host, user=user, password=password)\n"
        "cursor = connection.cursor()\n"
        'print(f"Connected to {host} as {user}.")\n'
    )


def _wave_markdown(
    wave_index: int, wave: Sequence[str], objects: Dict[str, IndexedObject]
) -> str:
    lines = [f"## Wave {wave_index} — {len(wave)} object(s)\n", "\n"]
    for qualified in wave:
        obj = objects.get(qualified)
        kind = obj.object_type if obj else "?"
        lines.append(f"- `{qualified}` ({kind})\n")
    return "".join(lines)


def _wave_code(
    wave_index: int,
    wave: Sequence[str],
    objects: Dict[str, IndexedObject],
    env_values: Dict[str, str],
) -> str:
    statements = _resolve_wave_statements(wave, objects, env_values)
    body_lines = [
        f"# Wave {wave_index} — deploy {len(statements)} statement(s)\n",
        "statements = [\n",
    ]
    for label, ddl in statements:
        body_lines.append(f"    # {label}\n")
        body_lines.append(f"    {_python_string_literal(ddl)},\n")
    body_lines.extend(
        [
            "]\n",
            "for index, sql in enumerate(statements, start=1):\n",
            "    if not sql.strip():\n",
            "        continue\n",
            "    preview = sql.strip().splitlines()[0][:80]\n",
            f'    print(f"[wave {wave_index}] {{index}}/{{len(statements)}} {{preview}}")\n',
            "    cursor.execute(sql)\n",
            f'print(f"Wave {wave_index} complete: {{len(statements)}} statement(s).")\n',
        ]
    )
    return "".join(body_lines)


def _verification_markdown() -> str:
    return (
        "## Verification\n"
        "\n"
        "Confirm the objects this notebook created are visible in the\n"
        "data dictionary. A non-empty result for each database in the\n"
        "package means the deployment landed.\n"
    )


def _verification_code(databases: List[str]) -> str:
    if not databases:
        return (
            "# No databases detected in package — nothing to verify.\n"
            'print("No databases to verify.")\n'
        )
    db_list = ", ".join(f"'{db}'" for db in databases)
    return (
        "# Count objects per database created by this notebook.\n"
        "verification_sql = (\n"
        '    "SELECT DatabaseName, COUNT(*) AS object_count "\n'
        '    "FROM DBC.TablesV "\n'
        f'    "WHERE DatabaseName IN ({db_list}) "\n'
        '    "GROUP BY DatabaseName ORDER BY DatabaseName"\n'
        ")\n"
        "cursor.execute(verification_sql)\n"
        "for database_name, object_count in cursor.fetchall():\n"
        '    print(f"{database_name:40s} {object_count} object(s)")\n'
    )


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _resolve_wave_statements(
    wave: Sequence[str],
    objects: Dict[str, IndexedObject],
    env_values: Dict[str, str],
) -> List[tuple[str, str]]:
    """Return ``(label, resolved_ddl)`` pairs for one wave."""
    resolved: List[tuple[str, str]] = []
    for qualified in wave:
        obj = objects.get(qualified)
        if obj is None or not obj.ddl_text:
            continue
        try:
            ddl, _ = substitute_tokens(obj.ddl_text, env_values)
            label, _ = substitute_tokens(qualified, env_values)
        except KeyError as exc:
            # A token in the DDL is missing from env_values. Leave a
            # comment in the cell rather than aborting the whole render
            # — the customer can fix the env config and re-run.
            label = qualified
            ddl = (
                f"-- [SHIPS] Unresolved token {{{{ {exc.args[0]} }}}} in "
                f"{qualified}. Add it to the env config and re-render.\n"
                f"{obj.ddl_text}"
            )
        resolved.append((label, ddl))
    return resolved


def _collect_databases(
    analysis: AnalysisResult, env_values: Dict[str, str]
) -> List[str]:
    """Distinct database names mentioned by package objects, resolved."""
    raw: set[str] = set()
    for qualified in analysis.objects:
        if "." in qualified:
            raw.add(qualified.split(".", 1)[0])
    resolved: set[str] = set()
    for name in raw:
        try:
            value, _ = substitute_tokens(name, env_values)
        except KeyError:
            value = name
        resolved.add(value)
    return sorted(resolved)


def _python_string_literal(text: str) -> str:
    """Render *text* as a Python triple-quoted string literal.

    Prefers ``'''`` for readability of inlined SQL; falls back to
    :func:`repr` on the rare DDL containing the triple-single-quote
    sequence.
    """
    if "'''" in text:
        return repr(text)
    # Strip a trailing newline so the closing quotes sit cleanly.
    return f"'''\n{text.rstrip()}\n'''"
