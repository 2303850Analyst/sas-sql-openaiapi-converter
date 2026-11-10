from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import tempfile
import uuid
import zipfile
from datetime import date
from pathlib import Path

import sqlglot
from sqlglot import exp
from tools.inspect_sources import LIMIT, LexicalError, decode_source, features, inspect_egp, mask_noncode

from .compiler import VERSION as RULE_VERSION
from .project_compiler import (
    DEFAULT_PROJECT_COMPILER,
    PROJECT_COMPILER_REVISION,
    ProgramRequest,
    ProjectCompilationRequest,
)
TARGETS = {"postgresql": "postgres", "greenplum": "postgres", "clickhouse": "clickhouse"}
INTAKE_REVISION = "sas-intake-3"
FEATURE_LABELS = {
    "merge": "MERGE BY: требуется проверка кратности и семантики",
    "first_last": "FIRST./LAST.: нужно определить порядок и равенство ключей",
    "transpose": "TRANSPOSE: нужен контракт выходных колонок",
    "runtime_table_exists": "Ветвление по наличию таблицы: нужен каталог",
    "macro_definition": "Макрос: выполняется ограниченным детерминированным препроцессором; неизвестные значения остаются требованиями",
    "calculated_alias": "CALCULATED: проверяется область видимости и отсутствие remerge",
    "stop_execution": "Остановка процесса должна войти в контракт запуска",
    "email_side_effect": "Уведомление: внешний эффект требует решения",
    "data_step": "DATA STEP: SET, присваивания, IF/ELSE, DO, DELETE и FIRST/LAST при выполнении условий правила",
}


def sha(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def public_source(source: str) -> str:
    """Remove connection statements and inline data before persistence/model use.

    This is conservative display filtering, not a general secret scanner.
    Model/API calls are disabled by the current project policy.
    """
    # The shared lexer owns the inline-data boundary. A regex once swallowed
    # executable code after a valid `;RUN;` terminator in a real EGP.
    regions: list[tuple[int, int, str]] = []
    try:
        mask_noncode(source, sas=True, inline_regions=regions)
    except LexicalError as error:
        # A failed scan has no trustworthy boundary for the remaining text:
        # it may contain unterminated CARDS/DATALINES or another inline-data
        # region. Persist only the verified prefix. Keep a lexical error at the
        # same line so the compiler cannot mistake this projection for a full
        # program. The original EGP must be reimported after parser support is
        # improved; its hash and source location remain in the project.
        suffix = source[error.offset:]
        source = source[:error.offset] + "*/" + "".join(
            char if char in "\r\n" else " " for char in suffix
        )
    if regions:
        chars = list(source)
        for begin, end, marker in regions:
            header_end = begin + len(marker)
            terminator_length = 4 if "4" in marker.casefold() else 1
            for index in range(header_end, end - terminator_length):
                if chars[index] not in "\r\n":
                    chars[index] = " "
        source = "".join(chars)
    pieces = re.split(r"(/\*.*?\*/)", source, flags=re.S)
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(
            r"(?is)\blibname\s+([A-Za-z_]\w*|_all_)\b[^;]*;",
            lambda m: f"libname {m.group(1)} /* параметры скрыты; mapping deferred */;" + "\n" * m.group().count("\n"),
            pieces[index],
        )
        pieces[index] = re.sub(
            r"(?is)(?:\bfilename\b|\bproc\s+pwencode\b|%let\s+\w*(?:pass|pwd|secret|token|key)\w*\s*=)[^;]*;",
            lambda m: "".join(c if c in "\r\n" else " " for c in m.group()),
            pieces[index],
        )
    source = "".join(pieces)
    source = re.sub(r"(?i)\b(?:password|passwd|pwd|token|api_key|secret)\s*=\s*(?:'[^']*'|\"[^\"]*\"|[^\s;]+)", "credential=[hidden]", source)
    return source


def statements(source: str):
    """Compatibility facade for the shared deterministic lexer."""
    from .sas_lexer import lex_statements
    return lex_statements(source)


def sql_candidates(source: str, target: str, edits=None, date_storage="unknown", source_catalog=None, format_catalog=None, parameters=None, semantic_ir=None) -> dict:
    from .compiler_pipeline import CompilationRequest, DEFAULT_PIPELINE
    return DEFAULT_PIPELINE.compile(CompilationRequest(source, target, edits or {}, date_storage,
                                                        source_catalog or {}, format_catalog or {},
                                                        parameters or {}, semantic_ir))


def semantic_snapshot(source: str, source_catalog=None, parameters=None) -> dict:
    from .semantic_engine import SasSemanticEngine
    return SasSemanticEngine().analyze(source, source_catalog, parameters)


def refresh_block_analysis(block: dict, target: str = "postgresql", date_storage: str = "unknown") -> dict:
    ir = semantic_snapshot(block["source"], block.get("source_catalog"), block.get("parameters"))
    parsed = sql_candidates(block["source"], target, block.get("logic_edits"), date_storage,
                            block.get("source_catalog"), block.get("format_catalog"), block.get("parameters"), ir)
    block["analysis_ir"] = ir
    block["ir_hash"] = ir["ir_hash"]
    block["candidate_count"] = len(parsed["candidates"])
    block["inputs"], block["outputs"] = parsed["inputs"], parsed["outputs"]
    return parsed


def make_block(ident, label, source, flow, member, raw_hash):
    finding = features(source, sas=True, allow_partial=True)
    issues = [{"severity": "warning", "message": title, "count": finding["feature_counts"][key],
               "line": finding["feature_lines"].get(key, [1])[0]}
              for key, title in FEATURE_LABELS.items() if finding["feature_counts"][key]]
    for diagnostic in finding["lexical_diagnostics"]:
        issues.append({"severity": "error", "message": "Частичный разбор: нужен просмотр исходника",
                       "line": diagnostic["stopped_at_line"]})
    clean = public_source(source)
    ir = semantic_snapshot(clean)
    candidates = sql_candidates(clean, "postgresql", semantic_ir=ir)
    return {"id": ident, "name": label, "original_name": label, "purpose": "",
            "flow": flow, "member": member, "source_hash": raw_hash, "source": clean,
            "display_source_redacted": clean != source,
            "lines": len(source.splitlines()), "features": finding["feature_counts"],
            "procedures": finding.get("procedures", {}), "issues": issues,
            "inputs": candidates["inputs"], "outputs": candidates["outputs"],
            "candidate_count": len(candidates["candidates"]), "manual_sql": "", "logic_edits": {},
            "analysis_ir": ir, "ir_hash": ir["ir_hash"]}


def import_project(name: str, raw: bytes, rule_pack: dict | None = None) -> dict:
    if not raw or len(raw) > LIMIT:
        raise ValueError("Пустой файл или размер больше 32 МБ")
    suffix = Path(name).suffix.lower()
    blocks, orders, diagnostics = [], [], []
    if suffix == ".egp":
        with tempfile.TemporaryDirectory(prefix="sas-intake-") as tmp:
            path = Path(tmp) / "input.egp"
            path.write_bytes(raw)
            inventory = inspect_egp(path)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for node in inventory["code_tasks"]:
                source = decode_source(archive.read(node["member"]))[0] if node["status"] != "source_missing_or_external" else ""
                block = make_block(node["id"], node["label"] or node["id"], source, node["flow"], node["member"], node.get("sha256", ""))
                if not source:
                    block["issues"].append({"severity": "error", "message": "Исходник отсутствует", "line": 1})
                blocks.append(block)
        orders = inventory["ordered_lists"]
        diagnostics = inventory["diagnostics"]
        other = {k.rsplit(".", 1)[-1]: v for k, v in inventory["element_types"].items()
                 if not k.endswith(("CodeTask", "ProcessFlowContainer", "OrderedList", "OrderedListContainer", "Log", "Code", "ODSResult"))}
        if other:
            diagnostics.append({"code": "metadata_inventory_only", "types": other})
    elif suffix == ".sas":
        blocks = [make_block("program-1", Path(name).stem, decode_source(raw)[0], "Main", name, sha(raw))]
    else:
        raise ValueError("В MVP поддержаны .egp и .sas")
    if not blocks:
        raise ValueError("Не найдено программ для анализа")
    order = orders[0]["task_ids"] if orders else [b["id"] for b in blocks]
    known = {b["id"] for b in blocks}
    selected = [i for i in order if i in known]
    if rule_pack is None:
        from .embedded_rules import embedded_rule_pack
        rule_pack = embedded_rule_pack()
    pack_summary = {key: rule_pack.get(key) for key in ("rule_pack_id", "compiler_revision", "statistics", "kind", "display_name") if key in rule_pack}
    project = {"id": uuid.uuid4().hex, "name": Path(name).name, "source_hash": sha(raw), "analysis_rule_version": RULE_VERSION,
               "intake_revision": INTAKE_REVISION,
               "scenario_confirmed": suffix == ".sas" or len(orders) == 1 or (not orders and len(blocks) == 1),
               "revision": 1, "date_storage": "unknown", "target": "greenplum", "target_version": "", "schema": "migration_draft",
               "run_date": date.today().isoformat(), "timezone": "Europe/Moscow",
               "catalog_complete": False,
               "blocks": blocks, "orders": orders, "selected": selected, "diagnostics": diagnostics,
               "rule_pack": pack_summary, "gate1": None, "gate2": None, "build": None, "events": [], "proposal": None}
    refresh_project_plan(project)
    return project


def reimport_project(previous: dict, name: str, raw: bytes) -> dict:
    """Rebuild from the original archive; retain edits for explicit review."""
    refreshed = import_project(name, raw)
    refreshed["id"] = previous["id"]
    refreshed["revision"] = previous["revision"] + 1
    refreshed["events"] = list(previous.get("events", []))
    for key in ("target", "target_version", "schema", "date_storage", "run_date", "timezone", "catalog_complete"):
        if key in previous:
            refreshed[key] = copy.deepcopy(previous[key])
    old_blocks = {block["id"]: block for block in previous["blocks"]}
    pending = []
    for block in refreshed["blocks"]:
        old = old_blocks.get(block["id"])
        if not old:
            continue
        if old.get("source_hash") == block.get("source_hash"):
            for key in ("name", "purpose", "source_catalog", "format_catalog", "parameters"):
                if key in old:
                    block[key] = copy.deepcopy(old[key])
        kinds = [key for key in ("manual_sql", "logic_edits") if old.get(key)]
        if kinds:
            pending.append({"block_id": block["id"], "previous_revision": previous["revision"],
                            "fields": kinds, "status": "review_required"})
    missing = [ident for ident in previous.get("selected", [])
               if ident not in {block["id"] for block in refreshed["blocks"]}]
    refreshed["selected"] = [ident for ident in previous.get("selected", [])
                             if ident not in missing]
    refreshed["scenario_confirmed"] = bool(previous.get("scenario_confirmed") and not missing)
    refreshed["reimport_conflicts"] = pending
    if missing:
        refreshed["diagnostics"].append({"code": "reimport_missing_selected_tasks", "task_ids": missing})
    for block in refreshed["blocks"]:
        refresh_block_analysis(block, refreshed["target"], refreshed["date_storage"])
    refresh_project_plan(refreshed)
    refreshed.update(gate1=None, gate2=None, build=None, proposal=None)
    return refreshed


def project_request(project: dict) -> ProjectCompilationRequest:
    by_id = {block["id"]: block for block in project["blocks"]}
    programs = tuple(
        ProgramRequest(
            id=ident,
            name=by_id[ident]["name"],
            source=by_id[ident]["source"],
            source_hash=by_id[ident].get("source_hash", ""),
            source_catalog=by_id[ident].get("source_catalog", {}),
            format_catalog=by_id[ident].get("format_catalog", {}),
            parameters=by_id[ident].get("parameters", {}),
            edits=by_id[ident].get("logic_edits", {}),
        )
        for ident in project["selected"]
    )
    raw_date = project.get("run_date")
    try:
        fixed_date = date.fromisoformat(raw_date) if raw_date else None
    except (TypeError, ValueError):
        raise ValueError("Дата запуска должна быть в формате ГГГГ-ММ-ДД") from None
    return ProjectCompilationRequest(
        programs=programs,
        target=project["target"],
        target_version=project.get("target_version", ""),
        date_storage=project.get("date_storage", "unknown"),
        run_date=fixed_date,
        timezone=project.get("timezone", "Europe/Moscow"),
        catalog_complete=bool(project.get("catalog_complete", False)),
    )


def refresh_project_plan(project: dict) -> dict:
    request = project_request(project)
    plan = DEFAULT_PROJECT_COMPILER.analyze(request)
    preview = DEFAULT_PROJECT_COMPILER.compile(request, plan)
    project["project_plan"] = plan
    by_id = {block["id"]: block for block in project["blocks"]}
    preview_by_id = {program["program_id"]: program for program in preview["programs"]}
    for program in plan["programs"]:
        block = by_id[program["id"]]
        compiled = preview_by_id[program["id"]]
        block["project_ir_hash"] = program["ir"]["ir_hash"]
        block["macro_requirement_count"] = len(program["macro_requirements"])
        block["macro_trace_count"] = len(program["macro_trace"])
        block["candidate_count"] = len(compiled["candidates"])
        block["project_unsupported_count"] = len(compiled["unsupported"])
        block["inputs"], block["outputs"] = compiled["inputs"], compiled["outputs"]
    return plan


def selected_ir_hash(project):
    if project.get("project_plan"):
        return project["project_plan"]["plan_hash"]
    by_id = {block["id"]: block for block in project["blocks"]}
    return sha([{"id": ident, "ir_hash": by_id[ident].get("ir_hash")} for ident in project["selected"]])


def target_profile_hash(project):
    return sha({"target": project.get("target"), "version": project.get("target_version"), "schema": project.get("schema"),
                "date_storage": project.get("date_storage"), "run_date": project.get("run_date"),
                "timezone": project.get("timezone"), "catalog_complete": project.get("catalog_complete", False)})


def plan_hash(project):
    return sha({"source_hash": project.get("source_hash"), "target_profile_hash": target_profile_hash(project),
                "selected": project.get("selected"), "blocks": project.get("blocks"),
                "rule_pack_id": project.get("rule_pack", {}).get("rule_pack_id"),
                "selected_ir_hash": selected_ir_hash(project)})


def generate(project):
    if project.get('analysis_rule_version') != RULE_VERSION:
        raise ValueError('Версия правил изменилась. Повторите анализ и согласование плана.')
    if not project["gate1"] or project["gate1"]["plan_hash"] != plan_hash(project):
        raise ValueError("Сначала согласуйте актуальный план черновика")
    if project["gate1"].get("ir_hash") != selected_ir_hash(project) or project["gate1"].get("rule_pack_id") != project.get("rule_pack", {}).get("rule_pack_id"):
        raise ValueError("IR или RulePack изменились после Gate 1. Повторите согласование.")
    project_plan = project.get("project_plan")
    if not project_plan or project_plan.get("plan_hash") != selected_ir_hash(project):
        raise ValueError("Нет актуального проектного плана. Повторите анализ.")
    compiled = DEFAULT_PROJECT_COMPILER.compile(project_request(project), project_plan)
    result = []
    by_id = {b["id"]: b for b in project["blocks"]}
    for parsed in compiled["programs"]:
        ident = parsed["program_id"]
        block = by_id[ident]
        code = "\n\n".join(f"-- Expanded SAS line {c['line']}\n{c['sql']}" for c in parsed["candidates"])
        manual = bool(block["manual_sql"].strip())
        if manual:
            code = block["manual_sql"]
        checks = []
        if code:
            try:
                trees = sqlglot.parse(code, read=TARGETS[project["target"]], error_level=sqlglot.ErrorLevel.RAISE)
                if not trees or any(t is None or t.find(exp.Command) for t in trees):
                    raise ValueError()
                checks.append({"name": "SQL AST", "status": "passed"})
            except Exception:
                checks.append({"name": "SQL AST", "status": "failed"})
        else:
            checks.append({"name": "SQL AST", "status": "not_run"})
        if not code:
            code = "-- Для этого блока пока нет SQL-кандидата. Требуется отдельное правило или ручная реализация."
        result.append({"id": ident, "name": block["name"], "sql": code, "manual": manual,
                       "candidate_count": len(parsed["candidates"]), "unsupported": parsed["unsupported"],
                       "operations": parsed["operations"], "steps": parsed["steps"],
                       "ir_hash": parsed["ir_hash"], "dependencies": parsed["dependencies"],
                       "cutoff": parsed["cutoff"], "checks": checks,
                       "source_hash": block["source_hash"], "semantic_status": "unverified",
                       "macro_requirements": parsed.get("macro_requirements", []),
                       "macro_trace": parsed.get("macro_trace", []),
                       "macro_source_map": parsed.get("macro_source_map", []),
                       "dataset_versions": [event for event in project_plan["dataset_versions"]
                                            if event["program_id"] == ident]})
        result[-1]["source_member"] = block["member"]
        result[-1]["display_source_redacted"] = block["display_source_redacted"]
        result[-1]["source_catalog"] = block.get('source_catalog',{})
        result[-1]["format_catalog"] = block.get('format_catalog',{})
        result[-1]["parameters"] = block.get('parameters',{})
    build = {"plan_hash": plan_hash(project), "project_plan_hash": project_plan["plan_hash"],
             "project_compiler_revision": PROJECT_COMPILER_REVISION, "rule_version": RULE_VERSION,
             "rule_pack_id": project.get("rule_pack", {}).get("rule_pack_id"),
             "ir_hash": selected_ir_hash(project), "target_profile_hash": target_profile_hash(project),
             "sqlglot_version": sqlglot.__version__, "target": project["target"],
             "target_version": project["target_version"], "blocks": result,
             "requirements": compiled["requirements"], "completeness": compiled["completeness"],
             "execution": "not_run", "comparison": "not_run", "status": "unverified_draft"}
    from .quality import assess
    build["quality"] = assess(project, build)
    build["hash"] = sha(build)
    return build


def project_program_preview(project: dict, block_id: str, edits: dict | None = None) -> dict:
    """Compile one UI preview through the same project-wide seam as Gate 2."""
    working = copy.deepcopy(project)
    block = next((item for item in working["blocks"] if item["id"] == block_id), None)
    if not block:
        raise ValueError("Блок не найден")
    if block_id not in working["selected"]:
        working["selected"].append(block_id)
    if edits is not None:
        block["logic_edits"] = edits
    plan = refresh_project_plan(working)
    compiled = DEFAULT_PROJECT_COMPILER.compile(project_request(working), plan)
    result = next(item for item in compiled["programs"] if item["program_id"] == block_id)
    return {
        **result,
        "project_plan_hash": plan["plan_hash"],
        "project_completeness": plan["completeness"],
        "project_requirements": [item for item in plan["requirements"] if item["program_id"] == block_id],
        "dataset_versions": [item for item in plan["dataset_versions"] if item["program_id"] == block_id],
    }


def transition(project, action, payload):
    p = copy.deepcopy(project)
    if p.get("intake_revision") != INTAKE_REVISION:
        raise ValueError("Исходник сохранён прежним фильтром. Импортируйте исходный EGP заново; старая ревизия сохранена.")
    from .embedded_rules import embedded_rule_pack
    expected_pack = embedded_rule_pack()
    if action != "reanalyze" and p.get("rule_pack", {}).get("rule_pack_id") != expected_pack["rule_pack_id"]:
        raise ValueError("Проект использует прежнюю версию встроенных правил. Повторите анализ.")
    if action == "edit":
        allowed = {"target", "target_version", "schema", "selected", "block", "date_storage", "run_date", "catalog_complete"}
        if set(payload) - allowed:
            raise ValueError("Недопустимые поля изменения")
        for field in ("target", "target_version", "schema", "selected", "date_storage", "run_date", "catalog_complete"):
            if field in payload:
                p[field] = payload[field]
        if "selected" in payload:
            p["scenario_confirmed"] = True
        if p.get("date_storage", "unknown") not in {"unknown", "native"}:
            raise ValueError("Некорректный контракт дат")
        if p["target"] not in TARGETS:
            raise ValueError("Неизвестная СУБД")
        if not isinstance(p["target_version"], str) or len(p["target_version"]) > 80:
            raise ValueError("Некорректная версия СУБД")
        if not isinstance(p["schema"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", p["schema"]):
            raise ValueError("Имя схемы: латинские буквы, цифры и подчёркивание")
        if not isinstance(p.get("catalog_complete", False), bool):
            raise ValueError("Признак полноты каталога должен быть логическим")
        try:
            date.fromisoformat(p["run_date"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Дата запуска должна быть в формате ГГГГ-ММ-ДД") from None
        known = {b["id"] for b in p["blocks"]}
        if not isinstance(p["selected"], list) or any(not isinstance(i, str) or i not in known for i in p["selected"]) or len(p["selected"]) != len(set(p["selected"])):
            raise ValueError("Некорректный состав плана")
        if "block" in payload:
            update = payload["block"]
            if not isinstance(update, dict) or set(update) - {"id", "name", "purpose", "manual_sql", "logic_edits", "source_catalog", "format_catalog", "parameters"}:
                raise ValueError("Недопустимые поля блока")
            block = next((b for b in p["blocks"] if b["id"] == update.get("id")), None)
            if not block:
                raise ValueError("Блок не найден")
            for field in ("name", "purpose", "manual_sql"):
                if field in update:
                    if not isinstance(update[field], str) or len(update[field]) > (100000 if field == "manual_sql" else 2000):
                        raise ValueError("Некорректное значение поля")
                    block[field] = update[field]
            if "parameters" in update:
                from .parameters import validate_parameters
                block["parameters"] = validate_parameters(update["parameters"])
            if "format_catalog" in update:
                from .format_catalog import validate_formats
                block["format_catalog"] = validate_formats(update["format_catalog"])
            if "source_catalog" in update:
                from .schema_flow import validate_catalog
                block["source_catalog"] = validate_catalog(update["source_catalog"])
            if "logic_edits" in update:
                if not isinstance(update["logic_edits"], dict):
                    raise ValueError("Некорректные правки логики")
                sql_candidates(block["source"],p["target"],update["logic_edits"],p.get("date_storage","unknown"),block.get("source_catalog"),block.get("format_catalog"),block.get("parameters"))
                block["logic_edits"] = update["logic_edits"]
                block["manual_sql"] = ""
        for b in p["blocks"]:
            refresh_block_analysis(b, p["target"], p.get("date_storage", "unknown"))
        refresh_project_plan(p)
        p['analysis_rule_version'] = RULE_VERSION
        p.update(gate1=None, gate2=None, build=None, proposal=None)
    elif action == "reanalyze":
        p["rule_pack"] = {key: expected_pack.get(key) for key in ("rule_pack_id", "compiler_revision", "statistics", "kind", "display_name") if key in expected_pack}
        for b in p["blocks"]:
            refresh_block_analysis(b, p["target"], p.get("date_storage", "unknown"))
            for issue in b['issues']:
                if issue['message'].startswith('DATA STEP:'):
                    issue['message'] = FEATURE_LABELS['data_step']
                elif issue['message'].startswith('Макрос:'):
                    issue['message'] = FEATURE_LABELS['macro_definition']
        if not p.get("run_date"):
            p["run_date"] = date.today().isoformat()
        p.setdefault("timezone", "Europe/Moscow")
        p.setdefault("catalog_complete", False)
        refresh_project_plan(p)
        p['analysis_rule_version'] = RULE_VERSION
        p.update(gate1=None,gate2=None,build=None,proposal=None)
    elif action == "gate1":
        if p.get('analysis_rule_version') != RULE_VERSION:
            raise ValueError('Повторите анализ текущими правилами перед согласованием')
        if not p["selected"]:
            raise ValueError("Выберите хотя бы один блок")
        if not p.get("scenario_confirmed", False):
            raise ValueError("Подтвердите выбранный сценарий запуска перед первым гейтом")
        if not p.get("rule_pack", {}).get("rule_pack_id") or not p.get("project_plan"):
            raise ValueError("Сначала повторите анализ: проект не содержит RulePack или проектный IR")
        p["gate1"] = {"plan_hash": plan_hash(p), "mode": "draft_plan", "revision": p["revision"],
                      "ir_hash": selected_ir_hash(p), "rule_pack_id": p["rule_pack"]["rule_pack_id"],
                      "target_profile_hash": target_profile_hash(p),
                      "project_compiler_revision": PROJECT_COMPILER_REVISION,
                      "completeness": p["project_plan"]["completeness"]}
        p["gate2"], p["build"] = None, None
    elif action == "build":
        p["build"] = generate(p)
        p["gate2"] = None
    elif action == "gate2":
        if not p["build"] or p["build"].get('rule_version') != RULE_VERSION or p["build"]["plan_hash"] != plan_hash(p) or p["build"].get("rule_pack_id") != p.get("rule_pack", {}).get("rule_pack_id") or p["build"].get("ir_hash") != selected_ir_hash(p):
            raise ValueError("Нет актуальной сборки")
        if any(c["status"] == "failed" for b in p["build"]["blocks"] for c in b["checks"]):
            raise ValueError("Исправьте ошибки SQL перед согласованием черновика")
        if not any(b["candidate_count"] or b["manual"] for b in p["build"]["blocks"]):
            raise ValueError("Нет SQL-кандидатов для согласования")
        p["gate2"] = {"build_hash": p["build"]["hash"], "mode": "unverified_draft"}
    elif action == "proposal":
        p["proposal"] = payload
    elif action == "apply_proposal":
        proposal = p["proposal"]
        if not proposal or proposal["plan_hash"] != plan_hash(p):
            raise ValueError("Предложение устарело")
        p = transition(p, "edit", {"block": proposal["change"]})
    else:
        raise ValueError("Неизвестное действие")
    return p


def export_package(project):
    if not project["build"] or project["build"].get('rule_version') != RULE_VERSION or project["build"]["plan_hash"] != plan_hash(project) or project["build"].get("rule_pack_id") != project.get("rule_pack", {}).get("rule_pack_id") or project["build"].get("ir_hash") != selected_ir_hash(project):
        raise ValueError("Сначала соберите SQL-черновик")
    data = io.BytesIO()
    manifest = {"status": "unverified_draft", "project_hash": project["source_hash"],
                "build": project["build"], "gate1": project["gate1"], "gate2": project["gate2"],
                "excluded_blocks": [b["id"] for b in project["blocks"] if b["id"] not in project["selected"]],
                "diagnostics": project["diagnostics"]}
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        for idx, block in enumerate(project["build"]["blocks"], 1):
            z.writestr(f"sql/{idx:02d}_draft.sql", "-- UNVERIFIED DRAFT. SAS/target execution and comparison NOT RUN.\n" + block["sql"])
        from .quality import report_text
        z.writestr("QUALITY.md", report_text(project["build"]["quality"]))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("README.txt", "SQL-кандидаты для ручной доработки. Не запускать как готовую миграцию.\n"
                   "В manifest.json перечислены неперенесённые statements и исключённые блоки.\n"
                   "Greenplum: общий SQL-рендеринг, физический DDL не реализован.\n"
                   "ClickHouse: синтаксический кандидат, engine и стратегия загрузки не определены.\n")
    return data.getvalue()
