# SHIPS Observability Guide
### OpenTelemetry tracing and OpenLineage data catalog integration

---

## Overview

SHIPS emits two complementary observability signals from the deployment pipeline:

| Signal | Standard | Answers | Tools |
|---|---|---|---|
| **Tracing** | OpenTelemetry | Did it succeed? How long did each stage take? | Jaeger, Zipkin, Grafana Tempo, Datadog, Honeycomb |
| **Lineage** | OpenLineage | What data assets were created? What feeds what? | Marquez, DataHub, Apache Atlas, OpenMetadata |

Both signals use the same activation model: set an environment variable, and the signal is live. Unset it, and the module is a complete no-op — zero overhead, no import cost.

---

## OpenTelemetry tracing

### What is traced

Each SHIPS pipeline stage emits an OpenTelemetry span when tracing is active. The span covers the full duration of the stage and carries key attributes:

| Span name | Stage | Key attributes |
|---|---|---|
| `ships.harvest` | Harvest | `ships.source_dir`, `ships.files_processed`, `ships.tokens_replaced` |
| `ships.inspect` | Inspect | `ships.error_count`, `ships.warning_count` |
| `ships.analyse` | Analyse | `ships.object_count`, `ships.wave_count` |
| `ships.package` | Package | `ships.package_name`, `ships.build_number`, `ships.file_count` |
| `ships.deploy` | Deploy | `ships.package_dir`, `ships.dry_run`, `ships.total`, `ships.completed`, `ships.failed` |

All spans share the same `ships` tracer and are parented under the pipeline run, so a full `process` run appears as a single trace with one child span per stage.

### Activation

Install the OTel extras:

```bash
pip install "ships[otel]"
# or
uv pip install -e ".[otel]"
```

Set the endpoint:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://my-collector:4318
```

Optional configuration:

```bash
export OTEL_SERVICE_NAME=ships              # default: ships
export OTEL_SDK_DISABLED=true               # disable without uninstalling
```

That is all. The next pipeline run will export traces automatically.

### Disabling tracing

Set `OTEL_SDK_DISABLED=true` or unset `OTEL_EXPORTER_OTLP_ENDPOINT`. No code changes required.

### Backend setup examples

#### Jaeger (local development)

```bash
docker run --rm -p 16686:16686 -p 4318:4318 \
    jaegertracing/all-in-one:latest

export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Open `http://localhost:16686` and search for service `ships`.

#### Grafana Tempo

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318
export OTEL_SERVICE_NAME=ships-deploy
```

#### Datadog

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://datadog-agent:4318
export OTEL_SERVICE_NAME=ships
```

### What a trace looks like

A `ships process` run with packaging produces a trace like this:

```
ships.process (12.4s)
  ├─ ships.harvest   (1.2s)  files_processed=47, tokens_replaced=312
  ├─ ships.inspect   (0.8s)  error_count=0, warning_count=2
  ├─ ships.analyse   (0.4s)  object_count=47, wave_count=6
  └─ ships.package   (2.1s)  build_number=0042, file_count=47
```

A `deploy_package` call produces:

```
ships.deploy (38.2s)  completed=47, failed=0, success=true
```

### Notes

- The OTel exporter is OTLP/HTTP (not gRPC). Ensure your collector accepts HTTP on port 4318.
- Spans are batched and exported asynchronously — a short pipeline run may complete before the final spans flush. SHIPS calls `force_flush` on the provider at process exit.
- If the collector is unreachable, span export fails silently. The pipeline run is not affected.

---

## OpenLineage data catalog integration

### What is lineage

OpenTelemetry tells you *how long it took and whether it failed*. OpenLineage tells you *what data assets were created and what feeds what*. They complement each other.

SHIPS emits OpenLineage `RunEvent` messages from the deploy pipeline. These are consumed natively by Marquez, DataHub, Apache Atlas, OpenMetadata, and any other OpenLineage-compatible data catalog — giving operations teams a live, queryable view of which Teradata objects were deployed by which build.

### Events emitted

#### `ships.deploy` job — one run per `deploy_package` call

| Event | When | Outputs |
|---|---|---|
| `START` | Entry to `deploy_package`, before any DDL | Empty — objects not yet created |
| `COMPLETE` | All objects deployed successfully | Every `COMPLETED` object as a dataset |
| `FAIL` | One or more objects failed, or unexpected exception | Partial — completed objects only; failed objects listed in `shipsFailures` facet |

All three events share the same `run_id` (a UUID generated at `START`), so the catalog can correlate them into a single deployment run.

### Output datasets

Every successfully deployed object appears as an OpenLineage `Dataset` with:

- **namespace**: `teradata://<host>` (from `OPENLINEAGE_NAMESPACE` env var, or `teradata://unknown` if not set)
- **name**: `DATABASE.OBJECT_NAME` (fully qualified)

Example: a view `REPORTING.MONTHLY_SALES_V` deployed to `td-prod.myorg.com` produces:

```json
{
  "namespace": "teradata://td-prod.myorg.com",
  "name": "REPORTING.MONTHLY_SALES_V"
}
```

### ShipsRunFacet

Every event carries a custom `ships` run facet with build provenance:

```json
{
  "ships": {
    "build_number": "0042",
    "environment": "PRD",
    "package_name": "OMR",
    "package_filename": "PRD_OMR_BUILD_0042_20260510_01_main.zip",
    "release_group": "PRD_OMR_BUILD_0042_20260510",
    "dry_run": false
  }
}
```

This links the lineage event directly to the package build — a data engineer can look up which build created a dataset and pull the full provenance from `context/ships.build.json`.

### Activation

No extra packages required — SHIPS uses stdlib HTTP and file I/O for transport.

Set the transport endpoint:

```bash
# HTTP push to a live catalog backend
export OPENLINEAGE_URL=http://marquez:5000

# Append-only NDJSON file (CI, air-gapped environments)
export OPENLINEAGE_URL=file:///var/log/ships/lineage.ndjson
```

Optional configuration:

```bash
# Override the dataset namespace (default: teradata://unknown)
export OPENLINEAGE_NAMESPACE=teradata://td-prod.myorg.com:1025

# Disable without removing the URL
export OPENLINEAGE_DISABLED=true
```

The next `deploy_package` call will emit events automatically.

If you want to use the full `openlineage-python` client ecosystem (extended transport options, client-side validation), install the optional extra:

```bash
pip install "ships[lineage]"
```

### Backend setup examples

#### Marquez (reference implementation)

```bash
docker run --rm -p 5000:5000 -p 3000:3000 \
    marquezproject/marquez:latest

export OPENLINEAGE_URL=http://localhost:5000
export OPENLINEAGE_NAMESPACE=teradata://localhost:1025
```

Open `http://localhost:3000` to explore the lineage graph. Deploy a package and refresh — the `ships.deploy` job appears immediately.

#### File transport (CI / air-gapped)

```bash
export OPENLINEAGE_URL=file:///var/log/ships/lineage.ndjson
```

Events are appended as NDJSON (one JSON object per line). You can ship this file to your catalog in a nightly batch, or consume it with any NDJSON reader:

```python
import json
from pathlib import Path

events = [
    json.loads(line)
    for line in Path("/var/log/ships/lineage.ndjson").read_text().splitlines()
]
completed = [
    ev for ev in events
    if ev["eventType"] == "COMPLETE"
]
```

#### DataHub

```bash
export OPENLINEAGE_URL=http://datahub-gms:8080
export OPENLINEAGE_NAMESPACE=urn:li:dataPlatform:teradata
```

#### OpenMetadata

```bash
export OPENLINEAGE_URL=http://openmetadata:8585/api/v1/lineage
```

### Disabling lineage

Unset `OPENLINEAGE_URL` or set `OPENLINEAGE_DISABLED=true`. No code changes required.

### Transport reliability

All transport errors are swallowed and logged at DEBUG level. A lineage catalog outage **never blocks a deployment**. If the HTTP endpoint is unreachable or the file path is not writable, SHIPS logs the failure and continues normally.

HTTP requests time out after 5 seconds.

### What lineage is not

- **OpenLineage does not replace `decisions.json`** — `decisions.json` is the SHIPS-native audit trail covering all pipeline stages. OpenLineage is the catalog-integration layer for data governance tools.
- **Phase 1 emits output datasets only** — input datasets (which objects a view depends on) are not yet populated in the `deploy_package` events. They are available via the static `analyse → export openlineage` export (see below).
- **Dry-run deployments emit START and FAIL events** — not COMPLETE, because no DDL was executed. The `ShipsRunFacet` carries `dry_run: true`.

### Combining with the static analysis export

The `ships analyse` command can export the full dependency graph in OpenLineage format without requiring a deploy or a running catalog:

```bash
python -m td_release_packager analyze \
    --source C:\Projects\OMR \
    --formats openlineage \
    --output C:\Projects\OMR\releases
```

This writes `<name>.openlineage.json` — a NDJSON file with one `COMPLETE` event per object, including full input/output dataset edges derived from the dependency graph. Use this to pre-populate a catalog before deploying, or to audit lineage in a read-only environment.

The static export and the runtime events are complementary: the static export provides the *dependency structure*; the runtime events provide the *when was it deployed* dimension.

---

## Environment variable reference

| Variable | Module | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel | OTLP/HTTP endpoint. Required to enable tracing. |
| `OTEL_SERVICE_NAME` | OTel | Service name on all spans (default: `ships`) |
| `OTEL_SDK_DISABLED` | OTel | Set to `true` to disable tracing without uninstalling |
| `OPENLINEAGE_URL` | Lineage | Transport endpoint. Required to enable lineage emission. |
| `OPENLINEAGE_NAMESPACE` | Lineage | Dataset namespace override (default: `teradata://unknown`) |
| `OPENLINEAGE_DISABLED` | Lineage | Set to `true` to disable lineage without removing the URL |

---

## Frequently asked questions

**Do I need both OTel and OpenLineage?**

No. They serve different audiences. If your team uses a tracing tool (Jaeger, Grafana, Datadog), enable OTel. If you use a data catalog (Marquez, DataHub), enable OpenLineage. If you use both, enable both — they are independent.

**Does enabling these signals slow down deployments?**

OTel: negligible. Spans are batched and exported asynchronously after the pipeline stage completes.

OpenLineage: near-zero. The START event is emitted synchronously before any DDL (a single JSON write or HTTP POST). COMPLETE/FAIL events are emitted after all DDL completes. The 5-second HTTP timeout is the only exposure — if the endpoint is unreachable, the timeout adds 5 seconds to the post-deploy step, not to the deployment itself.

**What if the catalog backend is down?**

Both modules swallow transport errors silently. A catalog outage never blocks a deployment or causes an error. Check your backend connectivity separately.

**Can I emit lineage from an air-gapped environment?**

Yes — use the file transport (`OPENLINEAGE_URL=file:///path/to/lineage.ndjson`). Collect the NDJSON file and ship it to your catalog backend when connectivity is available.

**The COMPLETE event has no input datasets for my view. Is this a bug?**

No — Phase 1 of the lineage integration emits output datasets only. The dependency edges (which objects a view reads from) will be populated in Phase 2, when the `AnalysisResult` is threaded into `deploy_package`. For now, use the static `analyse --formats openlineage` export to get the full dependency graph.
