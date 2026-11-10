"""Typed, target-neutral SAS intermediate representation contracts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Literal

Certainty = Literal["deterministic", "needs_metadata", "unsupported", "orchestrator_action"]


def canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SourceSpan:
    line: int
    end_line: int
    offset: int
    end_offset: int


@dataclass(frozen=True)
class DatasetVersion:
    dataset: str
    version: int
    role: Literal["read", "write"]
    producer_step_id: str | None = None
    temporary: bool = False


@dataclass(frozen=True)
class IrStatement:
    id: str
    step_id: str
    kind: str
    certainty: Certainty
    span: SourceSpan
    text: str
    masked: str
    requirements: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    inline_data: bool = False


@dataclass(frozen=True)
class IrStep:
    id: str
    kind: str
    title: str
    parent_id: str | None
    statement_ids: tuple[str, ...]
    children: tuple[str, ...]
    termination: str
    boundary_issue: str
    reads: tuple[DatasetVersion, ...] = ()
    writes: tuple[DatasetVersion, ...] = ()
    by_fields: tuple[str, ...] = ()
    requires_stable_order: bool = False
    effects: tuple[str, ...] = ()
    certainty: Certainty = "deterministic"
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependencyEdge:
    source_step_id: str
    target_step_id: str
    kind: Literal["data", "order", "effect"]
    dataset: str | None = None
    version: int | None = None


@dataclass(frozen=True)
class IrDiagnostic:
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    step_id: str | None = None
    statement_id: str | None = None


@dataclass(frozen=True)
class AnalysisResult:
    schema_version: int
    engine_revision: str
    source_hash: str
    input_hash: str
    catalog_hash: str
    parameter_hash: str
    semantic_policy_hash: str
    semantic_policy: dict
    cutoff: int | None
    catalog: dict
    parameters: dict
    statements: tuple[IrStatement, ...]
    steps: tuple[IrStep, ...]
    dependencies: tuple[DependencyEdge, ...]
    diagnostics: tuple[IrDiagnostic, ...]
    parser_steps: tuple[dict, ...] = field(repr=False)

    def to_dict(self) -> dict:
        result = asdict(self)
        # parser_steps are consumed by the compiler and therefore are part of
        # the approved immutable input, even though they are an internal view.
        result["ir_hash"] = canonical_hash(result)
        return result

