# Custom lint policy (`config/ships_lint_policy.yaml`)

SHIPS ships a fixed set of built-in Coding Discipline rules. A **custom lint
policy** lets a team add its own Teradata deployment rules as data — no Python,
no fork. Custom rules run during `ships inspect` alongside the built-in checks
and appear in the same console output and in `ships.decisions.json`.

- **Location:** `<project>/config/ships_lint_policy.yaml` (optional; absent ⇒ no custom rules).
- **Safety:** patterns are matched with Python `re` against payload text. SQL is
  treated as **data, never executed**. A rule cannot run arbitrary code.
- **Fail behaviour:** a malformed policy **fails closed** under `inspect --strict`
  (the run aborts); in developer mode an invalid individual rule is logged and
  skipped, and a structurally broken file is ignored with a warning.

## Rule shape

```yaml
rules:
  - name: no_replace_view                 # required, unique
    description: Use CREATE VIEW. The deployer owns idempotency.
    severity: ERROR                        # ERROR | WARNING | INFO | OFF
    applies_to:
      object_types: [VIEW]                 # optional scope; empty ⇒ all types
      phases: [DDL]                        # optional scope; empty ⇒ all phases
    deny_pattern: '^\s*replace\s+view\b'   # finding fires when this matches
    # required_pattern: '...'              # finding fires when this is ABSENT
    # exclude_pattern: '-- ships:allow'    # suppress the finding when this matches
    remediation:
      safe_fix_available: true
      automation_level: reviewable_codemod
      recommended_action: Change REPLACE to CREATE for supported object types.
      requires_human_review: false
```

A rule needs **at least one** of `deny_pattern` (fires on match) or
`required_pattern` (fires when missing). `exclude_pattern`, when present and
matched, suppresses the finding (an in-source waiver). All patterns are
case-insensitive and multiline.

### Scope vocabulary

- **`object_types`** — `TABLE`, `VIEW`, `MACRO`, `PROCEDURE`, `FUNCTION`,
  `TRIGGER`, `JOIN_INDEX`, `DATABASE`, `USER`, `DML`, …, plus `DCL` (a
  convenience alias matching GRANT/REVOKE). Empty ⇒ applies to every type.
- **`phases`** — `DDL`, `DCL`, `DML`, `PREREQS` (aliases: `pre-requisites`,
  `pre_requisites`), `POST_INSTALL` (alias: `post-install`). Empty ⇒ every phase.

The file's object type is derived from its DDL content; its phase from its
location under `payload/database/<phase>/`.

### Remediation metadata (agent-facing)

Carried verbatim into each finding's `details` in `ships.decisions.json` so
agents and CI can decide what to do:

| Key | Type | Meaning |
|---|---|---|
| `safe_fix_available` | bool | A safe automated fix exists. |
| `automation_level` | str | e.g. `reviewable_codemod`, `assisted`, `manual_review_required`. |
| `agent_may_fix` / `agent_may_suggest` | bool | What an agent is permitted to do. |
| `requires_human_review` | bool | A human must review before deploy. |
| `requires_live_metadata` | bool | Assessment needs live database metadata. |
| `stop_condition` / `blocked_action` | str | What is blocked and why. |
| `recommended_action` | str | Human-readable next step. |

Unknown remediation keys pass through unchanged (forward-compatible).

## Teradata examples

```yaml
rules:
  # Views must declare an explicit column contract before AS.
  - name: require_view_column_contract
    description: Views must declare an explicit column list before AS.
    severity: ERROR
    applies_to: { object_types: [VIEW], phases: [DDL] }
    required_pattern: '^\s*create\s+view\s+\S+\s*\('
    remediation:
      automation_level: assisted
      requires_human_review: true
      recommended_action: Add an explicit view column list before AS.

  # Dynamic SQL in procedures needs review before agentic deployment.
  - name: no_dynamic_sql_without_review
    description: Dynamic SQL requires review before agentic deployment.
    severity: WARNING
    applies_to: { object_types: [PROCEDURE], phases: [DDL] }
    deny_pattern: '\b(execute\s+immediate|dbc\.sysexecsql)\b'
    remediation:
      automation_level: manual_review_required
      requires_human_review: true
      recommended_action: Review dynamic SQL for injection, privilege, and deployment risk.

  # No DELETE without a WHERE clause in deployable DML.
  - name: no_unqualified_delete
    description: DELETE without WHERE is not allowed in deployable payloads.
    severity: ERROR
    applies_to: { phases: [DML] }
    deny_pattern: '^\s*delete\s+from\s+\S+\s*;'
    remediation:
      requires_human_review: true
      recommended_action: Add a WHERE clause or move the purge to an operational runbook.
```

## How findings appear

- **Console:** `[<rule_name>] <description>` under Step 1, with the rule's severity.
- **JSON (`ships.decisions.json`):** an issue with `code: INSPECT_CUSTOM_POLICY`,
  the rule name in `message`, and the remediation block in `details`.
- **`--strict`:** `WARNING` custom findings are promoted to `ERROR` (parity with
  built-in rules); `OFF` rules load but never fire.
