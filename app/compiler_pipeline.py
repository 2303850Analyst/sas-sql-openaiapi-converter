"""Single compiler interface: source/context in, deterministic result out."""
from __future__ import annotations

from dataclasses import dataclass, field

from .embedded_rules import embedded_rule_pack
from .semantic_engine import SasSemanticEngine
from .target_adapters import adapter_for


@dataclass(frozen=True)
class CompilationRequest:
    source: str
    target: str
    edits: dict = field(default_factory=dict)
    date_storage: str = "unknown"
    source_catalog: dict = field(default_factory=dict)
    format_catalog: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    semantic_ir: dict | None = None
    physical_plan: dict = field(default_factory=dict)


class CompilerPipeline:
    def compile(self, request: CompilationRequest) -> dict:
        adapter = adapter_for(request.target)
        ir = request.semantic_ir or SasSemanticEngine().analyze(request.source, request.source_catalog, request.parameters)
        from .compiler import VERSION, analyze
        result = analyze(
            request.source,
            request.target,
            request.edits,
            request.date_storage,
            request.source_catalog,
            request.format_catalog,
            request.parameters,
            ir,
            request.physical_plan,
        )
        pack = embedded_rule_pack()
        result["compiler_contract"] = {
            "rule_pack_id": pack["rule_pack_id"],
            "compiler_revision": VERSION,
            "rule_pack_compiler_revision": pack["compiler_revision"],
            "target": request.target,
            "dialect": adapter.capabilities.dialect,
            "ir_hash": ir["ir_hash"],
            "database_execution": "not_run",
        }
        return result


DEFAULT_PIPELINE = CompilerPipeline()
