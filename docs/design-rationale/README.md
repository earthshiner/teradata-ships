# SHIPS Design Rationale

This folder collects the design-philosophy notes that justify how SHIPS structures, names, classifies, and packages database source code. They are intentionally framework-agnostic — the principles apply to any database deployment toolchain — but SHIPS implements them directly.

Read these together if you want to understand *why* the SHIPS pipeline does what it does. Read individually for deep dives on specific surfaces.

## Index

| Document | Covers | When to read it |
|---|---|---|
| [Eponymous, Atomic Database Scripts](./eponymous-atomic-scripts.md) | The "one object, one file, one name, one deployment unit" principle. 20 functional reasons (ownership, source control, impact analysis, deployment ordering, partial deployment, restartability, automation, auditability, drift detection, migration, parallelism, error isolation, expert review, standards enforcement, merge conflicts, generated code, manifests, rollback, release notes, modern engineering practice). | When you need to argue *why* SHIPS splits source files into eponymous artefacts rather than letting monolithic deployment scripts ship as-is. |
| [Object-Type Extensions for Database Scripts](./object-type-extensions.md) | Why `.tbl` / `.viw` / `.grt` / `.cmt` / `.stt` / `.dml` rather than `.sql` for everything. Plays devil's advocate on `.sql` first, then makes the case for object-type extensions as machine-readable metadata. | When deciding what extension to give a generated, harvested, or hand-written database artefact. |
| [DML Naming in an Atomic Database Script Framework](./dml-naming.md) | Eponymous naming applies cleanly to DDL but fails when applied uncritically to DML. Defines when `<db>.<table>.dml` is appropriate, when `<source>.statement_NNN.multi_table.dml` is appropriate, and how the deployment manifest carries primary-target / target-candidates / confidence as the source of truth. | When harvesting raw DML, deciding how to name a multi-target script, or deciding what controls a deployment framework should apply to data-changing scripts. |

## How these connect

Eponymous atomic scripts is the **principle** — the framework's organising idea. Object-type extensions is the **surface** — how the principle shows up in the filesystem. DML naming is the **edge case** — what to do when the principle's "one object" presumption isn't honest.

If you're reading the runsheet at [docs/sessions/runsheet-mortgage-ai-data-product-demo.md](../sessions/runsheet-mortgage-ai-data-product-demo.md) and wondering why the harvester renames everything eponymously and why DML files get treated specially, this folder is the answer.

## How SHIPS implements them today

| Principle | SHIPS implementation | Reference |
|---|---|---|
| Eponymous renaming | `_extract_qualified_name` in `td_release_packager/ingest.py` extracts `db.object` from CREATE / REPLACE / COMMENT ON / COLLECT STATISTICS / INSERT / UPDATE / DELETE / MERGE / GRANT / REVOKE; the harvester renames to `<db>.<obj>.<ext>` | PRs #61–#65 |
| Object-type extensions | `_TYPE_TO_EXT` table in `ingest.py` — `.tbl`/`.viw`/`.mcr`/`.prc`/`.spl`/`.fnc`/`.trg`/`.jix`/`.hix`/`.grt`/`.cmt`/`.stt`/`.dml`/`.db`/`.usr`/`.rol`/`.prf`/`.aut`/`.map`/`.sjr`/`.fsv` | scaffolder + classifier |
| Aggregating types (one file = many statements for one object) | COMMENT / STATISTICS / DML are aggregating types. The harvester appends per-statement; `.cmt` and `.stt` files collect every comment / statistic for a target into one eponymous file | PRs #63, #64, #65 |
| Manifest as source of truth | `context/ships.build.json` carries metadata for each placed file; the deployer reads it for ordering and validation | builder + deployer |

## Open follow-ups (issues)

- [#66](https://github.com/earthshiner/teradata-ships/issues/66) — view-generator should emit `COMMENT ON` inheritance for SHIPS 1:1 locking views.
- A future issue will cover the DML splitter fix + MULTI_TABLE_DML opt-out marker described in [DML Naming](./dml-naming.md). Today multi-INSERT files fall through the splitter's keep-keyword filter and end up bundled under one (misleading) eponymous name; the standard there says: split per statement when each has a clear single target, fall back to source-file naming with manifest metadata when it doesn't, and let an explicit `-- MULTI_TABLE_DML` header force the source-file path when a script's order or transactional intent is the meaning.
