"""Immutable local revisions and compare-and-swap commands, no network side effects."""
from __future__ import annotations

from contextlib import closing
import copy
from datetime import datetime, timezone
import io
import json
import re
from pathlib import Path
import sqlite3
import uuid
import zipfile

from .context import task_context, plan_issues
from .contracts import Candidate, Plan, ManualInput, digest, parse_json
from .sources import SourceVault, ingest
from .verification import verify
from .preparation import prepare


class Conflict(ValueError):
    pass


class Workbench:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.vault = SourceVault(self.root / "originals")
        self.db = self.root / "v2.sqlite3"
        if self.db.exists():
            with closing(self.connect()) as old:
                version=old.execute("PRAGMA user_version").fetchone()[0]
                if version>2:raise ValueError("Project store belongs to a newer application version")
                if version<2:
                    backup_dir=self.root/'backups';backup_dir.mkdir(exist_ok=True)
                    backup=backup_dir/'pre-ai-code-review.sqlite3'
                    if not backup.exists():
                        with closing(sqlite3.connect(backup)) as target:old.backup(target)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS revisions (id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id,revision))")
            db.execute("PRAGMA user_version=2")

    def connect(self):
        return sqlite3.connect(self.db, timeout=10)

    def get(self, ident, revision=None):
        with closing(self.connect()) as db:
            if revision is None:
                row = db.execute("SELECT body FROM revisions WHERE id=? ORDER BY revision DESC LIMIT 1", (ident,)).fetchone()
            else:
                row = db.execute("SELECT body FROM revisions WHERE id=? AND revision=?", (ident, revision)).fetchone()
        if not row:
            raise KeyError("Project/revision not found")
        project = json.loads(row[0])
        if project.get("gate2") and (project.get("plan") or {}).get("acceptance")=="code_review" and not self.code_review_current(project):
            project["stale_gate2"]=project["gate2"]
            project["gate2"]=None
            project["state"]="NEEDS_HITL"
        project["preparation"] = prepare(project)
        if project.get('plan'):
            from .destinations import resolve
            project['destination_preview']=resolve(project['plan'])['registry']
        return project

    def recent(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT r.body FROM revisions r JOIN (SELECT id,MAX(revision) v FROM revisions GROUP BY id) s ON r.id=s.id AND r.revision=s.v ORDER BY r.rowid DESC LIMIT 30").fetchall()
        return [{key: p[key] for key in ("id", "name", "revision", "state")} for p in (json.loads(r[0]) for r in rows)]

    def import_egp(self, name, raw):
        snapshot = ingest(name, raw, self.vault)
        project = {"schema_version": 1, "id": uuid.uuid4().hex, "name": Path(name).name,
                   "revision": 1, "snapshot": snapshot, "scenario": None, "context": None,
                   "plan": None, "parameters": {}, "manual_inputs": [], "blockers": [], "gate1": None, "task": None,
                   "candidate": None, "evidence": None, "gate2": None, "state": "IMPORTED",
                   "events": []}
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO revisions VALUES (?,?,?)", (project["id"], 1, json.dumps(project, ensure_ascii=False)))
        return project

    def source_text(self, project, source_id):
        source = next((s for s in project["snapshot"]["sources"] if s["id"] == source_id), None)
        if not source or source["status"] != "available":
            raise KeyError("Source unavailable")
        return self.vault.text(source)

    def task_package(self, project):
        if not project["task"]:
            raise ValueError("Approve a current contract first")
        # Private local source attachments; this is NOT an approved model payload.
        # Future transport must apply the corporate exposure policy separately.
        task = copy.deepcopy(project["task"])
        task["local_sources"] = [{"id": ident, "text": self.source_text(project, ident)}
                                 for ident in task["context"]["source_ids"]
                                 if task["context"]["source_hashes"].get(ident)]
        task["transport_status"] = "local_only_not_approved_for_model"
        return task

    def command(self, ident, revision, action, payload):
        old = self.get(ident)
        if old["revision"] != revision:
            raise Conflict("Project changed; reload before applying command")
        new = copy.deepcopy(old)
        if action == "attach_source":
            if set(payload) != {"source_id", "filename", "text"} or not isinstance(payload["text"], str):
                raise ValueError("Provide source_id, filename and source text")
            if not payload["filename"].lower().endswith(".sas") or len(payload["text"].encode("utf-8")) > 2_000_000:
                raise ValueError("Attach a SAS source at most 2 MB")
            source = next((s for s in new["snapshot"]["sources"] if s["id"] == payload["source_id"]), None)
            if not source or source["status"] == "available":
                raise ValueError("Attachment must resolve a missing source")
            key = self.vault.put(payload["text"].encode("utf-8"))
            new.setdefault("attachments", []).append({"source_id": source["id"], "sha256": key,
                "filename": Path(payload["filename"]).name, "actor": "local_user"})
            source.update(status="available", sha256=key, encoding="utf-8", bytes=len(payload["text"].encode("utf-8")))
            new["snapshot"]["hash"] = digest({"archive": new["snapshot"]["archive_sha256"],
                "attachments": new["attachments"], "includes": new.get("include_sources", [])})
            if new["scenario"]:
                new["context"] = task_context(new["snapshot"], new["scenario"], self.vault, new["parameters"], new["manual_inputs"], includes=new.get("include_sources", ()))
            new.update(plan=None, blockers=[])
            invalidate(new)
        elif action == "attach_include":
            if set(payload)!={"source_id","offset","filename","text"} or not isinstance(payload['text'],str) or not payload['text'].strip() or len(payload['text'].encode())>2_000_000 or not isinstance(payload['filename'],str) or not payload['filename'].lower().endswith('.sas') or type(payload['offset']) is not int:
                raise ValueError('Provide an explicit include reference and one UTF-8 SAS file')
            parent=next((s for s in old['snapshot']['sources']+old.get('include_sources',[]) if s['id']==payload['source_id']),None)
            if not parent or parent['status']!='available':raise ValueError('Include parent source unavailable')
            from app.sas_lexer import lex_statements
            statements,_=lex_statements(self.vault.text(parent))
            stmt=next((s for s in statements if s['offset']==payload['offset']),None)
            if not stmt or not re.fullmatch(r'''%include\s+(['"])(.*?)\1\s*;''',stmt['text'].strip(),re.I|re.S):
                raise ValueError('Attach one file to one literal %INCLUDE; other forms require a reviewed strategy')
            key=self.vault.put(payload['text'].encode('utf-8'))
            item={'id':'include-'+digest([parent['id'],payload['offset'],key])[:20],'parent_source_id':parent['id'],
                'parent_sha256':parent['sha256'],'offset':payload['offset'],'label':Path(payload['filename']).name,
                'status':'available','sha256':key,'encoding':'utf-8','parameters_hash':digest(old['parameters'])}
            new['include_sources']=[s for s in new.get('include_sources',[]) if (s['parent_source_id'],s['offset'])!=(parent['id'],payload['offset'])]+[item]
            new['snapshot']['hash']=digest({'archive':new['snapshot']['archive_sha256'],'attachments':new.get('attachments',[]),'includes':new['include_sources']})
            if new["scenario"]:
                new["context"] = task_context(new["snapshot"], new["scenario"], self.vault,
                    new["parameters"], new["manual_inputs"], includes=new["include_sources"])
            new['plan_draft']=old['plan'] or old.get('plan_draft');new.update(plan=None,blockers=[]);invalidate(new)
        elif action == "attach_library":
            if set(payload)!={"scenario_id","before_source_id","filename","text"} or not isinstance(payload["text"],str) or not payload["text"].strip() or len(payload["text"].encode())>2_000_000 or not payload["filename"].lower().endswith('.sas'):
                raise ValueError("Provide a SAS library, scenario and explicit insertion point")
            order=next((o for o in new.get("scope_orders",[]) if o["id"]==payload["scenario_id"]),None)
            if not order or payload["before_source_id"] not in order["source_ids"]:
                raise ValueError("Choose the exact scenario and insertion point")
            key=self.vault.put(payload["text"].encode('utf-8'));source_id="library-"+key[:20]
            if any(s['id']==source_id for s in new['snapshot']['sources']):raise ValueError("This library is already attached; reuse its existing program ID")
            new['snapshot']['sources'].append({'id':source_id,'label':Path(payload['filename']).name,'status':'available','sha256':key,'encoding':'utf-8','bytes':len(payload['text'].encode()),'origin':'user_attached_library'})
            order['source_ids'].insert(order['source_ids'].index(payload['before_source_id']),source_id)
            new.setdefault('attachments',[]).append({'source_id':source_id,'sha256':key,'scenario_id':order['id']})
            new['snapshot']['hash']=digest({'archive':new['snapshot']['archive_sha256'],'attachments':new['attachments'],'includes':new.get('include_sources',[])})
            new['scenario'].update(id='whole-'+digest(new['scope_orders'])[:16],source_ids=list(dict.fromkeys(i for o in new['scope_orders'] for i in o['source_ids'])))
            new['context']=task_context(new['snapshot'],new['scenario'],self.vault,new['parameters'],new['manual_inputs'],includes=new.get('include_sources',()))
            new['plan_draft']=old['plan'];new.update(plan=None,blockers=[]);invalidate(new)
        elif action == "import_binding":
            from .contracts import Binding
            binding=parse_json(Binding,json.dumps(payload))
            if not old['context'] or binding.source_id not in {i['id'] for i in old['context']['inputs']}:
                raise ValueError("Choose a detected source before importing metadata")
            drafts={b['source_id']:b for b in new.get('binding_drafts',[])}
            drafts[binding.source_id]=binding.model_dump(mode='json');new['binding_drafts']=list(drafts.values())
            if new.get('intake'):
                new['intake']['bindings']=[b for b in new['intake']['bindings'] if b['source_id']!=binding.source_id]+[binding.model_dump(mode='json')]
            if new['plan']:
                new['plan']['bindings']=[b for b in new['plan']['bindings'] if b['source_id']!=binding.source_id]+[binding.model_dump(mode='json')]
                plan=parse_json(Plan,json.dumps(new['plan']));new['blockers']=plan_issues(plan,new['context'])
                invalidate(new)
                if plan.acceptance=='code_review':
                    from .task_planner import plan_tasks
                    new['task_plan']=plan_tasks(self,new)
                new['state']='PLAN_READY' if not new['blockers'] and not (new.get('task_plan') or {}).get('requirements') else 'NEEDS_CONTEXT'
            else:invalidate(new)
        elif action == "attach_reference":
            from .reference import ReferenceBundle
            if not old["plan"]:
                raise ValueError("Save a plan before attaching a reference registry")
            reference = parse_json(ReferenceBundle, json.dumps(payload))
            reference.check_plan(parse_json(Plan, json.dumps(old["plan"])), old["snapshot"]["hash"])
            new.update(reference_registry=reference.model_dump(mode="json"), evidence=None, gate2=None)
        elif action == "analyze_macros":
            if payload or not old["scenario"]:
                raise ValueError("Select scenario before macro analysis")
            from app.macro_processor import MacroProcessor
            processor = MacroProcessor()
            processor.seed(old["parameters"])
            expansions = []
            for source_id in old["scenario"]["source_ids"]:
                try:
                    result = processor.expand_program(source_id, self.source_text(old, source_id))
                    expansions.append({"source_id": source_id, "source_hash": result.source_hash,
                        "expanded_hash": result.expanded_hash, "source_map": list(result.source_map),
                        "requirements": list(result.requirements), "trace": list(result.trace)})
                except (ValueError, KeyError):
                    expansions.append({"source_id": source_id, "requirements": [{"code": "macro_analysis_unavailable"}]})
            new["macro_analysis"] = {"basis_hash": digest([old["snapshot"]["hash"],old["parameters"],old["scenario"]]), "programs": expansions}
        elif action == "compile_offline":
            if payload:
                raise ValueError("Offline compilation has no configuration payload")
            if (old.get('plan') or {}).get('acceptance')=='code_review':
                raise ValueError('The whole-EGP code-review path uses semantic tasks and independent AI review; select the legacy mode explicitly for limited offline compilation')
            from .offline import compile_project
            result = compile_project(self, old)
            new["offline"] = result
            new.update(candidate=None, evidence=None, gate2=None)
            if result["candidate"]:
                candidate = parse_json(Candidate, json.dumps(result["candidate"]))
                obligations = [r["id"] for r in old["context"]["regions"] if r["kind"] != "layout_or_comment"]
                evidence = verify(old["task"]["hash"], parse_json(Plan, json.dumps(old["plan"])), candidate, obligations)
                new.update(candidate=result["candidate"], evidence=evidence,
                    state="CANDIDATE_READY" if evidence["static_passed"] else "VALIDATION_FAILED")
            else:
                new["state"] = "OFFLINE_UNSUPPORTED"
        elif action == "save_intake":
            from .plan_advisor import Intake
            intake=parse_json(Intake,json.dumps(payload))
            if new.get('scope')!='entire_egp':raise ValueError('Confirm all project scenarios before AI planning')
            new['parameters']=intake.parameters
            new['context']=task_context(new['snapshot'],new['scenario'],self.vault,intake.parameters,new['manual_inputs'],includes=new.get('include_sources',()))
            registry={i['id'] for i in new['context']['inputs']}
            if set(b.source_id for b in intake.bindings)!=registry-set(intake.intermediate_inputs) or set(intake.intermediate_inputs)-registry:
                raise ValueError('Source bindings and declared intermediates must cover the current input registry')
            if any(not b.columns or not b.schema_complete or b.metadata_status!='user_supplied' for b in intake.bindings):raise ValueError('Confirm complete input fields/types before AI planning')
            new.update(intake=intake.model_dump(mode='json'),plan=None,blockers=[])
            invalidate(new)
        elif action == "apply_plan_proposal":
            proposal=old.get('plan_proposal')
            if payload or not proposal or not proposal['applicable'] or proposal['source_snapshot_hash']!=old['snapshot']['hash'] or proposal['intake_hash']!=digest(old.get('intake')):
                raise ValueError('A current independently reviewed contract proposal is required')
            intake=old['intake']
            body={'scenario_id':old['scenario']['id'],'acceptance':'code_review','mode':'fixed_run','target':'greenplum',
                'target_version':intake['target_versions'].get('greenplum','unknown'),'target_versions':intake['target_versions'],
                'targets':['greenplum','postgresql','clickhouse'],'bindings':intake['bindings'],'parameters':intake['parameters'],
                'source_semantics':intake['source_semantics'],'intermediate_inputs':intake['intermediate_inputs'],
                'parameter_types':old.get('parameter_types',{}),
                'test_mode':intake.get('test_mode',False),'output_destinations':intake.get('output_destinations',{}),
                'outputs':proposal['outputs'],'allowed_effects':proposal['allowed_effects'],'scenario_outputs':proposal['scenario_outputs'],'output_sources':proposal.get('output_sources',{}),
                'validation_scope':'Full EGP code, all declared fields and effects; independently reviewed contract proposal; user approval pending.'}
            plan=parse_json(Plan,json.dumps(body));new['plan']=plan.model_dump(mode='json')
            invalidate(new)
            new['plan_proposal_applied']=proposal
            from .task_planner import plan_tasks
            new['task_plan']=plan_tasks(self,new);new['blockers']=plan_issues(plan,new['context'])
            new['state']='PLAN_READY' if not new['blockers'] and not new['task_plan']['requirements'] else 'NEEDS_CONTEXT'
        elif action == "set_project_scope":
            if set(payload)!={"orders"} or not isinstance(payload["orders"],list) or not payload["orders"]:
                raise ValueError("Provide explicit separate scenario orders")
            known={s["id"] for s in old["snapshot"]["sources"]}
            defined=[]
            for item in payload["orders"]:
                if set(item)!={"id","label","source_ids"} or not item["source_ids"] or any(i not in known for i in item["source_ids"]):
                    raise ValueError("Every scenario needs a label, ID and known ordered sources")
                if not isinstance(item["id"],str) or not re.fullmatch(r"[\w-]{1,100}",item["id"]):
                    raise ValueError("Invalid scenario ID")
                defined.append({**item,"origin":"human_confirmed_project_scope"})
            if len({i["id"] for i in defined})!=len(defined) or {i for o in defined for i in o["source_ids"]}!=known:
                raise ValueError("Whole-EGP scope must cover every program in explicit scenarios")
            selected=list(dict.fromkeys(i for o in defined for i in o["source_ids"]))
            scenario={"id":"whole-"+digest(defined)[:16],"label":"Весь EGP: отдельные сценарии","source_ids":selected,"origin":"whole_project_scope"}
            new.update(scope="entire_egp",scope_orders=defined,scenario=scenario,plan=None,parameters={},parameter_types={},manual_inputs=[],blockers=[])
            new["context"]=task_context(old["snapshot"],scenario,self.vault,includes=new.get("include_sources",()))
            invalidate(new)
        elif action == "set_external_parameter":
            if set(payload)!={"name","type","value"} or not isinstance(payload['name'],str) or not re.fullmatch(r"[A-Za-z_]\w*",payload["name"]) or not isinstance(payload["value"],str) or len(payload['value'])>1000:
                raise ValueError("Provide a typed external parameter")
            kind,value=payload["type"],payload["value"]
            from decimal import Decimal
            from datetime import date
            if kind=="integer":int(value)
            elif kind=="decimal":
                if not Decimal(value).is_finite():raise ValueError("Parameter must be finite")
            elif kind=="date":date.fromisoformat(value)
            elif kind=="identifier":
                if not re.fullmatch(r"[A-Za-z_]\w*",value):raise ValueError("Invalid identifier parameter")
            elif kind!="string":raise ValueError("Unknown parameter type")
            name=payload['name'].casefold()
            new['parameters']={k:v for k,v in new['parameters'].items() if k.casefold()!=name}
            new['parameter_types']={k:v for k,v in new.get('parameter_types',{}).items() if k.casefold()!=name}
            new["parameters"][name]=value
            new["parameter_types"][name]=kind
            new["context"]=task_context(old["snapshot"],old["scenario"],self.vault,new["parameters"],old["manual_inputs"],includes=new.get("include_sources",()))
            new.update(plan=None,blockers=[])
            invalidate(new)
        elif action == "grant_model_calls":
            if set(payload)!={"additional_calls","rationale"} or type(payload["additional_calls"]) is not int or not 1<=payload["additional_calls"]<=100 or len(payload["rationale"].strip())<12:
                raise ValueError("Explicit grant requires 1..100 additional calls and rationale")
            new["additional_model_calls"]=old.get("additional_model_calls",0)+payload["additional_calls"]
        elif action in {"replace_task_sql", "request_unit_rereview"}:
            rereview = action == "request_unit_rereview"
            field = "reason" if rereview else "sql"
            value = payload.get(field)
            selected_hash=payload.get("candidate_hash") if rereview else None
            fields={"unit_key",field} | ({"candidate_hash"} if rereview and "candidate_hash" in payload else set())
            if (set(payload) != fields or not isinstance(value,str)
                    or ("candidate_hash" in fields and (not isinstance(selected_hash,str) or not re.fullmatch(r"[0-9a-f]{64}",selected_hash)))
                    or not (12 <= len(value.strip()) <= 2000 if rereview else 0 < len(value) <= 500000)):
                raise ValueError("Укажите задачу и основание повторного ревью" if rereview else "Provide a selected task and complete replacement SQL")
            pipeline=new.get("pipeline") or {}
            unit=pipeline.get("units",{}).get(payload["unit_key"])
            if not unit or not old["gate1"]:raise ValueError("Select an existing task of the approved plan")
            if rereview:
                attempt = next((a for a in reversed(unit["attempts"]) if a.get("candidate")
                                and a.get("static",{}).get("static_passed")
                                and (selected_hash is None or digest(a["candidate"])==selected_hash)), None)
                if not attempt:
                    raise ValueError("Нужен SQL, прошедший статическую проверку")
                unit["rereview_requested"] = {"candidate_hash":digest(attempt["candidate"]),"reason":value.strip()}
                unit.pop("manual_candidate",None)
            else:
                original=unit.get("candidate") or next((a["candidate"] for a in reversed(unit["attempts"]) if a.get("candidate")),None)
                if not original:raise ValueError("Task has no candidate to revise")
                replacement={**original,"artifacts":[{"name":"manual.sql","sql":value}],"assumptions":[]}
                parse_json(Candidate,json.dumps(replacement))
                unit["manual_candidate"]=replacement
                unit.pop("rereview_requested",None)
            unit["passed"]=False
            target=payload['unit_key'].split(':',1)[0]
            profile=pipeline['profiles'][target];profile['passed']=False
            for scenario in profile['scenarios'].values():
                if any(target+':'+u['id']==payload['unit_key'] for u in scenario['units']):scenario['passed']=False
            pipeline["release_allowed"]=False
            new.update(evidence=None,gate2=None,candidate=None,state="NEEDS_HITL")
        elif action == "select_scenario":
            if set(payload) == {"scenario_id"}:
                scenario = next((s for s in old["snapshot"]["scenarios"] if s["id"] == payload["scenario_id"]), None)
                if not scenario:
                    raise ValueError("Unknown scenario")
            elif set(payload) == {"source_ids"} and isinstance(payload["source_ids"], list):
                known = {s["id"] for s in old["snapshot"]["sources"]}
                if not payload["source_ids"] or any(i not in known for i in payload["source_ids"]):
                    raise ValueError("Invalid explicit execution order")
                scenario = {"id": "manual-" + digest(payload["source_ids"])[:16],
                            "label": "Порядок, заданный пользователем", "source_ids": payload["source_ids"],
                            "origin": "human_explicit_order"}
            else:
                raise ValueError("Select an EGP scenario or supply an explicit source order")
            new.update(scenario=scenario, context=task_context(old["snapshot"], scenario, self.vault, includes=new.get("include_sources",())),
                       plan=None, parameters={}, manual_inputs=[], blockers=[],scope="scenario",scope_orders=[])
            invalidate(new)
        elif action == "save_plan":
            if not old["context"]:
                raise ValueError("Select scenario first")
            plan = parse_json(Plan, json.dumps(payload, ensure_ascii=False))
            if plan.acceptance!='code_review' and (plan.test_mode or plan.output_destinations):
                raise ValueError('Destination overrides and testifier require the whole-EGP code-review path')
            if plan.scenario_id != old["scenario"]["id"]:
                raise ValueError("Scenario contract mismatch")
            new["context"] = task_context(old["snapshot"], old["scenario"], self.vault,
                                          plan.parameters, old["manual_inputs"], includes=new.get("include_sources",()))
            new["plan"] = plan.model_dump(mode="json")
            new['parameter_types']=dict(plan.parameter_types)
            new["parameters"] = plan.parameters
            new["blockers"] = plan_issues(plan, new["context"])
            invalidate(new)
            new["state"] = "NEEDS_CONTEXT" if new["blockers"] else "PLAN_READY"
            if plan.acceptance=="code_review":
                from .task_planner import plan_tasks
                new["task_plan"]=plan_tasks(self,new)
                if new["task_plan"]["requirements"]:new["state"]="NEEDS_CONTEXT"
        elif action == "declare_input":
            if not old["context"]:
                raise ValueError("Select scenario first")
            declaration = parse_json(ManualInput, json.dumps(payload))
            new["manual_inputs"].append(declaration.model_dump(mode="json"))
            new["context"] = task_context(old["snapshot"], old["scenario"], self.vault,
                                          old["parameters"], new["manual_inputs"], includes=new.get("include_sources",()))
            if len(new["context"]["inputs"]) == len(old["context"]["inputs"]):
                raise ValueError("Input already present")
            new["plan"] = None
            new["blockers"] = []
            invalidate(new)
        elif action == "set_parameters":
            if not old["context"] or set(payload) != {"parameters"} or not isinstance(payload["parameters"], dict):
                raise ValueError("Provide explicit parameters for selected scenario")
            params = payload["parameters"]
            if len(params) > 200 or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_]\w*", k)
                                       or not isinstance(v, str) or len(v) > 1000 for k, v in params.items()):
                raise ValueError("Invalid parameter name or value")
            if len({k.casefold() for k in params}) != len(params):
                raise ValueError("Duplicate case-insensitive parameter")
            new.update(parameters=params, plan=None, blockers=[],
                       context=task_context(old["snapshot"], old["scenario"], self.vault, params, old["manual_inputs"], includes=new.get("include_sources",())))
            invalidate(new)
        elif action == "approve_plan":
            if payload or not old["plan"]:
                raise ValueError("A saved plan is required")
            plan = parse_json(Plan, json.dumps(old["plan"]))
            blockers = plan_issues(plan, old["context"])
            if blockers:
                raise ValueError("Plan unresolved: " + "; ".join(blockers[:8]))
            if plan.acceptance=="code_review":
                from .task_planner import plan_tasks
                if old.get("scope")!="entire_egp":
                    raise ValueError("Confirm the whole EGP as separate scenario orders")
                if set(plan.targets)!={"greenplum","postgresql","clickhouse"}:
                    raise ValueError("Whole-EGP code package requires all three target profiles")
                planning=plan_tasks(self,old)
                if planning["requirements"]:raise ValueError("Task plan has unresolved requirements; open preparation")
                if len(old.get("scope_orders",[]))>1 and set(plan.scenario_outputs)!={o["id"] for o in old["scope_orders"]}:
                    raise ValueError("Declare outputs separately for every scenario")
                names={o.name.key for o in plan.outputs}
                if any(set(v)-names for v in plan.scenario_outputs.values()) or (plan.scenario_outputs and set(n for v in plan.scenario_outputs.values() for n in v)!=names):
                    raise ValueError("Scenario outputs must cover exactly the agreed result registry")
                new["task_plan"]=planning
            task = {"schema_version": 1, "id": uuid.uuid4().hex, "approved_revision": revision + 1,
                    "snapshot_hash": old["snapshot"]["hash"],
                    "context": old["context"], "plan": old["plan"],
                    "strategy_version": "local-review-1", "rule_version": "not_applied"}
            task["hash"] = digest(task)
            new.update(task=task, gate1={"task_hash": task["hash"], "actor": "local_user", "at": now()},
                       candidate=None, evidence=None, gate2=None, state="PLAN_APPROVED")
        elif action == "submit_candidate":
            if set(payload) != {"candidate_json"} or not isinstance(payload["candidate_json"], str):
                raise ValueError("Submit complete candidate JSON")
            if len(payload["candidate_json"].encode("utf-8")) > 2_000_000:
                raise ValueError("Candidate exceeds local limit")
            if not old["gate1"] or not old["task"]:
                raise ValueError("Approve current plan first")
            candidate = parse_json(Candidate, payload["candidate_json"])
            if candidate.task_hash != old["task"]["hash"]:
                raise Conflict("Candidate belongs to a different task")
            obligations = list(dict.fromkeys(r["id"] for r in old["context"]["regions"]
                                            if r["kind"] != "layout_or_comment"))
            evidence = verify(old["task"]["hash"], parse_json(Plan, json.dumps(old["plan"])), candidate, obligations)
            new.update(candidate=candidate.model_dump(mode="json"), evidence=evidence, gate2=None,
                       state="CANDIDATE_READY" if evidence["static_passed"] else "VALIDATION_FAILED")
            new["pipeline"]=None # Manual SQL needs fresh independent AI review.
        elif action == "retire_test_evidence":
            evidence_id = payload.get("id")
            reason = payload.get("reason")
            if set(payload) != {"id", "reason"} or not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 2000:
                raise ValueError("Укажите причину пересмотра свидетельства")
            records = old.get("quality_evidence", []) + old.get("unit_quality_failures", [])
            if not any(digest(record) == evidence_id for record in records) or evidence_id in old.get("retired_test_evidence", {}):
                raise ValueError("Свидетельство отсутствует или уже пересмотрено")
            new.setdefault("retired_test_evidence", {})[evidence_id] = {"reason":reason.strip(),"revision":revision + 1}
            new.update(gate2=None, state="NEEDS_HITL")
        elif action == "approve_release":
            from .linker import VERSION
            with closing(self.connect()) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                    if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','cancelling')", (ident,)).fetchone():
                        raise ValueError("Wait for the active job before accepting a release")
            evidence = old.get("evidence") or {}
            if old["plan"] and old["plan"].get("acceptance")=="code_review":
                pipeline=old.get("pipeline") or {}
                if payload or not pipeline.get("release_allowed") or not evidence.get('release_allowed') or not self.code_review_current(old):
                    raise ValueError("Code release requires current linked static and independent AI reviews")
                if self._known_test_failure(old, pipeline):
                    raise ValueError('A known test-data counterexample blocks this candidate')
                if pipeline.get("task_hash")!=old["task"]["hash"] or evidence.get("candidate_hash")!=digest(old["candidate"]) or evidence.get("pipeline_hash")!=pipeline.get("evidence_hash"):
                    raise Conflict("Code review is stale")
                new.pop("stale_gate2",None)
                new.update(gate2={"task_hash":old["task"]["hash"],"candidate_hash":digest(old["candidate"]),"evidence_hash":evidence["hash"],"actor":"local_user","at":now(),"basis":"code_review_not_data_equivalence"},state="CODE_ACCEPTED")
                return self._save_command(old,new,action)
            if evidence.get("verifier_version") != "complete-schema-exact-number-3":
                raise ValueError("Verification is stale; rerun with the current verifier")
            if payload or not evidence.get("release_allowed") or evidence.get("checks", {}).get("equivalence") != "passed":
                raise ValueError("Release blocked: independent SAS comparison for the same inputs is required")
            if evidence.get("task_hash") != old["task"]["hash"] or evidence.get("candidate_hash") != digest(old["candidate"]):
                raise Conflict("Verification is stale")
            new.update(gate2={"task_hash": old["task"]["hash"], "candidate_hash": digest(old["candidate"]),
                              "evidence_hash": evidence["hash"], "actor": "local_user", "at": now()}, state="ACCEPTED")
        else:
            raise ValueError("Unknown command")
        return self._save_command(old,new,action)

    def _save_command(self,old,new,action,*,actor="local_user"):
        ident,revision=old["id"],old["revision"]
        new["revision"] = revision + 1
        if (new.get('plan') or {}).get('acceptance')=='code_review' or new.get('scope')=='entire_egp':new['schema_version']=2
        new["preparation"] = prepare(new)
        if new.get('plan'):
            from .destinations import resolve
            new['destination_preview']=resolve(new['plan'])['registry']
        else:new.pop('destination_preview',None)
        new["events"].append({"revision": new["revision"], "action": action, "actor": actor, "at": now()})
        # Expensive analysis/verification above is outside the write transaction.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT MAX(revision) FROM revisions WHERE id=?", (ident,)).fetchone()[0]
            if current != revision:
                raise Conflict("Project changed during operation")
            db.execute("INSERT INTO revisions VALUES (?,?,?)", (ident, new["revision"], json.dumps(new, ensure_ascii=False)))
        return new

    @staticmethod
    def _test_scope(project):
        return digest([project["snapshot"]["hash"], project["plan"]])

    @staticmethod
    def code_review_current(project):
        from .ai_pipeline import VERSION, rules_fingerprint
        pipeline=project.get("pipeline") or {}
        evidence=project.get("evidence") or {}
        return (pipeline.get("version")==VERSION==evidence.get("verifier_version")
                and pipeline.get("rules_fingerprint")==rules_fingerprint()==evidence.get("rules_fingerprint"))

    def current_test_evidence(self, project, target, policy_hash):
        scope=self._test_scope(project)
        identity=self._profile_sql_identity(project.get("pipeline") or {"candidate":project.get("candidate")},target)
        return [r for r in project.get("quality_evidence",[]) if r["target"]==target
                and r.get("scope")==scope and r.get("sql_identity")==identity
                and r.get("policy_hash")==policy_hash and digest(r) not in project.get("retired_test_evidence",{})]

    @staticmethod
    def _sql_identity(candidate):
        return digest([artifact["sql"] for artifact in (candidate or {}).get("artifacts", [])])

    @classmethod
    def _profile_sql_identity(cls, pipeline, target):
        from .linker import assemble_sql
        scenarios = pipeline.get("profiles", {}).get(target, {}).get("scenarios", {})
        if scenarios:
            return digest({name:assemble_sql(scenario["artifacts"], target)["sha256"]
                           for name, scenario in scenarios.items()})
        return cls._sql_identity(pipeline.get("candidate"))

    def _known_test_failure(self, project, result):
        scope = self._test_scope(project)
        retired = project.get("retired_test_evidence", {})
        records = project.get("quality_evidence", []) + project.get("unit_quality_failures", [])
        for record in records:
            if record.get("matched") or digest(record) in retired or record.get("scope", scope) != scope:
                continue
            if "unit_key" in record:
                unit = result.get("units", {}).get(record["unit_key"], {})
                if not unit.get("passed"):
                    continue
                candidate = unit.get("candidate")
                sql_identity = self._sql_identity(candidate)
            else:
                candidate = result.get("candidate")
                sql_identity = self._profile_sql_identity(result, record["target"])
            if candidate and (record.get("sql_identity") == sql_identity
                              or record["candidate_hash"] == digest(candidate)):
                return True
        return False

    def record_pipeline(self, ident, revision, result):
        """Worker-only result application; no HTTP command can fabricate AI evidence."""
        old=self.get(ident)
        if old["revision"]!=revision or not old["task"] or result["task_hash"]!=old["task"]["hash"]:
            raise Conflict("Project changed during AI pipeline")
        new=copy.deepcopy(old)
        new.update(pipeline=result,candidate=result["candidate"],gate2=None,
            state="CODE_REVIEW_READY" if result["release_allowed"] else "NEEDS_HITL")
        evidence={"verifier_version":result["version"],"rules_fingerprint":result.get("rules_fingerprint"),"task_hash":old["task"]["hash"],
            "candidate_hash":digest(result["candidate"]),"pipeline_hash":result["evidence_hash"],
            "static_passed":bool(result["profiles"]) and all(s["link"]["static_passed"] for p in result["profiles"].values() for s in p["scenarios"].values()) and all(p["scenarios"] for p in result["profiles"].values()),
            "release_allowed":result["release_allowed"],"errors":[r["message"] for r in result["requirements"]],
            "checks":{"execution":"not_run","equivalence":"not_run","code_review":"passed" if result["release_allowed"] else "failed"},
            "notes":["Code review only; target execution and SAS result comparison not performed"]}
        evidence["hash"]=digest(evidence);new["evidence"]=evidence
        if self._known_test_failure(old, result):
            evidence['release_allowed']=False;evidence['checks']['test_data']='failed'
            evidence.pop('hash',None);evidence['hash']=digest(evidence)
            new['state']='VALIDATION_FAILED'
        return self._save_command(old,new,"record_ai_pipeline",actor="pipeline_worker")

    def review_policy_hash(self,checked):
        if checked.get('policy_hash'):return checked['policy_hash']
        reservation=(checked.get('provenance') or {}).get('reservation')
        if not reservation:return None
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_exchanges'").fetchone():return None
            row=db.execute('SELECT request FROM model_exchanges WHERE id=?',(reservation,)).fetchone()
        if not row:return None
        return digest(json.loads(row[0])['messages'][0]['content'])

    def cached_assembly_review(self,ident,revision,scope_hash,candidate_hash,policy_hash):
        """Latest matching immutable review, never an older pass over a newer failure."""
        from .ai_pipeline import VERSION, rules_fingerprint
        with closing(self.connect()) as db:
            rows=db.execute('SELECT revision,body FROM revisions WHERE id=? AND revision<=? ORDER BY revision DESC',(ident,revision))
            for rev,raw in rows:
                pipeline=json.loads(raw).get('pipeline') or {}
                for profile in pipeline.get('profiles',{}).values():
                    for scenario in profile.get('scenarios',{}).values():
                        checked=scenario.get('assembly_review') or {};review=checked.get('review') or {}
                        if review.get('scope_hash')==scope_hash and review.get('candidate_hash')==candidate_hash:
                            if pipeline.get('version')!=VERSION or pipeline.get('rules_fingerprint')!=rules_fingerprint() or review.get('verdict')!='pass' or self.review_policy_hash(checked)!=policy_hash:return None
                            return {**checked,'reuse_from_revision':rev}
        return None

    def record_test_evidence(self,ident,revision,result):
        """Worker-only independent test observation. It cannot be supplied by a model/HTTP command."""
        old=self.get(ident)
        required={'task_hash','candidate_hash','dataset_hash','oracle_hash','target','matched','summary'}
        if not required <= set(result) <= required | {'environment','policy_hash'} or type(result['matched']) is not bool or result['target'] not in {'greenplum','postgresql','clickhouse'}:
            raise ValueError('Invalid independent test evidence')
        if old['revision']!=revision or not old.get('candidate') or result['task_hash']!=old['task']['hash'] or result['candidate_hash']!=digest(old['candidate']):
            raise Conflict('Independent test evidence is stale')
        if any(not re.fullmatch(r'[0-9a-f]{64}',result[k]) for k in ('dataset_hash','oracle_hash')):raise ValueError('Test evidence needs dataset and oracle hashes')
        if 'policy_hash' in result and (not isinstance(result['policy_hash'],str) or not re.fullmatch(r'[0-9a-f]{64}',result['policy_hash'])):
            raise ValueError("Invalid comparison policy fingerprint")
        environment = result.get("environment", {})
        if not isinstance(environment, dict) or len(json.dumps(environment)) > 4000:
            raise ValueError("Invalid test environment description")
        new=copy.deepcopy(old)
        target = result["target"]
        version = old["plan"].get("target_versions", {}).get(target)
        record={**result,"environment":{"target_version":version, **environment},
                "scope":self._test_scope(old),"sql_identity":self._profile_sql_identity(old.get("pipeline") or {"candidate":old["candidate"]}, target),
                "verifier_version":(old.get("pipeline") or {}).get("version"),"recorded_revision":revision}
        new.setdefault('quality_evidence',[]).append(record)
        if not result['matched']:
            new['gate2']=None;new['state']='VALIDATION_FAILED'
            evidence=new.get('evidence') or {}
            evidence['release_allowed']=False;evidence.setdefault('checks',{})['test_data']='failed'
            evidence.setdefault('errors',[]).append('Independent synthetic test mismatch: '+result['target'])
            evidence.pop('hash',None);evidence['hash']=digest(evidence);new['evidence']=evidence
        return self._save_command(old,new,'record_independent_test',actor="verification_worker")

    def record_unit_test_failure(self,ident,revision,unit_key,candidate_hash,observation):
        """Trusted local worker feedback, bound to the exact SQL unit. Never an HTTP/model command."""
        old=self.get(ident);unit=(old.get('pipeline') or {}).get('units',{}).get(unit_key)
        if old['revision']!=revision or not unit or not unit.get('passed') or digest(unit.get('candidate'))!=candidate_hash:
            raise Conflict('Unit test failure does not belong to a current reviewed SQL unit')
        if not {'dataset_hash','message'} <= set(observation) <= {'dataset_hash','message','oracle_hash','environment'} or not re.fullmatch(r'[0-9a-f]{64}',observation['dataset_hash']) or not isinstance(observation['message'],str) or not 1<=len(observation['message'])<=2000:
            raise ValueError('Invalid bounded unit execution feedback')
        if 'oracle_hash' in observation and not re.fullmatch(r'[0-9a-f]{64}',observation['oracle_hash']):
            raise ValueError('Invalid oracle fingerprint')
        environment = observation.get("environment", {})
        if not isinstance(environment, dict) or len(json.dumps(environment)) > 4000:
            raise ValueError("Invalid test environment description")
        from .linker import finding
        new=copy.deepcopy(old);pipeline=new['pipeline'];changed=pipeline['units'][unit_key]
        new.setdefault('unit_quality_failures',[]).append({'unit_key':unit_key,'candidate_hash':candidate_hash,**observation,
            'scope':self._test_scope(old),'sql_identity':self._sql_identity(unit["candidate"]),'verifier_version':pipeline.get("version"),
            'recorded_revision':revision,'environment':{'target_version':old['plan'].get('target_versions', {}).get(unit_key.split(':', 1)[0]), **environment}})
        # Retain candidate, static checks, review and provenance. The new failure
        # forces a real repair; dependent unit hashes bind the changed predecessor.
        changed['passed']=False;changed['attempts'][-1]['findings'].append({**finding('target_execution_failed',observation['message']),
            'dataset_hash':observation['dataset_hash'],'candidate_hash':candidate_hash,'origin':'local_test_worker'})
        target=unit_key.split(':',1)[0];pipeline['profiles'][target]['passed']=False
        for scenario in pipeline['profiles'][target]['scenarios'].values():scenario['passed']=False
        pipeline['release_allowed']=False
        new.update(gate2=None,evidence=None,candidate=None,state='NEEDS_HITL')
        return self._save_command(old,new,'record_unit_test_failure',actor="verification_worker")

    def record_plan_proposal(self,ident,revision,result):
        old=self.get(ident)
        if old['revision']!=revision or result['source_snapshot_hash']!=old['snapshot']['hash'] or result['intake_hash']!=digest(old.get('intake')):
            raise Conflict('Project changed during AI planning')
        new=copy.deepcopy(old);new['plan_proposal']=result
        return self._save_command(old,new,'record_plan_proposal')

    def record_execution(self, ident, revision, evidence):
        """Internal worker entry point; HTTP users/models cannot submit verification claims."""
        old = self.get(ident)
        if old["revision"] != revision:
            raise Conflict("Project changed during execution")
        if not old["task"] or not old["candidate"] or evidence["task_hash"] != old["task"]["hash"] or evidence["candidate_hash"] != digest(old["candidate"]):
            raise Conflict("Execution does not belong to current candidate")
        new = copy.deepcopy(old)
        new.update(evidence=evidence, gate2=None, revision=revision + 1,
                   state="REVIEW_READY" if evidence["release_allowed"] else
                         ("REFERENCE_MATCHED" if evidence["checks"].get("comparison") == "passed" else "VALIDATION_FAILED"))
        new["events"].append({"revision": new["revision"], "action": "record_execution", "actor": "verification_worker", "at": now()})
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT MAX(revision) FROM revisions WHERE id=?", (ident,)).fetchone()[0] != revision:
                raise Conflict("Project changed during operation")
            db.execute("INSERT INTO revisions VALUES (?,?,?)", (ident, new["revision"], json.dumps(new, ensure_ascii=False)))
        return new

    def export_draft(self, project):
        if project.get("pipeline"):
            return self.export_code_package(project)
        if not project["candidate"]:
            raise ValueError("No candidate to export")
        accepted = bool(project.get("gate2")) and (project.get("evidence") or {}).get("verifier_version") == "complete-schema-exact-number-3"
        manifest = {"status": "accepted_fixed_run" if accepted else "unverified_draft", "release_allowed": accepted,
                    "project_id": project["id"], "revision": project["revision"],
                    "task_hash": project["task"]["hash"], "candidate_hash": digest(project["candidate"]),
                    "plan": project["plan"], "evidence": project["evidence"], "gate2": project.get("gate2"),
                    "source_snapshot_hash": project["snapshot"]["hash"],
                    "session_policy": "all SQL artifacts in order, in one uninterrupted session"}
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for a in project["candidate"]["artifacts"]:
                archive.writestr("sql/" + a["name"], a["sql"].encode("utf-8"))
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        return out.getvalue()

    def export_code_package(self,project):
        pipeline=project["pipeline"]
        from .destinations import resolve
        from .linker import VERSION, assemble_sql
        destinations=resolve(project['plan'])
        physical=destinations['plan']
        accepted=bool(project.get("gate2")) and self.code_review_current(project) and pipeline.get("release_allowed") and project["gate2"].get("candidate_hash")==digest(project.get("candidate"))
        manifest={"schema_version":2,"status":"accepted_code_not_executed" if accepted else "incomplete_code_draft",
            "release_allowed":bool(accepted),"task_hash":pipeline["task_hash"],"pipeline_hash":pipeline["evidence_hash"],
            "rules_fingerprint":pipeline.get("rules_fingerprint"),
            "source_snapshot_hash":project["snapshot"]["hash"],"entrypoints":[],"execution":"not_run","sas_comparison":"not_run",
            "parameters":project["plan"]["parameters"],"parameter_types":project["plan"].get("parameter_types",{}),
            "bindings":project["plan"]["bindings"],"gate1":project["gate1"],"gate2":project.get("gate2"),
            'outputs':physical['outputs'],'allowed_effects':physical['allowed_effects'],
            'output_sources':physical.get('output_sources',{}),'scenario_outputs':physical.get('scenario_outputs',{}),
            'testifier':{'enabled':destinations['test_mode'],'destinations':destinations['registry'],'initial_copies':destinations['seeds']},
            'source_semantics':project['plan']['source_semantics'],
            "requirements":pipeline["requirements"],"profiles":{}}
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
            for target,profile in pipeline["profiles"].items():
                manifest["profiles"][target]={"code_passed":profile["passed"],"execution":"not_run",
                    "target_version":project["plan"].get("target_versions",{}).get(target,project["plan"]["target_version"] if target==project["plan"]["target"] else "unknown")}
                for i,(sid,scenario) in enumerate(profile["scenarios"].items()):
                    path=f"sql/{target}/scenario_{i+1:03d}.sql"
                    assembled = assemble_sql(scenario["artifacts"], target)
                    entrypoint = scenario.get("entrypoint")
                    if accepted and entrypoint != assembled:
                        raise Conflict("Проверенный файл сценария не совпадает со сборкой")
                    combined = (entrypoint or assembled)["sql"]
                    if combined:z.writestr(path,combined.encode("utf-8"))
                    manifest["entrypoints"].append({"scenario_id":sid,"target":target,"path":path if combined else None,
                        "sha256":(entrypoint or assembled)["sha256"] if combined else None,
                        "code_passed":scenario["passed"] and entrypoint == assembled,
                        "session":"one uninterrupted session per scenario; scenarios are alternatives"})
            z.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False,indent=2))
            z.writestr('temporary/registry.json',json.dumps([r for r in destinations['registry'] if r['work_default'] or r['storage']=='temporary'],ensure_ascii=False,indent=2))
            z.writestr('temporary/README.md','# WORK и временные таблицы\n\nСписок находится в registry.json. По умолчанию это TEMP-таблицы SQL-сессии, не файлы на диске. Явное назначение схемы/БД делает таблицу постоянной; итоговый storage указан для каждой записи. Все шаги выбранного сценария выполняются в одной сессии. Изменения назначения и testifier записаны в manifest.json.\n')
            task_plan=project.get('task_plan') or {}
            z.writestr("source-map.json",json.dumps({'classifications':task_plan.get('classifications',[]),
                'tasks':[{k:v for k,v in u.items() if k in {'id','scenario_id','source_id','source_hash','start','end','line','end_line','statement_map','obligations','dependencies'}} for u in task_plan.get('tasks',[])],
                'sql_units':{k:{'hash':u['hash'],'candidate_hash':digest(u.get('candidate')),'artifacts':[a['name'] for a in (u.get('candidate') or {}).get('artifacts',[])]} for k,u in pipeline['units'].items()}},ensure_ascii=False,indent=2))
            if project.get('plan_proposal_applied'):z.writestr('plan-proposal-review.json',json.dumps(project['plan_proposal_applied'],ensure_ascii=False,indent=2))
            z.writestr("code-reviews.json",json.dumps(pipeline,ensure_ascii=False,indent=2))
            z.writestr("RUN.md","# Запуск пакета\n\nВыберите один целевой диалект и один сценарий из manifest.json. Не склеивайте альтернативные сценарии. Выполните соответствующий SQL-файл в одной сессии с согласованными источниками и параметрами. Проверки кода не подтверждают исполнение или совпадение данных с SAS. Секреты подключений задаются отдельно.\n")
        return out.getvalue()


def invalidate(project):
    project.update(gate1=None, task=None, candidate=None, evidence=None, gate2=None,
                   offline=None, macro_analysis=None, pipeline=None,task_plan=None,plan_proposal=None,plan_proposal_applied=None,state="NEEDS_CONTEXT")


def now():
    return datetime.now(timezone.utc).isoformat()
