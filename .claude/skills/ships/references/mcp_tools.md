# SHIPS MCP Tools

Load this when a task needs the agent/MCP tool surface, deployment safeguards, or HTTP transport/auth details.

## Transports

- `python -m ships_mcp`: stdio transport for desktop agents.
- `python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000`: enterprise HTTP transport.
- `python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000 --stateless`: stateless HTTP mode for serverless or load-balanced deployments.

HTTP-only flags (`--host`, `--port`, `--path`, `--stateless`) are rejected for stdio.

## Authentication

SHIPS validates JWT bearer tokens for HTTP transports. Enable auth with JWKS settings:

```bash
python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000 \
  --auth-jwks-uri https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys \
  --auth-issuer https://login.microsoftonline.com/{tenant}/v2.0 \
  --auth-audience api://ships-mcp \
  --auth-required-scopes ships.read,ships.deploy \
  --auth-resource-url http://ships-mcp.internal:8000
```

`--auth-jwks-uri` enables auth. `--auth-resource-url` is required when JWKS auth is enabled. JWKS is cached for 1 hour and refreshed on unknown `kid`. Runtime dependencies are `PyJWT[crypto]` and `httpx`.

## Tool groups

Pipeline tools without database connection:

- `ships_scaffold`
- `ships_harvest`
- `ships_generate`
- `ships_inspect`
- `ships_analyse`
- `ships_package`
- `ships_process`

Plan / changeset tools (no database connection):

- `ships_plan` — detect-and-recommend a packaging plan from a raw source tree (#379).
  Params: `source` (required), `project`, `env`, `name`, `mode` (`quick`|`detailed`),
  `strict`, `scaffolded`, `no_generate`.
  Returns: `{success, detected[], notes[], commands[], rationale[], plan{}}`.
- `ships_changeset` — preview changed objects + downstream dependants (#114/#115).
  Params: `project` (required), `since_tag`, `since_commit`, `objects`.
  Returns: `{success, mode (git|baseline|objects|none), changed[], dependants[],
  selected[], changed_files[], note}`. Feed the same `since_*`/`objects` to
  `ships_package` for a changeset-scoped build.

Catalogue export tool (no database connection):

- `ships_metadata_export` — export AI-native data-product metadata from a built
  package (#244). Params: `package_dir` (required), `output` (required),
  `catalogue` (`alation`|`collibra`|`datahub`), `include_internal`, `strict`.
  Returns: `{success, catalogue, output_dir, files[], interfaces, assets, columns,
  warnings[]}`.

`ships_package` extra params: `since_tag` / `since_commit` / `objects`
(changeset-scoped build), `source_github` / `source_ref` / `github_token`
(clone-free GitHub source), `root_parent`, `change_ref`.

`ships_process` extra params: `source_github` / `source_ref` / `github_token`,
`output`, `root_parent`, `author` / `description` / `commit`. Omitting
`env` / `env_config` / `name` derives them from the `ships.yaml` `packaging:`
profile (#384), so `ships_process(project=...)` runs the full pipeline argless.

> The interactive `ships wizard` (#381) is **not** an MCP tool — MCP is
> non-interactive. Use `ships_plan` for the agent path.

Deployment tools requiring `host`, `user`, and `password`:

- `ships_deploy`
- `ships_rollback`
- `ships_deploy_explain`

Read-only / authoring tools:

- `ships_decisions`
- `ships_verify`
- `ships_explain_run`
- `ships_status`
- `ships_describe_package`
- `ships_validate_*` / `ships_author_*`, `ships_apply_diff`,
  `ships_explain_violation`, `ships_list_fixable_rules`, `ships_fix`, `ships_clean`

## Deployment guardrails

Always inspect Package Trust Score before deployment. If `trust_label` is `BLOCKED`, do not call `ships_deploy`; fix Inspect, provenance, reproducibility, or token issues first.

## End-to-end agentic flow

`ships_plan` → `ships_process` (argless via `packaging:` profile, or from GitHub) → `ships_changeset` → `ships_package` (changeset-scoped) → `ships_verify` (gate on `trust_label`) → `ships_deploy` → `ships_metadata_export`. Every step returns structured JSON with an audit trail in `decisions.json`.
