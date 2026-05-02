"""
orchestrator_demo.py -- Smoke / sanity demo for the orchestrator
foundation (build-order items 1-3 of the SHIPS orchestrator design).

Exercises the three foundation modules end-to-end against a fresh
temporary project:

    1. ships_yaml -- generate, write, re-load, validate, apply defaults
    2. cascade    -- resolve settings across all five layers and show
                    where each value came from
    3. decisions  -- open a decisions.json, run two fake stages with
                    issues / config provenance / outputs, then dump
                    the resulting JSON

This is not part of the production CLI -- there isn't one yet. It's
a runnable trace that proves the foundation hangs together. Safe to
delete once the orchestrator gets a real `process` verb (item 5).

Run from the repo root:

    python tools/orchestrator_demo.py

No arguments. Creates and tears down its own temp directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Make the src/ layout importable when running directly from the repo
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

from td_release_packager.orchestrator import (  # noqa: E402
    LAYER_1_DEFAULTS,
    Cascade,
    DecisionsManifest,
    LayerSource,
    apply_defaults,
    generate_default,
    load,
    validate,
    write_if_missing,
)


def _hr(title: str) -> None:
    """Print a section header."""
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _show(label: str, value) -> None:
    """Print a labelled value, indenting nested content."""
    print(f"  {label}: {value}")


def main() -> None:
    with tempfile.TemporaryDirectory() as project_dir:
        ships_path = os.path.join(project_dir, "ships.yaml")
        decisions_path = os.path.join(project_dir, "decisions.json")

        # -------------------------------------------------- 1. ships_yaml
        _hr("1. ships_yaml -- generate, write, load, validate")

        seed = generate_default(
            project_name="DemoProject",
            environments=["DEV", "TST", "PRD"],
        )
        wrote = write_if_missing(ships_path, seed)
        _show("write_if_missing (1st call)", wrote)

        wrote_again = write_if_missing(ships_path, seed)
        _show("write_if_missing (2nd call -- same file)", wrote_again)
        _show("file size on disc", os.path.getsize(ships_path))

        loaded = load(ships_path)
        _show("project name", loaded["project"])
        _show("environments", loaded["environments"])

        errs = validate(loaded)
        _show("validate() errors", errs)

        with_defaults = apply_defaults(loaded)
        _show(
            "apply_defaults -- stages.generate.strict",
            with_defaults["stages"]["generate"]["strict"],
        )

        # -------------------------------------------------- 2. cascade
        _hr("2. cascade -- five-layer resolution with provenance")

        # Pretend the user invoked SHIPS with --strict-generate and
        # the project ships.yaml hand-pinned strict on harvest only.
        project_overrides = {
            "stages": {
                "harvest": {"strict": True, "on_error": "halt"},
            },
        }
        cli_overrides = {
            "stages": {
                "generate": {"strict": True},
            },
        }

        cascade = Cascade(
            defaults=LAYER_1_DEFAULTS,
            project=project_overrides,
            cli=cli_overrides,
            source_paths={
                LayerSource.LAYER_3_PROJECT: ships_path,
                LayerSource.LAYER_5_CLI: "argv: --strict-generate",
            },
        )

        for path in (
            "stages.generate.strict",  # CLI wins
            "stages.harvest.strict",   # project wins
            "stages.harvest.on_error", # project wins
            "stages.scaffold.strict",  # default falls through
            "stages.package.on_error", # default falls through
        ):
            r = cascade.resolve(path)
            print(
                f"  {path:<32} = {str(r.value):<6} "
                f"[{r.source.value}: {r.source_path}]"
            )

        # -------------------------------------------------- 3. decisions
        _hr("3. decisions -- record a two-stage fake run")

        manifest = DecisionsManifest(
            decisions_path,
            project_meta={"name": "DemoProject", "version": "1.0"},
        )

        with manifest.run("python tools/orchestrator_demo.py") as run:
            with run.stage("scaffold") as stage:
                stage.set_status("no-op")
                stage.set_inputs(project_dir=project_dir)
                stage.set_decisions(reason="project already scaffolded")

            with run.stage("harvest") as stage:
                # Echo the resolved cascade values into the manifest
                # -- this is what real stages will do.
                strict = cascade.resolve("stages.harvest.strict")
                stage.set_config_resolved(
                    name="strict",
                    value=strict.value,
                    source=strict.source.value,
                    source_path=strict.source_path,
                )
                stage.set_inputs(files_read=3)
                stage.set_outputs(files_written=["customer.tbl", "orders.tbl"])
                stage.set_decisions(tokens_applied=2)
                stage.add_issue(
                    severity="warning",
                    code="HRV-CANDIDATE",
                    message="STAGING_DB looks like a token candidate",
                    location="customer.tbl:1",
                )
                stage.set_status("warning")

        # Dump the resulting decisions.json
        with open(decisions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print()
        print(json.dumps(data, indent=2))

        _hr("Done")
        print("  All three foundation modules exercised cleanly.")
        print(f"  (Temp project dir torn down: {project_dir})")


if __name__ == "__main__":
    main()
