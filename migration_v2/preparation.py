"""Actionable, derived preparation requirements. Never grants approvals."""
from __future__ import annotations

from .contracts import digest
import re
from .context import AI_HANDLED_ISSUES


def prepare(project):
    context = project.get("context") or {}
    approved_plan = project.get('plan')
    plan = approved_plan or project.get('intake') or project.get('plan_draft') or {}
    ai_mode=plan.get('acceptance')=='code_review' or bool(project.get('intake'))
    bindings = {b["source_id"]: b for b in plan.get("bindings", [])}
    reviewed = {r["issue_id"] for r in plan.get("resolutions", [])}
    requirements = []

    def add(code, obj, message, actions, category="context", stage="context_ready", detail=""):
        requirements.append({"id": "req:" + digest([code, obj])[:24], "code": code,
            "object_id": obj, "category": category, "severity": "blocking", "status": "open",
            "message": message, "detail": detail, "actions": actions, "blocks": [stage]})
        return requirements[-1]

    selected = set((project.get("scenario") or {}).get("source_ids", []))
    for source in project["snapshot"]["sources"]:
        if source["status"] != "available" and (not selected or source["id"] in selected):
            add("SOURCE_FILE_REQUIRED", source["id"], "Нужен исходник программы " + source["label"],
                ["attach_source"], "source")
    if not project.get("scenario"):
        add("EXECUTION_ORDER_REQUIRED", "scenario", "Выберите сценарий или задайте порядок программ", ["select_scenario"])
    parameters = set()
    for entry in context.get("inputs", []):
        if entry['id'] in plan.get('intermediate_inputs',[]):continue
        binding = bindings.get(entry["id"])
        if entry.get("dynamic") and not (binding and binding.get('members_complete') and binding.get('members')):
            parameters.update(re.findall(r"&([A-Za-z_]\w*)", entry["original"]))
        if binding is None:
            add("SOURCE_BINDING_REQUIRED", entry["id"], "Задайте назначение источника " + entry["original"], ["save_plan"], "mapping")
        elif not binding["columns"]:
            add("COLUMN_TYPES_REQUIRED", entry["id"], "Нужны поля и типы источника " + entry["original"], ["save_plan"], "metadata")
        elif ai_mode and (not binding.get('schema_complete') or binding.get('metadata_status')!='user_supplied'):
            add('COMPLETE_SCHEMA_REQUIRED',entry['id'],'Подтвердите полный состав полей источника '+entry['original'],['import_binding'],'metadata')
    supplied={k.casefold() for k in project.get('parameters',{})}
    for name in sorted(n for n in parameters if n.casefold() not in supplied):
        add("PARAMETER_REQUIRED", name.casefold(), "Задайте начальное значение параметра " + name,
            ["set_parameters"], "parameter")
    for item in context.get("issues", []):
        if ai_mode and item["code"] in AI_HANDLED_ISSUES:continue
        if ai_mode and item['code']=='dynamic_source':
            ident=item['id'].removesuffix(':dynamic')
            if ident in plan.get('intermediate_inputs',[]) or bindings.get(ident,{}).get('members_complete'):continue
        if item["id"] in reviewed and not item["hard"]:
            continue
        if item["code"] == "missing_source":
            continue  # One root cause, not two questions.
        add(item["code"], item["id"], "Программа " + item["source_id"] + ": " + item["code"],
            ["save_plan"] if not item["hard"] else [], "implementation" if item["hard"] else "review",
            "plan_complete", item.get("detail", ""))
    if approved_plan and not ai_mode and not plan.get("comparisons"):
        add("SAS_REFERENCE_REQUIRED", "reference", "Нужен независимый результат SAS для проверки данных",
            ["save_plan"], "reference", "results_matched")
    if plan.get("mode") == "reusable_etl":
        add("REUSABLE_RUNNER_REQUIRED", "mode", "Регулярный процесс требует исполнителя управляющих шагов",
            [], "implementation", "plan_complete")
    grouped={}
    for item in (project.get("task_plan") or {}).get("requirements",[]):
        message={"external_parameter_required":"Нужно внешнее значение параметра ","macro_definition_required":"Нужно определение макроса "}.get(item["code"],"Требуется уточнение плана: ")
        obj=item.get("name",item.get("region_id",item.get("source_id","project")))
        key=(item['code'],obj)
        if key not in grouped:
            grouped[key]=add(item['code'],obj,message+item.get('name',item.get('source_id',item['code'])),item.get('actions',[]),'parameter' if item['code']=='external_parameter_required' else 'source','plan_complete')
            grouped[key]['occurrences']=[]
        grouped[key]['occurrences'].append({k:v for k,v in item.items() if k in {'source_id','scenario_id','region_id','line','offset','reference','environment_variable'}})
    seen={}
    for item in (project.get("pipeline") or {}).get("requirements",[]):
        key=digest([item["code"],item["message"],item.get('required_information')])
        if key not in seen:
            actions=["generate"] if item["code"] in {"review_protocol_error", "review_interrupted"} else (
                ["update_contract"] if item.get("required_information") else ["replace_task_sql","update_contract","grant_model_calls"])
            seen[key]=add(item["code"],key,item["message"],actions,"review","code_checked",item.get("required_information") or '')
            seen[key]['occurrences']=[]
        seen[key]['occurrences'].append({k:v for k,v in item.items() if k in {'target','scenario_id','unit_id','obligation','sas_location','sql_location'}})
    for r in requirements:
        if r.get('occurrences'):r['detail']=(r['detail']+' ' if r['detail'] else '')+'Места: '+__import__('json').dumps(r['occurrences'],ensure_ascii=False)
    evidence = project.get("evidence") or {}
    checks = evidence.get("checks", {})
    ready = bool(context) and not any("context_ready" in r["blocks"] for r in requirements)
    complete = bool(approved_plan) and not project.get("blockers") and not any("plan_complete" in r["blocks"] for r in requirements)
    return {"schema_version": 1, "requirements": requirements,
        "basis_hash": digest({"snapshot": project["snapshot"]["hash"], "context": context, "plan": plan}),
        "stages": {"source_read": "passed", "context_ready": "passed" if ready else "needs_input",
            "plan_complete": "passed" if complete else "needs_input",
            "sql_generated": "passed" if project.get("candidate") else "not_run",
            "sql_executed": checks.get("execution", "not_run"),
            "code_checked":checks.get("code_review","not_run"),
            "code_accepted":"passed" if project.get("gate2") else "not_run",
            "results_matched": checks.get("equivalence", "not_run")}}
