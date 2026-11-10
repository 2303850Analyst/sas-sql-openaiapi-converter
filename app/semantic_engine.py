"""Target-neutral SAS semantic inventory and typed IR construction.

The engine is intentionally conservative.  It recognizes structure and data
flow without claiming that every statement can be lowered to SQL.  Missing
catalogues, unresolved macros and external effects stay explicit in the IR.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import asdict

from tools.inspect_sources import mask_noncode

from .ir import AnalysisResult, DatasetVersion, DependencyEdge, IrDiagnostic, IrStatement, IrStep, SourceSpan, canonical_hash
from .parameters import expand, validate_parameters
from .sas_blocks import partition, statement_id, walk
from .sas_lexer import lex_statements
from .schema_flow import validate_catalog

ENGINE_REVISION = "sas-ir-3"
TABLE = r'(?:[A-Za-z_\u0080-\uffff]\w*\.)?[A-Za-z_\u0080-\uffff]\w*'
EFFECT_PATTERNS = (
    (r'^libname\b', "library_binding"),
    (r'^filename\b', "file_binding"),
    (r'^proc\s+http\b', "http_request"),
    (r'^proc\s+(?:import|export)\b', "file_transfer"),
    (r'^(?:infile|file)\b', "file_io"),
    (r'^(?:x\b|systask\b)', "operating_system"),
    (r'^endsas\b', "session_stop"),
)


def _hash_bytes(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dataset_name(value: str) -> str:
    value = re.sub(r'\s*\(.*\)\s*$', '', value.strip(), flags=re.S)
    name = value.casefold()
    return name[5:] if name.startswith("work.") else name


def _dataset_parts(value: str) -> tuple[str, bool]:
    name = _dataset_name(value)
    return name, "." not in name or name.startswith("work.")


def _simple_names(text: str) -> list[str]:
    return re.findall(TABLE, re.sub(r'\([^()]*(?:\([^()]*\)[^()]*)*\)', ' ', text), re.I)


def _statement_kind(masked: str, owner: dict) -> tuple[str, str, list[str]]:
    text = masked.strip()
    requirements: list[str] = []
    if re.fullmatch(r'(?:run(?:\s+cancel)?|quit)\s*;', text, re.I):
        return "boundary", "deterministic", requirements
    if re.match(r'%macro\b', text, re.I):
        return "macro_definition", "deterministic", requirements
    if re.match(r'%mend\b', text, re.I):
        return "macro_end", "deterministic", requirements
    if text.startswith('%'):
        if re.match(r'%(?:let|local|global)\b', text, re.I):
            requirements.append("macro_scope")
            return "macro_state", "needs_metadata", requirements
        requirements.append("macro_expansion")
        return "macro_call", "needs_metadata", requirements
    if re.match(r'proc\s+sql\b', text, re.I):
        return "proc_header", "deterministic", requirements
    if re.match(r'proc\b', text, re.I):
        return "procedure_header", "deterministic", requirements
    if re.match(r'data\b', text, re.I):
        return "data_header", "deterministic", requirements
    if owner.get("kind") == "proc_sql" and re.match(r'(?:select|create\s+table|insert|update|delete)\b', text, re.I):
        if re.search(r'[%&]', text):
            requirements.append("resolved_macro_values")
            return "query", "needs_metadata", requirements
        return "query", "deterministic", requirements
    for keyword, kind in (("set", "set"), ("merge", "merge"), ("by", "by"), ("where", "where"), ("if", "condition"), ("else", "condition"), ("retain", "retain"), ("output", "output"), ("array", "array"), ("format", "attribute"), ("informat", "attribute"), ("length", "attribute"), ("label", "attribute"), ("keep", "projection"), ("drop", "projection"), ("rename", "projection")):
        if re.match(rf'{keyword}\b', text, re.I):
            if kind in {"merge", "retain", "array", "output"}:
                requirements.append({"merge": "pdv_and_by_cardinality", "retain": "row_state_and_order", "array": "array_domain", "output": "output_cardinality"}[kind])
                return kind, "needs_metadata", requirements
            return kind, "deterministic", requirements
    if owner.get("kind") == "data" and re.match(r'[A-Za-z_\u0080-\uffff]\w*(?:\s*\([^)]*\))?\s*(?:=|\+)', text, re.I):
        return "assignment", "deterministic", requirements
    if owner.get("kind") in {"data", "procedure", "proc_sql"}:
        requirements.append("dedicated_statement_rule")
        return "unknown_statement", "unsupported", requirements
    if re.search(r'[%&]', text):
        requirements.append("macro_expansion")
        return "macro_call", "needs_metadata", requirements
    return "global_statement", "orchestrator_action", ["orchestrator_policy"]


def _effects(masked: str) -> list[str]:
    return [effect for pattern, effect in EFFECT_PATTERNS if re.search(pattern, masked.strip(), re.I)]


def _node_io(node: dict, items_by_id: dict[str, dict]) -> tuple[list[str], list[str]]:
    items = [items_by_id[item_id] for item_id in node["operation_ids"] if item_id in items_by_id]
    reads: list[str] = []
    writes: list[str] = []
    if node["kind"] == "data" and items:
        head = re.match(r'\s*data\s+(.+?);', items[0]["masked"], re.I | re.S)
        if head:
            for value in _simple_names(head.group(1)):
                if value.casefold() not in {"data", "view", "debug", "nested"} and value.casefold() != "_null_":
                    writes.append(value)
        for item in items[1:]:
            set_match = re.match(r'\s*(?:set|merge)\s+(.+?);', item["masked"], re.I | re.S)
            if set_match:
                for value in _simple_names(set_match.group(1)):
                    if value.casefold() not in {"end", "key", "indsname", "open", "uniqueout"}:
                        reads.append(value)
    elif node["kind"] == "proc_sql":
        for item in items:
            text = item["masked"]
            create = re.search(r'\bcreate\s+table\s+(' + TABLE + r')', text, re.I)
            if create:
                writes.append(create.group(1))
            for match in re.finditer(r'\b(?:from|join)\s+(' + TABLE + r')', text, re.I):
                reads.append(match.group(1))
            insert = re.search(r'\binsert\s+into\s+(' + TABLE + r')', text, re.I)
            update = re.search(r'\bupdate\s+(' + TABLE + r')', text, re.I)
            delete = re.search(r'\bdelete\s+from\s+(' + TABLE + r')', text, re.I)
            for match in (insert, update, delete):
                if match:
                    writes.append(match.group(1))
    elif node.get("procedure") in {"sort", "append", "transpose", "summary", "means"} and items:
        head = items[0]["masked"]
        source = re.search(r'\bdata\s*=\s*(' + TABLE + r')', head, re.I)
        if node.get("procedure") == "append":
            output = re.search(r'\bbase\s*=\s*(' + TABLE + r')', head, re.I)
        else:
            output = re.search(r'\bout\s*=\s*(' + TABLE + r')', head, re.I)
            if not output:
                output = next((re.search(r'\bout\s*=\s*(' + TABLE + r')', item["masked"], re.I) for item in items if re.search(r'\bout\s*=', item["masked"], re.I)), None)
        if source:
            reads.append(source.group(1))
            if output:
                writes.append(output.group(1))
            elif node.get("procedure") == "sort":
                writes.append(source.group(1))
    return list(dict.fromkeys(map(_dataset_name, reads))), list(dict.fromkeys(map(_dataset_name, writes)))


def _by_fields(node: dict, items_by_id: dict[str, dict]) -> list[str]:
    result: list[str] = []
    for item_id in node["operation_ids"]:
        item = items_by_id.get(item_id)
        if not item:
            continue
        match = re.match(r'\s*by\s+(.+?);', item["masked"], re.I | re.S)
        if match:
            tokens = re.findall(r'[A-Za-z_\u0080-\uffff]\w*', match.group(1))
            result.extend(token for token in tokens if token.casefold() not in {"descending", "notsorted", "groupformat"})
    return list(dict.fromkeys(value.casefold() for value in result))


class SasSemanticEngine:
    def analyze(self, source: str, source_catalog: dict | None = None, parameters: dict | None = None) -> dict:
        catalog = validate_catalog(source_catalog or {})
        bindings = validate_parameters(parameters or {})
        diagnostics: list[IrDiagnostic] = []
        mutable_macro = bool(re.search(r'%\s*(?:let|global|local|include|syscall)\b|\b(?:symputx?|dosubl|execute)\s*\(', source, re.I))
        effective_bindings = {} if mutable_macro else bindings
        if bindings and mutable_macro:
            diagnostics.append(IrDiagnostic("warning", "mutable_macro_state", "Внешние параметры не подставлены: программа изменяет макросостояние"))

        items, cutoff = lex_statements(source)
        for item in items:
            if effective_bindings:
                item["original_text"] = item["text"]
                item["text"] = expand(item["text"], effective_bindings)
                item["masked"] = mask_noncode(item["text"], sas=True)
        parser_steps = partition(items, cutoff)
        owners = {operation_id: node for node in walk(parser_steps) for operation_id in node["operation_ids"]}
        item_by_id = {statement_id(index, item): item for index, item in enumerate(items)}
        nodes_by_id = {node["id"]: node for node in walk(parser_steps)}
        from .macro_scope import blocked_statements
        blocked_flags = blocked_statements(items)
        macro_blocked_steps = {owners[statement_id(index, item)]["id"] for index, item in enumerate(items) if blocked_flags[index]}

        def inside_macro(node: dict) -> bool:
            parent_id = node.get("parent_id")
            while parent_id:
                parent = nodes_by_id[parent_id]
                if parent["kind"] == "macro":
                    return True
                parent_id = parent.get("parent_id")
            return False

        statements: list[IrStatement] = []
        for index, item in enumerate(items):
            ident = statement_id(index, item)
            owner = owners[ident]
            kind, certainty, requirements = _statement_kind(item["masked"], owner)
            effects = _effects(item["masked"])
            if effects and certainty == "deterministic":
                certainty = "orchestrator_action"
            statements.append(
                IrStatement(
                    ident,
                    owner["id"],
                    kind,
                    certainty,
                    SourceSpan(item["line"], item["end_line"], item["offset"], item["end_offset"]),
                    item["text"],
                    item["masked"],
                    tuple(requirements),
                    tuple(effects),
                    bool(item.get("inline_data")),
                )
            )

        statement_map = {statement.id: statement for statement in statements}
        versions: dict[str, int] = {}
        producers: dict[tuple[str, int], str] = {}
        dependencies: list[DependencyEdge] = []
        ir_steps: list[IrStep] = []
        last_effect_step: str | None = None
        for node in walk(parser_steps):
            reads, writes = _node_io(node, item_by_id)
            template_scope = inside_macro(node) or node["id"] in macro_blocked_steps
            read_versions: list[DatasetVersion] = []
            write_versions: list[DatasetVersion] = []
            requirements: list[str] = []
            if template_scope:
                requirements.append("macro_invocation_context")
            for dataset in reads:
                version = versions.get(dataset, 0)
                producer = producers.get((dataset, version))
                read_versions.append(DatasetVersion(dataset, version, "read", producer, _dataset_parts(dataset)[1]))
                if not template_scope and producer and producer != node["id"]:
                    dependencies.append(DependencyEdge(producer, node["id"], "data", dataset, version))
                contract = catalog.get(dataset, catalog.get("work." + dataset) if "." not in dataset else None)
                if contract is None:
                    requirements.append(f"catalog:{dataset}")
                else:
                    from .semantic_policy import DEFAULT_POLICY
                    requirements.extend(DEFAULT_POLICY.requirements_for(contract))
            for dataset in writes:
                version = versions.get(dataset, 0) + 1 if not template_scope else 0
                if not template_scope:
                    versions[dataset] = version
                    producers[(dataset, version)] = node["id"]
                write_versions.append(DatasetVersion(dataset, version, "write", node["id"] if not template_scope else None, _dataset_parts(dataset)[1]))
            if set(reads) & set(writes):
                requirements.append("versioned_self_write")

            by = _by_fields(node, item_by_id)
            owned = [statement_map[item_id] for item_id in node["operation_ids"] if item_id in statement_map]
            flags = any(re.search(r'\b(?:first|last)\s*\.', statement.masked, re.I) for statement in owned)
            requires_order = bool(by and (flags or node.get("procedure") == "sort" or any(statement.kind in {"retain", "merge"} for statement in owned)))
            if requires_order:
                for read in read_versions:
                    contract = catalog.get(read.dataset, catalog.get("work." + read.dataset, {}))
                    if not isinstance(contract, dict) or not contract.get("order_by") or not contract.get("unique_order"):
                        requirements.append(f"stable_order:{read.dataset}")
                    producer = read.producer_step_id
                    if producer:
                        dependencies.append(DependencyEdge(producer, node["id"], "order", read.dataset, read.version))

            effects = tuple(dict.fromkeys(effect for statement in owned for effect in statement.effects))
            if effects and last_effect_step and last_effect_step != node["id"]:
                dependencies.append(DependencyEdge(last_effect_step, node["id"], "effect"))
            if effects:
                last_effect_step = node["id"]
            certainties = {statement.certainty for statement in owned}
            if template_scope:
                certainty = "needs_metadata"
            elif node["boundary_issue"] or "unsupported" in certainties:
                certainty = "unsupported"
            elif effects or "orchestrator_action" in certainties:
                certainty = "orchestrator_action"
            elif requirements or "needs_metadata" in certainties:
                certainty = "needs_metadata"
            else:
                certainty = "deterministic"
            if node["boundary_issue"]:
                requirements.append("complete_step_boundary")
                diagnostics.append(IrDiagnostic("error", "boundary_issue", node["boundary_issue"], node["id"]))
            ir_steps.append(
                IrStep(
                    node["id"],
                    node["kind"],
                    node["title"],
                    node.get("parent_id"),
                    tuple(node["operation_ids"]),
                    tuple(child["id"] for child in node["children"]),
                    node["termination"],
                    node["boundary_issue"],
                    tuple(read_versions),
                    tuple(write_versions),
                    tuple(by),
                    requires_order,
                    effects,
                    certainty,
                    tuple(dict.fromkeys(requirements)),
                )
            )
        if cutoff is not None:
            diagnostics.append(IrDiagnostic("error", "lexical_cutoff", "Разбор остановлен; хвост источника не интерпретирован"))
        for statement in statements:
            if statement.certainty in {"unsupported", "needs_metadata"}:
                diagnostics.append(IrDiagnostic("warning" if statement.certainty == "needs_metadata" else "error", statement.certainty, ", ".join(statement.requirements) or "Нет правила", statement.step_id, statement.id))

        catalog_hash = canonical_hash(catalog)
        parameter_hash = canonical_hash(bindings)
        from .semantic_policy import DEFAULT_POLICY
        policy = asdict(DEFAULT_POLICY)
        policy_hash = canonical_hash(policy)
        result = AnalysisResult(
            2,
            ENGINE_REVISION,
            _hash_bytes(source),
            canonical_hash({"source": _hash_bytes(source), "catalog": catalog_hash, "parameters": parameter_hash, "semantic_policy": policy_hash}),
            catalog_hash,
            parameter_hash,
            policy_hash,
            policy,
            cutoff,
            catalog,
            bindings,
            tuple(statements),
            tuple(ir_steps),
            tuple(dependencies),
            tuple(diagnostics),
            tuple(copy.deepcopy(parser_steps)),
        )
        return result.to_dict()


def compiler_items(ir: dict) -> tuple[list[dict], int | None, list[dict]]:
    from .sas_lexer import without_block_comments
    items = []
    for statement in ir.get("statements", []):
        span = statement["span"]
        items.append(
            {
                "id": statement["id"],
                "text": without_block_comments(statement["text"]),
                "masked": statement["masked"],
                "line": span["line"],
                "end_line": span["end_line"],
                "offset": span["offset"],
                "end_offset": span["end_offset"],
                "inline_data": statement.get("inline_data", False),
            }
        )
    return items, ir.get("cutoff"), list(copy.deepcopy(ir.get("parser_steps", [])))

