"""
Provenance Schema (v2) — SHIPS Filename Transformation Chain.

Tracks each filename transformation a payload file undergoes during
packaging, so the HTML report can show a DBA exactly where in the
pipeline a mapping decision was made (or went wrong).

The chain has four stages, in pipeline order:

    source           Original filename in the project source tree.
    eponymous        After _resolve_filename(...) extracts the
                     qualified Database.ObjectName from resolved DDL
                     content and renames the file to match.
    token_resolved   After {{TOKEN}} substitution is applied to the
                     filename itself (only fires when the eponymous
                     stage left tokens in the name, e.g.
                     "{{DOM_DATABASE_T}}.db").
    package          Final path inside the package archive
                     (phase prefix + sub_dir + resolved filename).

Each stage has a status:

    applied          Transformation ran and the path changed.
    no_op            Transformation ran but the path was unchanged
                     (e.g. no tokens in filename to substitute, or
                     DDL had no qualified name to derive from).
    skipped          Transformation did not run for this file type
                     (e.g. binary file — UnicodeDecodeError path).
    failed           Transformation hit an error. The note field
                     carries the error context.

Per Coding Discipline rule 9 (fail fast / no silent skips), the
status field is mandatory — "filename unchanged between two
stages" is otherwise ambiguous.

Schema version: 2 (no backward compat with v1 — v1 was a flat
{package_path: source_path} dict).

Author: SHIPS / Teradata Field Engineering
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# Schema version — bumped on any breaking change. Readers MUST
# refuse to load a JSON document with a different version.
SCHEMA_VERSION = 2


# Pipeline stage names, in the order they run inside _copy_payload.
# The list is canonical — a chain MUST contain exactly these four
# entries, in this order, even when individual stages are no_op or
# skipped. This invariant lets consumers iterate without branching.
STAGE_ORDER: List[str] = [
    "source",
    "eponymous",
    "token_resolved",
    "package",
]


class Status(str, Enum):
    """
    Status of a single transformation stage.

    Subclassing str makes the enum JSON-serialisable directly via
    asdict() without a custom encoder.
    """

    APPLIED = "applied"
    NO_OP = "no_op"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class Stage:
    """
    A single transformation stage in the chain.

    Attributes:
        stage:  One of the canonical stage names (see STAGE_ORDER).
        path:   The file path as it stands at the END of this stage.
                For ``source`` this is the input filename; for each
                subsequent stage it is the path AFTER the stage's
                transformation has run (or would have run).
        status: Outcome — see the Status enum.
        note:   Human-readable explanation. Required for non-applied
                statuses (no_op, skipped, failed) so the report can
                explain why the path didn't change. Optional for
                applied (the path change speaks for itself, but a
                note still helps clarify what changed and why).
    """

    stage: str
    path: str
    status: Status
    note: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate stage name and require a note for non-applied statuses."""
        if self.stage not in STAGE_ORDER:
            raise ValueError(
                f"[ProvenanceSchema] Unknown stage name '{self.stage}'. "
                f"Valid stages: {', '.join(STAGE_ORDER)}."
            )

        # Per discipline rule 9 — non-applied statuses MUST explain
        # themselves. A no_op without explanation is a silent skip.
        if (
            self.status in (Status.NO_OP, Status.SKIPPED, Status.FAILED)
            and not self.note
        ):
            raise ValueError(
                f"[ProvenanceSchema] Stage '{self.stage}' has status "
                f"'{self.status.value}' but no note. Non-applied "
                f"statuses must include a note explaining why."
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict, omitting note when None."""
        d: Dict[str, Any] = {
            "stage": self.stage,
            "path": self.path,
            "status": self.status.value,
        }
        if self.note is not None:
            d["note"] = self.note
        return d


@dataclass
class ProvenanceChain:
    """
    The full transformation chain for a single payload file.

    Built up incrementally during _copy_payload as each stage runs.
    Once finalised (all four stages added), it is ready to be
    serialised into the v2 JSON document.

    Attributes:
        stages: Ordered list of Stage entries — must contain exactly
                the four stages in STAGE_ORDER once finalised.
    """

    stages: List[Stage] = field(default_factory=list)

    def add(self, stage: Stage) -> None:
        """
        Append a stage to the chain.

        Validates that stages are added in canonical order — adding
        ``package`` before ``eponymous`` is a programming error and
        will raise rather than produce a malformed chain.

        Args:
            stage: The Stage to append.

        Raises:
            ValueError: If the stage is out of order or duplicated.
        """
        expected_index = len(self.stages)
        if expected_index >= len(STAGE_ORDER):
            raise ValueError(
                f"[ProvenanceChain] Cannot add stage '{stage.stage}' — "
                f"chain already has {len(self.stages)} stages "
                f"(maximum {len(STAGE_ORDER)})."
            )

        expected_stage = STAGE_ORDER[expected_index]
        if stage.stage != expected_stage:
            raise ValueError(
                f"[ProvenanceChain] Stage out of order. Expected "
                f"'{expected_stage}' (position {expected_index}), "
                f"got '{stage.stage}'."
            )

        self.stages.append(stage)

    def is_complete(self) -> bool:
        """True if all four canonical stages have been recorded."""
        return len(self.stages) == len(STAGE_ORDER)

    def final_path(self) -> str:
        """
        Return the final (package) path.

        Raises:
            ValueError: If the chain is not yet complete.
        """
        if not self.is_complete():
            raise ValueError(
                f"[ProvenanceChain] Cannot read final_path — chain "
                f"has only {len(self.stages)}/{len(STAGE_ORDER)} stages."
            )
        return self.stages[-1].path

    def source_path(self) -> str:
        """
        Return the original source path.

        Raises:
            ValueError: If the chain has no stages yet.
        """
        if not self.stages:
            raise ValueError(
                "[ProvenanceChain] Cannot read source_path — chain is empty."
            )
        return self.stages[0].path

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return {"stages": [s.to_dict() for s in self.stages]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProvenanceChain":
        """
        Reconstruct a chain from a parsed JSON dict.

        Used by the report renderer to read v2 JSON back in.

        Args:
            data: Dict with a ``stages`` key holding a list of stage
                  dicts.

        Returns:
            A populated ProvenanceChain.

        Raises:
            ValueError: If the dict is malformed.
        """
        if "stages" not in data:
            raise ValueError(
                "[ProvenanceChain] Cannot deserialise — missing 'stages' key."
            )

        chain = cls()
        for s in data["stages"]:
            chain.add(
                Stage(
                    stage=s["stage"],
                    path=s["path"],
                    status=Status(s["status"]),
                    note=s.get("note"),
                )
            )
        return chain


@dataclass
class ProvenanceDocument:
    """
    The full v2 provenance document — top-level container written
    to ``ships.provenance.json`` at the package root.

    Attributes:
        version:      Schema version. Always SCHEMA_VERSION on write;
                      readers MUST refuse mismatched versions.
        generated_at: ISO 8601 UTC timestamp of when the document
                      was written.
        entries:      Map of final package path → ProvenanceChain.
    """

    version: int = SCHEMA_VERSION
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    entries: Dict[str, ProvenanceChain] = field(default_factory=dict)

    def add_chain(self, chain: ProvenanceChain) -> None:
        """
        Add a completed chain to the document, keyed by final path.

        Args:
            chain: A ProvenanceChain that has all four stages recorded.

        Raises:
            ValueError: If the chain is incomplete or its final path
                        collides with an existing entry. On collision,
                        the error message names BOTH source paths so
                        the DBA can immediately see which two files
                        need disambiguating.
        """
        if not chain.is_complete():
            raise ValueError(
                f"[ProvenanceDocument] Refusing to add incomplete "
                f"chain — has {len(chain.stages)}/{len(STAGE_ORDER)} "
                f"stages."
            )

        key = chain.final_path()
        if key in self.entries:
            existing = self.entries[key]
            raise ValueError(
                f"[ProvenanceDocument] Duplicate package path "
                f"'{key}'.\n"
                f"Two source files resolved to the same package "
                f"destination:\n"
                f"\n"
                f"  Source 1: {existing.source_path()}\n"
                f"  Source 2: {chain.source_path()}\n"
                f"\n"
                f"This indicates a builder collision. To fix, either "
                f"rename one of the source files or remove the stale "
                f"copy. A common cause is pre-migration artefacts "
                f"kept alongside their replacements (e.g. _T-prefixed "
                f"files alongside _V-prefixed equivalents)."
            )

        self.entries[key] = chain

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
        }

    def write(self, path: str) -> None:
        """
        Serialise to disk as JSON.

        Args:
            path: File path to write to.
        """
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "ProvenanceDocument":
        """
        Load and validate a v2 provenance document from disk.

        Used by the report renderer.

        Args:
            path: File path to read from.

        Returns:
            A populated ProvenanceDocument.

        Raises:
            ValueError: If the file is missing, malformed, or has a
                        schema version other than SCHEMA_VERSION.
        """
        p = Path(path)
        if not p.exists():
            raise ValueError(f"[ProvenanceDocument] File not found: {path}")

        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)

        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"[ProvenanceDocument] Schema version mismatch in "
                f"{path}. Expected v{SCHEMA_VERSION}, got v{version}. "
                f"This package was built by a different version of "
                f"the builder — rebuild the package or update the "
                f"reader."
            )

        doc = cls(
            version=version,
            generated_at=data.get("generated_at", ""),
            entries={},
        )

        for key, chain_data in data.get("entries", {}).items():
            doc.entries[key] = ProvenanceChain.from_dict(chain_data)

        return doc
