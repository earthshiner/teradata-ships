# Clearscape Experience Notebook Target

A SHIPS render target that turns any project into a self-contained
Jupyter notebook (`.ipynb`) for deployment to a Teradata **Clearscape
Experience** sandbox.

Clearscape Experience is Teradata's free-trial environment. Customers
and internal associates spin up a small Teradata instance, open
Jupyter, run a notebook, and see a working data product without
installing anything. This target produces the notebook you hand them.

---

## When to use it

| Use it for | Don't use it for |
|---|---|
| Customer-facing demos on a Clearscape sandbox | Production deployment |
| Internal "show me the data product" walkthroughs | Long-running, repeatable CI/CD |
| Workshops where attendees deploy step-by-step | Any deployment that needs preflight, rollback, or a trust report |
| Hand-off where the recipient cannot install SHIPS | Anything where the audience reads the build report, not the DDL |

The renderer is **non-production by design**. It does no preflight,
no rollback, no integrity fingerprinting. Those belong to
`ships package` + `ships deploy`. If the deployment matters, use
those instead.

---

## Quick start

Render a notebook from the bundled CallCentre example:

```bash
python -m td_release_packager notebook \
    --project examples/callcentre \
    --env-config examples/callcentre/config/env/DEV.conf \
    --name CallCentre
```

Output (default): `examples/callcentre/output/CallCentre.clearscape.ipynb`

Hand that file to a Clearscape user. They upload it to their Jupyter,
run the cells in order, and the data product appears in their
sandbox.

---

## CLI reference

```
python -m td_release_packager notebook \
    --project       PATH        # SHIPS project directory (required)
    --env-config    PATH        # Env config file used to resolve tokens (required)
    [--name         NAME]       # Package display name (default: project basename)
    [--output       PATH]       # Output .ipynb path
                                # (default: <project>/output/<name>.clearscape.ipynb)
    [--env-name     LABEL]      # Logical env label stamped in the intro cell (default: DEV)
```

### What each flag controls

- **`--project`** — the SHIPS project to render. Must contain a
  `payload/` tree the analyser can walk; a scaffolded project (output
  of `ships scaffold`, `ships harvest`, or `ships demo`) is the normal
  input.
- **`--env-config`** — the environment config file (typically
  `config/env/DEV.conf`). All `{{TOKEN}}` placeholders in the DDL are
  resolved against this file before they land in the notebook, so
  the customer sees real database names, not tokens.
- **`--name`** — the display name printed in the title cell and used
  to derive the default output filename.
- **`--output`** — override the output path. Useful when generating
  notebooks for several environments side-by-side.
- **`--env-name`** — the label printed in the intro cell ("Generated
  by SHIPS for the **DEV** environment"). Does not affect token
  resolution — that is driven by `--env-config`.

---

## Anatomy of the produced notebook

A render of a project with N analysed waves produces `3 + 2N + 2`
cells:

| # | Cell | Purpose |
|---|---|---|
| 1 | Markdown | Intro: package name, object/wave counts, how-to-run |
| 2 | Code | `%pip install --quiet teradatasql` |
| 3 | Code | Connection cell — `getpass` prompts for host / user / password |
| 4 | Markdown | Wave 1 header + bullet list of objects (collapsed in `<details>` when > 12) |
| 5 | Code | Wave 1 — inline DDL list + execution loop via `cursor.execute` |
| 6 | Markdown | Wave 2 header |
| 7 | Code | Wave 2 — inline DDL |
| … | … | … |
| -2 | Markdown | Verification header |
| -1 | Code | `SELECT DatabaseName, COUNT(*) FROM DBC.TablesV …` smoke test |

Each code cell is independently re-runnable. Statements use
`CREATE OR REPLACE` semantics where Teradata supports them, so
re-running a wave is safe.

### Why one cell per wave

Waves are SHIPS' analysed parallel-deployable groups. Mapping
wave → cell gives the audience three useful things at once:

1. **A clear story** — each cell is a chapter the presenter can talk
   over.
2. **Granular re-run** — if one cell fails, you fix and re-run just
   that cell, not the whole notebook.
3. **A consistent unit of progress** — the wave structure mirrors
   what `ships deploy` would do in production, so what the customer
   sees in the demo matches what they would see in a real deploy.

The alternative (one cell per object) would explode a typical data
product into 200+ cells. The alternative (one cell per logical
module) would lose the wave-level idempotency.

---

## How DDL appears in cells

Every wave cell is a list of triple-quoted Python strings followed by
a small execution loop. For a wave containing one table:

```python
# Wave 1 — deploy 1 statement(s)
statements = [
    # CallCentre.Customer_T
    '''
CREATE TABLE CallCentre.Customer_T (
    customer_id INTEGER NOT NULL,
    customer_name VARCHAR(100)
) PRIMARY INDEX(customer_id);
    ''',
]
for index, sql in enumerate(statements, start=1):
    if not sql.strip():
        continue
    preview = sql.strip().splitlines()[0][:80]
    print(f"[wave 1] {index}/{len(statements)} {preview}")
    cursor.execute(sql)
print(f"Wave 1 complete: {len(statements)} statement(s).")
```

The cell is plain Python — no SHIPS dependency in the executing
environment beyond `teradatasql`.

---

## Label conventions in markdown bullets

The analyser uses two synthetic identifiers internally that the
renderer translates into customer-friendly labels:

| Analyser identifier | Rendered as |
|---|---|
| `$DATABASE.X` (parentless CREATE DATABASE) | `Database: X` |
| `$FILE:path/foo.dcl` (GRANTs without a parsed object) | `GRANTs from foo.dcl` |
| `{{TOKEN}}` (any unresolved token) | Resolved value from `--env-config` |

Long waves are wrapped in a `<details><summary>Show all N objects
</summary>…</details>` block. Jupyter renders this natively, keeping
the cell scannable while still letting the reader expand the full
list.

---

## Running the produced notebook in Clearscape

1. Sign in to your Clearscape Experience instance.
2. Open Jupyter (usually via the **Launch Notebook** button on the
   instance page).
3. Upload the `.ipynb`.
4. Run the install cell. `%pip install` succeeds even when the
   sandbox already has the driver — no harm in re-running.
5. Run the connection cell. Enter the host / username / password for
   your Clearscape instance when prompted. The password prompt uses
   `getpass` so it does not appear in the cell output.
6. Run each wave cell in order. Watch the per-statement progress
   line in the cell output.
7. Run the verification cell. A row per database with a non-zero
   `object_count` means the deployment landed.

---

## Customising the notebook

The renderer is a single function — see
[clearscape_notebook.py](../src/td_release_packager/clearscape_notebook.py).
If you need to change cell content (different driver, different
verification query, branded markdown, etc.) the public entry point is:

```python
from td_release_packager.analyser import analyse_project
from td_release_packager.clearscape_notebook import render_notebook, write_notebook
from td_release_packager.token_engine import read_env_config

analysis = analyse_project("my-project")
env_values = read_env_config("my-project/config/env/DEV.conf")
notebook = render_notebook(analysis, package_name="MyDemo", env_values=env_values)

# notebook is a plain dict in nbformat 4.5 shape. Mutate it however
# you like before writing — e.g. add a branded markdown cell at the
# top, or replace the verification cell with a query of your own.
notebook["cells"].insert(0, {
    "cell_type": "markdown",
    "metadata": {},
    "source": ["# Acme Corp — Internal Demo\n"],
})

write_notebook(notebook, "MyDemo.ipynb")
```

The notebook dict is plain JSON and conforms to nbformat 4.5, so it
also opens directly in any tooling that consumes notebooks
(`nbconvert`, `papermill`, JupyterLab, VS Code, etc.).

---

## Troubleshooting

**"Unresolved token X in Y" comment appearing in a code cell.**
The DDL referenced a token that isn't in your `--env-config` file.
The renderer leaves the original DDL in place and emits a comment so
the deployment still proceeds for the other statements; fix the env
config and re-render.

**Verification cell prints zero objects.**
Either the wave cells didn't run, or the database names in the env
config differ from what the verification query is looking for. The
verification query uses the same env-resolved database names as the
wave cells, so this usually means a wave cell errored — scroll up
and look at the per-statement progress output.

**"Could not resolve table CallCentre.Foo" inside a wave cell.**
A view or downstream object referenced a table that hasn't been
created yet. This shouldn't happen — the wave ordering is supposed
to prevent it. If it does, it usually means the analyser missed a
reference; re-run `python -m td_release_packager analyse --project
…` and look at the wave assignment, then file an issue with the
DDL that confused the analyser.

**Cells too long to scan.**
Long waves are already collapsed behind `<details>` blocks at the
markdown level. The code cells themselves grow with object count;
for a 75-object wave the cell is several hundred lines of triple-
quoted SQL. That's intentional — the DDL is part of the demo. If a
customer asks for shorter cells, your real ask is probably "split
the data product into smaller modules", not "change the notebook
shape".

---

## Design notes

- **Inline DDL, not a packaged artefact reference.** A Clearscape
  sandbox may have restricted network egress; the notebook must work
  with just the Teradata connection. Inlining the DDL also makes the
  DDL part of the demo narrative — customers can read what's being
  deployed.
- **`getpass` prompt, not env vars.** Keeps the notebook self-
  explanatory. A future iteration can add an env-var fallback if
  customers ask, but defaulting to a prompt removes one assumption
  about the sandbox setup.
- **No `nbformat` runtime dependency.** The renderer hand-writes the
  notebook JSON. Adding `nbformat` would have given marginal
  validation help at the cost of a runtime dependency for a feature
  most SHIPS users don't use.
- **A separate `ships notebook` subcommand, not a `--target` flag on
  `ships package`.** The notebook isn't a release archive — it
  doesn't flow through preflight, trust scoring, or archive
  finalisation. Keeping it under its own command keeps both surfaces
  simple.

---

## Reference example

[examples/callcentre/](../examples/callcentre/) — a full 7-module
AI-Native Data Product (CallCentre) scaffolded into a SHIPS project
and ready to render. See its
[README](../examples/callcentre/README.md) for a step-by-step
Clearscape walkthrough.
