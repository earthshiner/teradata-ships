# Catalogue metadata export (`ships metadata`)

Issue [#244](https://github.com/earthshiner/teradata-ships/issues/244).

SHIPS can enrich an enterprise catalogue with **product-aware** metadata. A
catalogue harvests tables, views, files, and BI assets, but it doesn't know
which assets form a data product, which interfaces are approved for consumption,
what the contract states, whether SHIPS validation passed, or what design
decisions shaped the product. The metadata export fills that gap.

```text
The catalogue harvests what exists.
SHIPS explains why it exists, how it should be used, whether it is trusted,
and which product boundary it belongs to.
```

## Commands

```bash
ships metadata export-alation  --package-dir ./releases/CallCentre_..._01_main --output ./metadata
ships metadata export-collibra --package-dir ./releases/CallCentre_..._01_main --output ./metadata
ships metadata export-datahub  --package-dir ./releases/CallCentre_..._01_main --output ./metadata
```

| Flag | Meaning |
|------|---------|
| `--package-dir` | Root of an unpacked SHIPS package, or a release-group dir with the main package in a subdir (required) |
| `--output` | Output directory; the bundle is written to `<output>/<catalogue>/` (required) |
| `--include-internal` | Treat internal implementation objects (tables) as interfaces too |
| `--strict` | Fail if any required product metadata is missing |

## Architecture — one model, many catalogues

SHIPS extracts a single **neutral `ProductMetadata`** from the package's context
evidence (`td_release_packager.metadata_export`), then each catalogue renderer
projects that one model into its own JSON. Adding a catalogue means adding a
renderer, not re-reading the package.

```
ships package  ──►  context/*.json + payload  ──►  extract_product_metadata()
                                                          │  ProductMetadata
                                          ┌───────────────┼───────────────┐
                                       alation.render               collibra.render
                                          │                               │
                                  alation/ bundle                 collibra/ bundle
```

Sources read: `ships.build.json` (identity + provenance),
`ships.dependencies.json` (physical assets, interfaces, lineage),
`ships.trust.json` + `ships.integrity.json` (trust state), payload DDL (columns)
and DCL (access grants), and `ships.decisions.json` when present.

## Conservative by design

The extractor never fabricates business meaning. Owners, glossary terms,
AI-approval, and data classifications are emitted **only** when present in the
package; otherwise the section is left empty and a warning is recorded. Views and
macros are treated as approved consumer-facing interfaces; tables and other
physical objects are internal unless `--include-internal` is passed. SQL is
parsed as text, never executed.

## Alation bundle

`<output>/alation/`: `data_product.json`, `logical_interfaces.json`,
`physical_mappings.json`, `column_metadata.json`, `glossary_terms.json`,
`lineage.json`, `quality_and_trust.json`, `access_model.json`, `provenance.json`,
`decisions.json`, and `manifest.json` (bundle type/version, source package,
warnings, file list).

## Collibra bundle

`<output>/collibra/`: `collibra_import.json` — a resource list following
Collibra's Import-API conventions (a `Community` → `Domain` → `Asset` graph: the
data product, its interface assets, and column assets, with attributes and
relations; lineage as `Relation` resources) — plus `governance.json` (trust +
access), `provenance.json`, `decisions.json`, and `manifest.json`.

## DataHub bundle

`<output>/datahub/`: `datahub_mcps.json` — a list of MetadataChangeProposals
(MCPs) in the file-emitter `aspect.json` envelope consumable by DataHub's file
source. Emits a `dataset` entity per physical asset (`datasetProperties`,
`subTypes`, `schemaMetadata` with typed fields), `upstreamLineage` per lineage
edge, and a `dataProduct` entity grouping the assets — plus `manifest.json`.
Dataset URNs follow `urn:li:dataset:(urn:li:dataPlatform:teradata,<DB.Object>,<ENV>)`.

## Out of scope (future enhancements)

Direct publishing to Alation/Collibra APIs, authentication, reading live
catalogue state, round-trip drift detection, and AI-inferred business metadata
are intentionally left for later issues. This is a file-only export.
