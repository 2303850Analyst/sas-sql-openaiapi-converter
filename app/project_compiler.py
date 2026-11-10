"""Project-wide SAS compilation with shared macro and dataset-version state.

The external interface has two operations: ``analyze`` returns the immutable
semantic plan for Gate 1, and ``compile`` lowers exactly that plan.  Existing
DATA/PROC handlers remain internal implementation details.

LIBNAME mapping is deliberately deferred by the current product decision.  A
LIBNAME statement is retained as an explicit plan requirement and is never
used to guess a database or schema.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from .compiler import DIALECTS, VERSION
from .compiler_pipeline import CompilationRequest, DEFAULT_PIPELINE
from .embedded_rules import embedded_rule_pack
from .ir import canonical_hash
from .macro_processor import ExpandedProgram, MacroProcessor
from .parameters import validate_parameters
from .schema_flow import observe, validate_catalog
from .semantic_engine import SasSemanticEngine


PROJECT_COMPILER_REVISION = "sas-project-compiler-1.2"


@dataclass(frozen=True)
class ProgramRequest:
    id: str
    name: str
    source: str
    source_hash: str = ""
    source_catalog: dict = field(default_factory=dict)
    format_catalog: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    edits: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectCompilationRequest:
    programs: tuple[ProgramRequest, ...]
    target: str
    target_version: str = ""
    date_storage: str = "unknown"
    run_date: date | None = None
    timezone: str = "Europe/Moscow"
    catalog_complete: bool = False


def _dataset(value: str) -> str:
    value = value.casefold().strip()
    return value[5:] if value.startswith("work.") else value


def _physical_stage(dataset: str, version: int) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", dataset.casefold()).strip("_") or "table"
    digest = hashlib.sha256(dataset.encode("utf-8")).hexdigest()[:8]
    return f"__sasv_{base[-30:]}_{digest}_v{version}"


def _contract(catalog: dict, dataset: str):
    return catalog.get(dataset) or (catalog.get("work." + dataset) if "." not in dataset else None)


def _merge_catalogs(programs: tuple[ProgramRequest, ...]) -> dict:
    result: dict = {}
    for program in programs:
        current = validate_catalog(program.source_catalog or {})
        for name, entry in current.items():
            canonical = _dataset(name)
            existing = _contract(result, canonical)
            if existing is not None and existing != entry:
                raise ValueError(f"Каталог содержит разные контракты одной таблицы: {canonical}")
            result[canonical] = entry
            if "." not in canonical:
                result["work." + canonical] = entry
    return result


def _public_requirement(code: str, message: str, program_id: str, line: int, *, blocking: bool, step_id: str = "", macro: str = "") -> dict:
    return {
        "id": "req-" + hashlib.sha256(f"{code}|{program_id}|{line}|{step_id}|{macro}|{message}".encode("utf-8")).hexdigest()[:16],
        "code": code,
        "message": message,
        "program_id": program_id,
        "line": line,
        "step_id": step_id,
        "macro": macro,
        "blocking": blocking,
        "status": "open",
    }


def _requirement_groups(requirements: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for item in requirements:
        key = (item["code"], item["message"], item.get("macro", ""), item["blocking"])
        grouped.setdefault(key, []).append(item)
    result = []
    for (code, message, macro, blocking), items in grouped.items():
        programs = list(dict.fromkeys(item["program_id"] for item in items))
        occurrences = [{"program_id": item["program_id"], "line": item["line"], "step_id": item.get("step_id", "")} for item in items]
        result.append({
            "id": "req-group-" + hashlib.sha256(f"{code}|{macro}|{message}".encode("utf-8")).hexdigest()[:16],
            "code": code,
            "message": message,
            "macro": macro,
            "blocking": blocking,
            "count": len(items),
            "program_ids": programs,
            "occurrences": occurrences[:20],
            "occurrences_truncated": len(occurrences) > 20,
            "status": "open",
        })
    return sorted(result, key=lambda item: (not item["blocking"], item["code"], item["macro"], item["id"]))


class ProjectCompiler:
    @staticmethod
    def _execution_units(programs: list[dict]):
        """Ordered effects; PROC SQL can write the same table more than once."""
        from .semantic_engine import _node_io

        for program in programs:
            statements = {item["id"]: item for item in program["ir"]["statements"]}
            for step in program["ir"]["steps"]:
                if step["kind"] == "proc_sql":
                    for statement_id in step["statement_ids"]:
                        statement = statements[statement_id]
                        reads, writes = _node_io(
                            {"kind": "proc_sql", "operation_ids": [statement_id]},
                            {statement_id: {"masked": statement["masked"]}},
                        )
                        if reads or writes:
                            yield program, step, statement_id, reads, writes
                else:
                    yield program, step, step["statement_ids"][0] if step["statement_ids"] else "", \
                        [item["dataset"] for item in step["reads"]], \
                        [item["dataset"] for item in step["writes"]]

    def analyze(self, request: ProjectCompilationRequest) -> dict:
        if request.target not in DIALECTS:
            raise ValueError("Неизвестная целевая СУБД")
        catalog = _merge_catalogs(request.programs)
        processor = MacroProcessor(
            run_date=request.run_date,
            catalog=catalog,
            catalog_complete=request.catalog_complete,
        )
        expansions: list[ExpandedProgram] = []
        program_plans: list[dict] = []
        requirements: list[dict] = []

        for program in request.programs:
            parameters = validate_parameters(program.parameters or {})
            expansion = processor.expand_program(program.id, program.source, parameters)
            expansions.append(expansion)
            ir = SasSemanticEngine().analyze(expansion.source, catalog, {})
            for requirement in expansion.requirements:
                requirements.append(_public_requirement(
                    requirement["code"], requirement["message"], program.id, requirement["line"],
                    blocking=requirement["blocking"], macro=requirement.get("macro", ""),
                ))
            for statement in ir["statements"]:
                if statement.get("effects") and "library_binding" in statement["effects"]:
                    requirements.append(_public_requirement(
                        "library_mapping_deferred",
                        "LIBNAME сохранён в плане; разрешение библиотеки исключено из этой доработки по решению пользователя",
                        program.id,
                        statement["span"]["line"],
                        blocking=False,
                        step_id=statement["step_id"],
                    ))
            if ir.get("cutoff") is not None:
                requirements.append(_public_requirement(
                    "lexical_cutoff", "Неразобранный хвост программы может содержать исполняемую логику",
                    program.id, ir["cutoff"], blocking=True,
                ))
            program_plans.append({
                "id": program.id,
                "name": program.name,
                "source_hash": program.source_hash or hashlib.sha256(program.source.encode("utf-8")).hexdigest(),
                "expanded_hash": expansion.expanded_hash,
                "macro_requirements": list(expansion.requirements),
                "macro_trace": list(expansion.trace),
                "macro_source_map": list(expansion.source_map),
                "ir": ir,
            })

        physical, dataset_versions, version_requirements = self._physical_plan(program_plans)
        requirements.extend(version_requirements)
        for program in program_plans:
            program["physical_plan"] = physical[program["id"]]

        requirements = list({item["id"]: item for item in requirements}.values())
        blocking = [item for item in requirements if item["blocking"]]
        static_unsupported = any(
            step["certainty"] == "unsupported"
            for program in program_plans for step in program["ir"]["steps"]
        )
        context = {
            "target": request.target,
            "target_version": request.target_version,
            "date_storage": request.date_storage,
            "run_date": request.run_date.isoformat() if request.run_date else None,
            "timezone": request.timezone,
            "catalog_complete": request.catalog_complete,
            "catalog_hash": canonical_hash(catalog),
            "program_order": [program.id for program in request.programs],
            "macro_limits": {"expanded_statements": 20_000, "iterations": 1_000, "call_depth": 30},
        }
        plan = {
            "schema_version": 1,
            "compiler_revision": PROJECT_COMPILER_REVISION,
            "lowerer_revision": VERSION,
            "rule_pack_id": embedded_rule_pack()["rule_pack_id"],
            "context": context,
            "context_hash": canonical_hash(context),
            "macro_state_hash": processor.state_hash(),
            "macro_symbols": processor.public_symbols(),
            "programs": program_plans,
            "dataset_versions": dataset_versions,
            "requirements": requirements,
            "requirement_groups": _requirement_groups(requirements),
            "completeness": "partial" if blocking or static_unsupported else "complete",
            "library_mapping": "deferred_by_user",
            "database_execution": "not_run",
            "comparison": "not_run",
        }
        plan["plan_hash"] = canonical_hash(plan)
        return plan

    def compile(self, request: ProjectCompilationRequest, plan: dict) -> dict:
        expected = self.analyze(request)
        if expected["plan_hash"] != plan.get("plan_hash"):
            raise ValueError("Проектный план изменился; повторите Gate 1")
        catalog = _merge_catalogs(request.programs)
        expansions = self._expand(request, catalog)
        results: list[dict] = []
        global_step_status: dict[str, str] = {}
        version_available: dict[tuple[str, int], bool] = {}
        unresolved_macro_context = False
        plan_programs = {program["id"]: program for program in plan["programs"]}

        for source_program, expansion in zip(request.programs, expansions):
            program_plan = plan_programs[source_program.id]
            aliases = self._catalog_aliases(catalog, program_plan["physical_plan"])
            result = DEFAULT_PIPELINE.compile(CompilationRequest(
                source=expansion.source,
                target=request.target,
                edits=source_program.edits,
                date_storage=request.date_storage,
                source_catalog=aliases,
                format_catalog=source_program.format_catalog,
                parameters={},
                semantic_ir=program_plan["ir"],
                physical_plan=program_plan["physical_plan"],
            ))
            result["program_id"] = source_program.id
            result["program_name"] = source_program.name
            result["expanded_hash"] = expansion.expanded_hash
            result["macro_requirements"] = list(expansion.requirements)
            result["macro_trace"] = list(expansion.trace)
            result["macro_source_map"] = list(expansion.source_map)
            # Unknown generated code can change any later table dependency.
            # A known error-policy macro is different: retain independent SQL
            # fragments while reporting its unresolved orchestration effect.
            unresolved_macro_context = unresolved_macro_context or any(
                requirement.get("code") in {
                    "macro_unknown", "macro_loop_bounds",
                    "macro_loop_condition", "macro_source_cutoff",
                } for requirement in expansion.requirements
            )
            if unresolved_macro_context:
                for operation in result["operations"]:
                    if operation["status"] == "candidate":
                        operation.update(
                            status="unsupported",
                            reason="Макроконтекст сценария не разрешён; выбор таблиц и значений не доказан",
                        )
                        operation.pop("sql", None)
                        operation.pop("original_sql", None)
            self._propagate_cross_program_blocks(result, source_program.id, plan, global_step_status)
            self._enforce_version_dependencies(result, source_program.id, plan, version_available)
            self._update_catalog(catalog, result, request.target)
            candidates_by_id = {operation["id"]: operation for operation in result["candidates"]}
            for step in program_plan["ir"]["steps"]:
                expected = [event for event in plan["dataset_versions"]
                            if event["program_id"] == source_program.id
                            and event["step_id"] == step["id"] and event["role"] == "write"]
                blocked = any(
                    event["operation_id"] not in candidates_by_id or
                    _dataset(candidates_by_id[event["operation_id"]].get("logical_output") or "") != event["dataset"]
                    for event in expected
                )
                global_step_status[f"{source_program.id}::{step['id']}"] = "blocked" if blocked else "available"
            results.append(result)

        build = {
            "schema_version": 1,
            "compiler_revision": PROJECT_COMPILER_REVISION,
            "lowerer_revision": VERSION,
            "plan_hash": plan["plan_hash"],
            "context_hash": plan["context_hash"],
            "rule_pack_id": plan["rule_pack_id"],
            "target": request.target,
            "target_version": request.target_version,
            "programs": results,
            "requirements": plan["requirements"],
            "requirement_groups": plan["requirement_groups"],
            "completeness": "partial" if plan["completeness"] == "partial" or any(
                program["unsupported"] for program in results
            ) else "complete",
            "execution": "not_run",
            "comparison": "not_run",
            "status": "unverified_draft",
        }
        build["build_hash"] = canonical_hash(build)
        return build

    def _expand(self, request: ProjectCompilationRequest, catalog: dict) -> list[ExpandedProgram]:
        processor = MacroProcessor(run_date=request.run_date, catalog=catalog, catalog_complete=request.catalog_complete)
        return [processor.expand_program(program.id, program.source, validate_parameters(program.parameters or {})) for program in request.programs]

    def _physical_plan(self, programs: list[dict]):
        totals: dict[str, int] = {}
        reads_version_zero: set[str] = set()
        versions: dict[str, int] = {}
        for program, step, operation_id, reads, writes in self._execution_units(programs):
                for read in reads:
                    dataset = _dataset(read)
                    if versions.get(dataset, 0) == 0:
                        reads_version_zero.add(dataset)
                for write in writes:
                    dataset = _dataset(write)
                    totals[dataset] = totals.get(dataset, 0) + 1
                    versions[dataset] = versions.get(dataset, 0) + 1

        physical: dict[str, dict] = {program["id"]: {} for program in programs}
        dataset_versions: list[dict] = []
        requirements: list[dict] = []
        versions.clear()
        producers: dict[tuple[str, int], str] = {}
        physical_versions: dict[tuple[str, int], str] = {}
        for program, step, operation_id, reads, writes in self._execution_units(programs):
                global_step = f"{program['id']}::{step['id']}"
                read_map: dict[str, str] = {}
                write_map: dict[str, str] = {}
                for read in reads:
                    dataset = _dataset(read)
                    version = versions.get(dataset, 0)
                    physical_name = dataset if version == 0 else physical_versions[(dataset, version)]
                    read_map[dataset] = physical_name
                    dataset_versions.append({
                        "program_id": program["id"], "step_id": step["id"], "global_step_id": global_step,
                        "operation_id": operation_id,
                        "dataset": dataset, "version": version, "role": "read", "physical_name": physical_name,
                        "producer_step_id": producers.get((dataset, version)),
                    })
                for write in writes:
                    dataset = _dataset(write)
                    version = versions.get(dataset, 0) + 1
                    versions[dataset] = version
                    needs_staging = totals.get(dataset, 0) > 1 or dataset in read_map
                    final_can_use_logical = version == totals.get(dataset, version) and dataset not in reads_version_zero
                    physical_name = dataset if not needs_staging or final_can_use_logical else _physical_stage(dataset, version)
                    physical_versions[(dataset, version)] = physical_name
                    producers[(dataset, version)] = global_step
                    write_map[dataset] = physical_name
                    dataset_versions.append({
                        "program_id": program["id"], "step_id": step["id"], "global_step_id": global_step,
                        "operation_id": operation_id,
                        "dataset": dataset, "version": version, "role": "write", "physical_name": physical_name,
                        "producer_step_id": global_step,
                    })
                    if version == totals.get(dataset, version) and physical_name != dataset:
                        requirements.append(_public_requirement(
                            "publication_policy_required",
                            "Финальная версия читает существующий объект; автоматическая замена запрещена без политики публикации",
                            program["id"], 0, blocking=True, step_id=step["id"],
                        ))
                if read_map or write_map:
                    if step["kind"] == "proc_sql":
                        physical[program["id"]].setdefault(step["id"], {"operations": {}})["operations"][operation_id] = {
                            "reads": read_map, "writes": write_map,
                        }
                    else:
                        physical[program["id"]][step["id"]] = {"reads": read_map, "writes": write_map}
        return physical, dataset_versions, requirements

    @staticmethod
    def _catalog_aliases(catalog: dict, physical_plan: dict) -> dict:
        result = dict(catalog)
        for mapping in physical_plan.values():
            for unit in ([mapping] if not mapping.get("operations") else mapping["operations"].values()):
                for logical, physical in unit.get("reads", {}).items():
                    entry = _contract(catalog, logical)
                    if entry is not None:
                        result[physical.casefold()] = entry
                        if "." not in physical:
                            result["work." + physical.casefold()] = entry
        # validate_catalog caps external user input at 200 tables. Internal
        # aliases are deterministic and can exceed that cap in a large EGP, so
        # CompilationRequest receives only aliases needed by this program.
        if len(result) > 200:
            used = {name for mapping in physical_plan.values()
                    for unit in ([mapping] if not mapping.get("operations") else mapping["operations"].values())
                    for name in unit.get("reads", {}).values()}
            compact = {name: entry for name, entry in result.items() if name in used or not name.startswith("work.__sasv_")}
            result = compact
        return result

    @staticmethod
    def _update_catalog(catalog: dict, result: dict, target: str) -> None:
        for operation in result["candidates"]:
            try:
                observe(operation, catalog, DIALECTS[target])
            except Exception:
                continue

    @staticmethod
    def _enforce_version_dependencies(result: dict, program_id: str, plan: dict,
                                      available: dict[tuple[str, int], bool]) -> None:
        operations = {operation["id"]: operation for operation in result["operations"]}
        for event in plan["dataset_versions"]:
            if event["program_id"] != program_id:
                continue
            operation = operations.get(event["operation_id"])
            key = (event["dataset"], event["version"])
            if event["role"] == "read" and event["version"] > 0:
                if operation and operation["status"] == "candidate" and not available.get(key, False):
                    operation.update(
                        status="unsupported",
                        reason="Входная версия не создана: предыдущая запись не преобразована",
                    )
                    operation.pop("sql", None)
                    operation.pop("original_sql", None)
            elif event["role"] == "write":
                available[key] = bool(
                    operation and operation["status"] == "candidate" and
                    _dataset(operation.get("logical_output") or "") == event["dataset"]
                )
        result["candidates"] = [operation for operation in result["operations"] if operation["status"] == "candidate"]
        result["unsupported"] = [
            {"id": operation["id"], "line": operation["line"], "reason": operation["reason"]}
            for operation in result["operations"] if operation["status"] == "unsupported"
        ]

    @staticmethod
    def _propagate_cross_program_blocks(result: dict, program_id: str, plan: dict, statuses: dict[str, str]) -> None:
        plan_program = next(program for program in plan["programs"] if program["id"] == program_id)
        reads_by_step = {
            step["id"]: [
                version for version in plan["dataset_versions"]
                if version["program_id"] == program_id and version["step_id"] == step["id"] and version["role"] == "read"
            ]
            for step in plan_program["ir"]["steps"]
        }
        for operation in result["operations"]:
            if operation["status"] != "candidate":
                continue
            unavailable = [
                value for value in reads_by_step.get(operation["step_id"], [])
                if value.get("producer_step_id") and statuses.get(value["producer_step_id"]) == "blocked"
            ]
            if unavailable:
                operation.update(
                    status="unsupported",
                    reason="Предыдущий шаг выбранного EGP-сценария не преобразован; входная версия недоступна",
                    blocked_by=[value["producer_step_id"] for value in unavailable],
                )
                operation.pop("sql", None)
                operation.pop("original_sql", None)
        result["candidates"] = [operation for operation in result["operations"] if operation["status"] == "candidate"]
        result["unsupported"] = [
            {"id": operation["id"], "line": operation["line"], "reason": operation["reason"],
             "blocked_by": operation.get("blocked_by", [])}
            for operation in result["operations"] if operation["status"] == "unsupported"
        ]


DEFAULT_PROJECT_COMPILER = ProjectCompiler()

