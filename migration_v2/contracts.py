"""Versioned, closed contracts. None of these objects carries an approval claim."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    raw = value if isinstance(value, bytes) else json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ObjectName(Contract):
    parts: tuple[str, ...]

    @field_validator("parts")
    @classmethod
    def valid_parts(cls, value):
        if not 1 <= len(value) <= 2 or any(
            not re.fullmatch(r"[^\W\d]\w*", p, re.UNICODE) or len(p.encode("utf-8")) > 63
            for p in value
        ):
            raise ValueError("Use one or two explicit identifier parts, each at most 63 bytes")
        return value

    @property
    def key(self):
        return ".".join(self.parts)


def sas_object(name: str) -> ObjectName:
    parts = tuple(name.casefold().split("."))
    if len(parts) == 2 and parts[0] == "work":
        parts = parts[1:]
    return ObjectName(parts=parts)


class Column(Contract):
    name: str = Field(min_length=1, max_length=128)
    data_type: str = Field(min_length=1, max_length=128)
    nullable: bool | None = None


class SourceMember(Contract):
    original: str = Field(min_length=1,max_length=128)
    target: ObjectName
    columns: tuple[Column,...] = Field(min_length=1)


class Binding(Contract):
    source_id: str
    decision: Literal["replace", "keep_original"]
    target: ObjectName
    columns: tuple[Column, ...] = ()
    metadata_status: Literal["not_checked", "user_supplied"] = "not_checked"
    unique_keys: tuple[tuple[str, ...], ...] = ()
    members: tuple[SourceMember,...] = ()
    members_complete: bool = False
    schema_complete: bool = False

    @model_validator(mode="after")
    def distinct_columns(self):
        if len({c.name for c in self.columns}) != len(self.columns):
            raise ValueError("Duplicate input column")
        if any(not key or len(key) != len(set(key)) or set(key) - {c.name for c in self.columns} for key in self.unique_keys):
            raise ValueError("Invalid unique key")
        if self.members:
            if not self.members_complete:raise ValueError('Confirm complete ordered source-family membership')
            if len({m.original.casefold() for m in self.members})!=len(self.members):raise ValueError('Duplicate source-family member')
            for member in self.members:
                if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?',member.original) or len({c.name for c in member.columns})!=len(member.columns):raise ValueError('Invalid member name/schema')
        return self


class Output(Contract):
    name: ObjectName
    storage: Literal["temporary", "persistent"]
    columns: tuple[str, ...] = Field(min_length=1)
    role: Literal["final", "intermediate"] = "final"
    column_metadata: tuple[Column, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()
    clickhouse_engine: Literal["MergeTree", "ReplacingMergeTree", "Memory"] | None = None
    clickhouse_nullable_key: bool = False

    @model_validator(mode="after")
    def destination(self):
        if (len(self.name.parts) == 1) != (self.storage == "temporary"):
            raise ValueError("Unqualified/WORK output is temporary; explicit schema is persistent")
        if len(set(self.columns)) != len(self.columns) or any(not c.strip() for c in self.columns):
            raise ValueError("Output columns must be distinct and nonempty")
        names = [column.name for column in self.column_metadata]
        if len(names) != len(set(names)) or set(names) - set(self.columns):
            raise ValueError("Output metadata must identify distinct declared columns")
        if any(not key or len(set(key)) != len(key) or set(key) - set(self.columns) for key in self.unique_keys):
            raise ValueError("Output keys must use distinct declared columns")
        return self


class ReviewResolution(Contract):
    issue_id: str
    rationale: str = Field(min_length=12, max_length=4000)


class ManualInput(Contract):
    origin_source_id: str
    name: ObjectName
    rationale: str = Field(min_length=12, max_length=4000)


class Comparison(Contract):
    output: ObjectName
    reference: ObjectName
    provenance: Literal["synthetic_handwritten", "sas_reference"]
    # SAS provenance is an operator declaration, never supplied by the model.
    reference_run_id: str = Field(min_length=1, max_length=200)
    input_snapshot_hash: str = ""
    source_snapshot_hash: str = ""
    parameters_hash: str = ""

    @field_validator("input_snapshot_hash", "source_snapshot_hash", "parameters_hash")
    @classmethod
    def valid_hash(cls, value):
        if value and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid reference hash")
        return value


class SourceSemantics(Contract):
    numeric_arithmetic: Literal["unconfirmed","exact_decimal_transport","source_binary_float"] = "unconfirmed"
    numeric_missing: Literal["unconfirmed", "ordinary_as_null", "special_tags_lost"] = "unconfirmed"
    character_missing: Literal["unconfirmed", "null_empty_ascii_spaces"] = "unconfirmed"
    character_storage: Literal["unconfirmed", "unpadded_varchar"] = "unconfirmed"
    input_observation_order: Literal["unconfirmed", "explicit_sort_only"] = "unconfirmed"


class Plan(Contract):
    scenario_id: str
    target: Literal["greenplum", "postgresql", "clickhouse"] = "greenplum"
    target_version: str = Field(min_length=1, max_length=64)
    mode: Literal["fixed_run", "reusable_etl"] = "fixed_run"
    acceptance: Literal["data_equivalence", "code_review"] = "data_equivalence"
    targets: tuple[Literal["greenplum", "postgresql", "clickhouse"], ...] = ()
    target_versions: dict[str, str] = Field(default_factory=dict)
    max_model_calls: int | None = Field(default=64, ge=1, le=500)
    max_repair_attempts: int | None = Field(default=2, ge=0, le=2)
    max_run_seconds: int = Field(default=1800, ge=10, le=7200)
    allowed_effects: dict[str, tuple[Literal["create", "insert", "update", "delete", "drop"], ...]] = Field(default_factory=dict)
    scenario_outputs: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    output_sources: dict[str,str] = Field(default_factory=dict)
    output_destinations: dict[str,ObjectName] = Field(default_factory=dict)
    test_mode: bool = False
    intermediate_inputs: tuple[str,...] = ()
    parameters: dict[str, str] = Field(default_factory=dict)
    parameter_types: dict[str, Literal["string", "integer", "decimal", "date", "identifier"]] = Field(default_factory=dict)
    bindings: tuple[Binding, ...] = ()
    outputs: tuple[Output, ...] = Field(min_length=1)
    validation_scope: str = Field(min_length=12, max_length=8000)
    resolutions: tuple[ReviewResolution, ...] = ()
    shared_target_rationale: dict[str, str] = Field(default_factory=dict)
    comparisons: tuple[Comparison, ...] = ()
    source_semantics: SourceSemantics = Field(default_factory=SourceSemantics)

    @model_validator(mode="after")
    def no_duplicates(self):
        if len(self.targets)!=len(set(self.targets)) or set(self.target_versions)-{"greenplum","postgresql","clickhouse"}:
            raise ValueError("Invalid or duplicate target profile")
        if any(not v.strip() or len(v)>64 or any(ord(c)<32 for c in v) for v in self.target_versions.values()):
            raise ValueError("Invalid target version")
        if set(self.allowed_effects)-{o.name.key for o in self.outputs} or any(not v or len(v)!=len(set(v)) for v in self.allowed_effects.values()):
            raise ValueError("Effect policy must identify declared outputs and distinct actions")
        if set(self.output_sources)-{o.name.key for o in self.outputs}:raise ValueError('Unknown output source mapping')
        from .destinations import resolve
        resolve(self)
        for values in ([b.source_id for b in self.bindings], [o.name.key for o in self.outputs],
                       [r.issue_id for r in self.resolutions]):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate contract entry")
        if len({k.casefold() for k in self.parameters}) != len(self.parameters) or any(
            not re.fullmatch(r"[A-Za-z_]\w*", k) or len(v) > 1000 for k, v in self.parameters.items()
        ):
            raise ValueError("Invalid or duplicate macro parameter")
        if not any(o.role == "final" for o in self.outputs):
            raise ValueError("At least one final output is required")
        names = [c.output.key for c in self.comparisons]
        if len(names) != len(set(names)) or set(names) - {o.name.key for o in self.outputs}:
            raise ValueError("Comparison must identify a distinct declared output")
        return self


class SqlArtifact(Contract):
    name: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}\.sql$")
    sql: str = Field(min_length=1, max_length=500000)

    @field_validator("name")
    @classmethod
    def filename(cls, value):
        if ".." in value:
            raise ValueError("Unsafe artifact name")
        return value


class Candidate(Contract):
    schema_version: Literal[1] = 1
    task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[SqlArtifact, ...] = Field(min_length=1, max_length=5000)
    source_claims: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_names(self):
        if len({a.name for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate SQL artifact")
        return self


def parse_json(model, raw: bytes | str):
    """JSON is the wire format; reject duplicate keys and nonfinite constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    json.loads(raw, object_pairs_hook=pairs,
               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    return model.model_validate_json(raw)
