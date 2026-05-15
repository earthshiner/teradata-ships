# SHIPS — Internal Pitch Document
### Standardised Hosting and Ingestion of Packaged Scripts

---

## The problem in one sentence

**An agent cannot ask Dave.**

Traditional Teradata deployment relies on tribal knowledge: Dave knows the DEV database naming convention, Dave knows to skip the third script on Tuesdays, Dave knows what a rollback looks like. That works — until Dave isn't there, or until the deployer isn't a person.

---

## Two audiences, one tool

SHIPS solves deployment for two very different users, and they both use the same commands.

### The developer with a PoC due on Friday

You've built something in a dev sandbox. You need to stand it up properly before the demo. You don't want to write a deployment guide, teach a DBA the schema, or spend an afternoon debugging script execution order. You want to drop your SQL files in a folder and get something deployable.

```bash
python -m td_release_packager process \
    --project my_poc \
    --source my_sql/ \
    --auto-tokenise \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name my_poc
```

That's it. SHIPS classifies every file, resolves token substitutions, sorts deployment order, validates syntax, and produces a self-contained `.zip`. The DBA runs one command. You get a professional deployment with an audit trail, a build number, and rollback capability — without doing any of the plumbing.

**No DBA runbook. No deployment guide. No "run these scripts in this order."**

### The autonomous agent deploying to production

An AI agent has generated a set of database objects as part of a larger workflow. It needs to deploy them to a live Teradata environment — correctly ordered, idempotent, auditable, and recoverable if anything goes wrong.

```python
# The agent does exactly what the human does
result = ships_mcp.process(
    project_dir="/projects/OMR",
    source_dir="/generated/ddl/",
    auto_tokenise=True,
    env="PRD",
    env_config="config/env/PRD.conf",
    package_name="OMR",
    strict=True
)

if result.trust_label == "READY":
    ships_mcp.deploy(package_path=result.archive_path, ...)
```

The agent follows the same workflow as a human. The same commands. The same artefacts. The same audit trail. **The only difference is who types the command.**

---

## The SHIPS spectrum

SHIPS scales from a developer's afternoon project to a governed enterprise deployment without changing the workflow — only the ceremony level.

| | **Personal / PoC** | **Team / Project** | **Enterprise / Governed** |
|---|---|---|---|
| **Who deploys** | The developer | A DBA | An agent or CI pipeline |
| **Build number** | Auto-incremented | Auto-incremented, Git-tracked | Auto-incremented, signed |
| **Token review** | Auto-tokenise | Token map reviewed and committed | Token map in source control |
| **DBA handoff** | Optional — self-deploy | Formal — package + trust report | Automated — pipeline gate |
| **Audit trail** | DBQL query band | DBQL + decisions.json | DBQL + OpenLineage + OTel |
| **Drift detection** | Not configured | Optional | Required |
| **Schema drift** | N/A | Warn-on-drift | Abort-on-drift |
| **Compliance** | N/A | Change log | SOX / APRA evidence ready |

The developer doing a PoC still gets a build number, a rollback mechanism, and a tamper-proof package — for free. They just don't need to care about them unless something goes wrong.

---

## What the package IS

A SHIPS package is a **Deployment Product** — the same self-contained, governed, observable, portable pattern used for AI-Native Data Products, applied to the deployment lifecycle.

A Data Product wraps data so consumers can trust it without understanding the ETL. A SHIPS package wraps DDL so DBAs and agents can trust the deployment without understanding the development.

```
releases/DEV_OMR_BUILD_0042_20260510/
    DEV_OMR_BUILD_0042_20260510_01_main.zip
    DEV_OMR_BUILD_0042_20260510_01_main.zip.sha256
    release_group.json
    README.txt

Inside the package archive:
    deploy.py                   ← The only thing anyone runs
    context/ships.build.json                  ← Who built it, when, from what commit
    context/ships.integrity.json      ← SHA-256 tamper detection
    _waves.txt                  ← Deployment dependency ordering
    lib/
        database_package_deployer/   ← Full deployment engine, embedded
    payload/database/
        DDL/tables/             ← Fully resolved, tokenised DDL
        DDL/views/
        DDL/procedures/
    _rollback/                  ← Pre-deployment SHOW captures (populated at deploy)
```

No external knowledge required. No environment setup. No script editing. **Unzip and deploy.**

---

## Why agents need SHIPS

Traditional deployment artefacts were designed for humans. They assume:
- Someone will read the README
- Someone knows the database naming conventions
- Someone will notice if the order is wrong
- Someone will handle the rollback if something breaks

Agents cannot make those assumptions. An agent hitting a traditional deployment process gets stuck at the first ambiguity.

SHIPS removes the ambiguity:

| Human assumption | SHIPS replaces it with |
|---|---|
| "Ask Dave how DEV databases are named" | Token map in source control — machine-readable, no conversation required |
| "You just know tables go before views" | Topological dependency graph — computed, not assumed |
| "Review this and email the DBA" | Trust Report — pass/fail signal the agent can act on without judgement |
| "Run these scripts in this order" | `_waves.txt` — explicit, deterministic, generated from analysis |
| "If it fails, Dave took a backup" | Pre-deployment SHOW captures in `_rollback/` — automatic, verifiable |
| "Did the deployment succeed?" | JSON manifest — per-object state, machine-readable |

Every step the human does implicitly, SHIPS makes explicit. Every check a DBA performs manually, SHIPS performs automatically. Every piece of context that lives in a developer's head, SHIPS records in a file.

**The agent does not need to understand Teradata deployment. It needs to understand the SHIPS contract.**

---

## The trust layer

Every SHIPS package earns a trust label before deployment:

```
================================================================
  Package Trust: ✓ READY
================================================================
  ✓ inspect_token_format     No malformed token markers
  ✓ inspect_lint             No Coding Discipline violations
  ✓ inspect_grants           Grant coverage clean
  ✓ provenance_complete      Provenance document present
================================================================
```

| Label | What it means | Agent action |
|---|---|---|
| **READY** | All signals pass | Proceed to deployment |
| **READY-WITH-CAVEATS** | Warnings present | Proceed and log warnings |
| **BLOCKED** | Critical signal failed | Stop, escalate, do not deploy |

For a human, this is a pre-deployment checklist. For an agent, it is an executable gate — no judgement required.

---

## Schema drift: knowing what changed

SHIPS knows what it last deployed for every object. Before deploying an object, it compares the current live state (via `SHOW TABLE/VIEW/PROCEDURE`) against what it deployed last time.

If a DBA made an emergency hotfix between deployments, SHIPS surfaces it:

```
⚠ DRIFT DETECTED: OMR_STD.Customer
  Object was changed out-of-band since last SHIPS deploy.

  --- OMR_STD.Customer (last SHIPS deploy)
  +++ OMR_STD.Customer (current database)
  @@ -3,6 +3,7 @@
       Id INTEGER NOT NULL
      ,Name VARCHAR(100)
  +   ,Region VARCHAR(50)
   )
```

The agent or DBA decides: overwrite (SHIPS wins), skip (hotfix preserved), or abort (investigate first). Nothing is hidden.

**This is the difference between a deployment tool and a governed deployment framework.** A deployment tool executes. A governed deployment framework knows the difference between what it deployed and what's in the database — and tells you.

---

## Rollback: three layers

| Rollback type | Command | When |
|---|---|---|
| **Wave rollback** | `deploy.py rollback <manifest> --wave 3` | A specific deployment wave failed — undo just that wave |
| **Full build rollback** | `deploy.py rollback <manifest>` | Undo the entire deployment using pre-captured DDL snapshots |
| **Feature rollback** | `ships rollback --to-tag v1.2.3 --env PRD` | The wrong feature shipped — re-deploy from a previous git tag |

Feature rollback is the enterprise safety net: if a deployment went live but was wrong, one command rebuilds from the tagged source and deploys with drift-overwrite enabled. The audit trail records the rollback as a distinct build, traceable back to the original tag commit.

---

## Observability: what's happening and what changed

SHIPS integrates with two complementary observability standards:

**OpenTelemetry** answers: *did it succeed, and how long did it take?*
```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318
# Every pipeline stage now emits a span to your tracing backend
```

**OpenLineage** answers: *what data assets were created and what feeds what?*
```bash
export OPENLINEAGE_URL=http://marquez:5000
export OPENLINEAGE_NAMESPACE=teradata://td-prod.myorg.com:1025
# Every deployment emits START/COMPLETE/FAIL events to your data catalog
```

Both are zero-configuration: set an environment variable and they're active. Unset it and they're silent. No code changes, no performance overhead.

---

## Designed for the whole lifecycle

```
Developer writes SQL
       ↓
ships onboard     ← Scans source, recommends tokenisation path
       ↓
ships process     ← Harvest → Inspect → Analyse → Package (one command)
       ↓
Package archive   ← Self-contained, signed, trust-scored
       ↓
DBA or Agent      ← deploy.py — one command, no knowledge required
       ↓
DBQL audit trail  ← Every statement tagged: BUILD, PKG, ENV, WAVE
       ↓
OpenLineage       ← Deployed objects appear in your data catalog
       ↓
decisions.json    ← Full run history, queryable by agents
       ↓
Drift detection   ← Next deployment knows what changed out-of-band
       ↓
Rollback ready    ← Wave, build, or tag — three layers of recovery
```

---

## The pitch to your client

> *"Your development team writes SQL. Your DBAs deploy it. Your agents will eventually do both. Today, none of them have a common language for what a deployment looks like. SHIPS is that language. It works the same way whether a developer is running a Friday-afternoon PoC or an agent is autonomously deploying to production at 2am. The audit trail, the trust score, the rollback mechanism, and the data catalog integration are there either way — the developer doesn't even have to think about them."*

---

## Why now

Three things are converging:

1. **AI agents are entering the deployment lifecycle.** Teradata customers are exploring agentic workflows for data engineering. Those agents need a deployment contract they can follow without human mediation.

2. **The compliance bar is rising.** SOX, APRA CPS 234, and equivalent standards increasingly require evidence that deployed systems are traceable, integrity-verified, and auditable. Manual scripts cannot satisfy that evidence burden at scale.

3. **The developer experience bar is rising.** Developers who've worked in modern CI/CD pipelines expect packaging and deployment to be push-button. SHIPS brings that expectation to the Teradata ecosystem — for the first time.

SHIPS addresses all three simultaneously, with one tool, one standard, and no change to how developers write SQL.

---

## Getting started

**One project, one afternoon:**

```bash
# 1. Install
git clone https://github.com/earthshiner/teradata-deployment-agent
cd teradata-deployment-agent && uv sync

# 2. Onboard an existing SQL project (wizard recommends the path)
python -m td_release_packager onboard --source /my/sql/

# 3. Process (scaffold + harvest + inspect + analyse + package)
python -m td_release_packager scaffold --name MyProject
python -m td_release_packager process \
    --project MyProject \
    --source /my/sql/ \
    --auto-tokenise \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name MyProject

# 4. Deploy
cd releases/DEV_MyProject_BUILD_0001_<timestamp>/
unzip DEV_MyProject_BUILD_0001_<timestamp>_01_main.zip
cd MyProject_DEV_BUILD_0001_*/
python deploy.py --host myserver --user dbc
```

**For an existing codebase:** use `ships onboard` to get a tailored recommended path.

**For agents:** connect via the MCP server — see [docs/MCP_GUIDE.md](./MCP_GUIDE.md).

**For enterprise governance:** configure `deployment.baseline_dir` in `ships.yaml`, set `OPENLINEAGE_URL`, and add `OTEL_EXPORTER_OTLP_ENDPOINT` — see [docs/OBSERVABILITY.md](./OBSERVABILITY.md).

**To package directly from GitHub** (no local clone needed):

```bash
# Public repository — no token
python -m td_release_packager process \
    --project MyProject \
    --source-github myorg/myrepo \
    --source-ref main \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name MyProject

# Private repository
export GITHUB_TOKEN=ghp_your_token
python -m td_release_packager process \
    --project MyProject \
    --source-github myorg/myrepo \
    --source-ref v1.4.2 \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name MyProject
```

SHIPS downloads the repository tarball via the GitHub REST API — no `git` required. The resolved commit SHA is stamped into `context/ships.build.json` automatically. For GitHub Enterprise Server, set `SHIPS_GITHUB_API_URL`.
