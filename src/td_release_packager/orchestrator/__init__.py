"""
orchestrator — SHIPS pipeline orchestration foundation.

Exposes three load-bearing primitives that every later orchestrator
stage depends on:

    ships_yaml  — schema, Layer 1 defaults, parser, validator,
                  generate-on-first-run helper for ships.yaml.
    cascade     — five-layer configuration resolver (CLI > env-properties
                  > project ships.yaml > platform template > defaults).
    decisions   — append-only writer for decisions.json, the audit
                  trail every stage records its run into.

These are foundation only — they do not modify existing stages and
they do not introduce a `process` verb. Refactoring stages onto
this foundation is a separate piece of work (build-order item 4 in
the orchestrator design).
"""

from td_release_packager.orchestrator.cascade import (
    Cascade,
    CascadeConfigError,
    LayerSource,
    ResolvedSetting,
    SettingNotFound,
)
from td_release_packager.orchestrator.decisions import (
    DECISIONS_FILENAME,
    FINAL_STATUSES,
    ISSUE_SEVERITIES,
    SCHEMA_VERSION,
    STAGE_STATUSES,
    DecisionsCorruptError,
    DecisionsError,
    DecisionsManifest,
    DecisionsSchemaError,
    RunRecorder,
    StageRecorder,
)
from td_release_packager.orchestrator.issue_codes import (
    INSPECT_GRANT_VIOLATION,
    INSPECT_LINT_VIOLATION,
    INSPECT_TOKEN_MALFORMED,
    ANALYSE_CYCLE,
    ANALYSE_EXTERNAL_REF,
    GENERATE_ERROR,
    GENERATE_WARNING,
    HARVEST_CLASSIFICATION_WARNING,
    HARVEST_TOKEN_CANDIDATE,
    HARVEST_UNCLASSIFIED,
    ISSUE_CODES,
    PACKAGE_WARNING,
    PROPERTIES_NOT_FOUND,
    TOKEN_UNDEFINED,
    TOKEN_UNUSED,
    describe,
    is_registered,
)
from td_release_packager.orchestrator.ships_yaml import (
    LAYER_1_DEFAULTS,
    STAGES,
    VALID_ON_ERROR,
    ShipsConfigError,
    ValidationError,
    apply_defaults,
    generate_default,
    load,
    validate,
    write_if_missing,
)

__all__ = [
    # ships_yaml
    "LAYER_1_DEFAULTS",
    "STAGES",
    "VALID_ON_ERROR",
    "ShipsConfigError",
    "ValidationError",
    "apply_defaults",
    "generate_default",
    "load",
    "validate",
    "write_if_missing",
    # cascade
    "Cascade",
    "CascadeConfigError",
    "LayerSource",
    "ResolvedSetting",
    "SettingNotFound",
    # decisions
    "DECISIONS_FILENAME",
    "FINAL_STATUSES",
    "ISSUE_SEVERITIES",
    "SCHEMA_VERSION",
    "STAGE_STATUSES",
    "DecisionsCorruptError",
    "DecisionsError",
    "DecisionsManifest",
    "DecisionsSchemaError",
    "RunRecorder",
    "StageRecorder",
    # issue_codes
    "ANALYSE_CYCLE",
    "ANALYSE_EXTERNAL_REF",
    "GENERATE_ERROR",
    "GENERATE_WARNING",
    "HARVEST_CLASSIFICATION_WARNING",
    "HARVEST_TOKEN_CANDIDATE",
    "HARVEST_UNCLASSIFIED",
    "INSPECT_GRANT_VIOLATION",
    "INSPECT_LINT_VIOLATION",
    "INSPECT_TOKEN_MALFORMED",
    "ISSUE_CODES",
    "PACKAGE_WARNING",
    "PROPERTIES_NOT_FOUND",
    "TOKEN_UNDEFINED",
    "TOKEN_UNUSED",
    "describe",
    "is_registered",
]
