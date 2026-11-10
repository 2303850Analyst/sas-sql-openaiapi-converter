"""Shared SAS semantic contracts used by analysis and lowering.

The module owns facts that must be identical for every target dialect.  It
does not inspect data and never turns an absent contract into an assumption.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SasType = Literal["character", "numeric", "date", "datetime"]
Certainty = Literal["deterministic", "needs_metadata", "unsupported", "orchestrator_action"]


@dataclass(frozen=True)
class SemanticPolicy:
    numeric_missing: Literal["ordinary_null", "encoded_special"] = "ordinary_null"
    character_missing: Literal["blank_or_null"] = "blank_or_null"
    character_length_unit: Literal["bytes"] = "bytes"
    unqualified_output: Literal["temporary"] = "temporary"
    row_order: Literal["explicit_only"] = "explicit_only"
    unknown_behavior: Literal["block"] = "block"

    def requirements_for(self, entry: dict | list | None) -> tuple[str, ...]:
        if not isinstance(entry, dict):
            return ("column_catalog", "column_types", "missing_policy", "collation")
        requirements: list[str] = []
        if not entry.get("column_types"):
            requirements.append("column_types")
        if not entry.get("missing_policy"):
            requirements.append("missing_policy")
        if not entry.get("collation"):
            requirements.append("collation")
        if any(value == "character" for value in entry.get("column_types", {}).values()) and not entry.get("encoding"):
            requirements.append("encoding")
        return tuple(requirements)


DEFAULT_POLICY = SemanticPolicy()


def certainty(requirements: list[str] | tuple[str, ...], *, effect: bool = False, supported: bool = True) -> Certainty:
    if not supported:
        return "unsupported"
    if effect:
        return "orchestrator_action"
    return "needs_metadata" if requirements else "deterministic"
