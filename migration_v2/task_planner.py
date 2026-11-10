"""Lossless semantic task boundaries over ORIGINAL SAS; no prerequisite macro evaluator."""
from __future__ import annotations
import re
from .context import analyze_source
from .contracts import digest
from .dataset_syntax import dataset_list
from .includes import include_index, attached_include

EXTERNAL_ENV = re.compile(r'%let\s+(\w+)\s*=\s*%sysget\(\s*([A-Za-z_]\w*)\s*\)\s*;', re.I)

MACRO_BUILTINS=set("macro mend let local global if then else do end to by while until return goto label put include sysfunc qsysfunc eval sysevalf str nrstr bquote nrbquote quote nrquote superq unquote scan qscan substr qsubstr length qlength upcase qupcase lowcase symexist symglobl symlocal sysget abort display input window inc list symdel copy".split())


def orders(project):
    return project.get("scope_orders") or ([project["scenario"]] if project.get("scenario") else [])


def plan_tasks(wb, project):
    if not project.get("plan") or not project.get("scenario"):
        raise ValueError("Save the context and outputs before planning tasks")
    sources = {s["id"]: s for s in project["snapshot"]["sources"]+project.get('include_sources',[])}
    analyses = {i: analyze_source(s, wb.vault) for i,s in sources.items()}
    tasks, classifications, requirements = [], [], []
    include_map=include_index(project.get('include_sources',[]), project['plan']['parameters'])
    def include_context(parent,stmt,stack=()):
        attachment=attached_include(include_map,parent,stmt)
        if not attachment:
            requirements.append({'code':'include_source_required','source_id':parent['id'],'offset':stmt['offset'],
                'line':stmt['line'],'reference':stmt['text'],'actions':['attach_include']});return []
        if len(stack)>=16 or attachment['sha256'] in stack:
            requirements.append({'code':'include_cycle_or_depth','source_id':parent['id'],'offset':stmt['offset']});return []
        text=wb.vault.text(attachment)
        from app.sas_lexer import lex_statements
        children,cutoff=lex_statements(text)
        if cutoff is not None:
            requirements.append({'code':'include_lexical_cutoff','source_id':attachment['id'],'line':cutoff,'actions':['attach_include']})
        for region in analyses[attachment["id"]]["regions"]:
            if region["kind"] == "macro" and region.get("termination") != "%mend":
                requirements.append({"code":"macro_definition_incomplete","source_id":attachment["id"],"region_id":region["id"]})
        contexts=[{**attachment,'text':text}]
        for child in children:
            if re.match(r'%include\b',child['masked'].strip(),re.I):
                contexts.extend(include_context(attachment,child,stack+(attachment['sha256'],)))
        return contexts

    def included_regions(items):
        """Yield nested regions in lexical execution order, not attachment order."""
        ids = {item["id"] for item in items}
        children = {(item["parent_source_id"],item["offset"]):item for item in items}

        def visit(item, stack):
            if item["id"] in stack:
                return
            for region in analyses[item["id"]]["regions"]:
                yield item, region
                child = children.get((item["id"], region["start"]))
                if child and region["kind"] != "macro":
                    yield from visit(child, (*stack,item["id"]))

        for item in items:
            if item["parent_source_id"] not in ids:
                yield from visit(item, ())
    for order in orders(project):
        definitions, assignments, previous = {}, [], None
        for source_id in order["source_ids"]:
            source = sources.get(source_id)
            if not source or source["status"] != "available":
                requirements.append({"code":"source_required", "source_id":source_id,"scenario_id":order["id"]})
                continue
            text = wb.source_text(project, source_id)
            regions = analyses[source_id]["regions"]
            pending, depth = [], 0
            for region_index,region in enumerate(regions):
                fragment = text[region["start"]:region["end"]]
                classification = {"scenario_id":order["id"],"source_id":source_id,"region_id":region["id"],
                    "start":region["start"],"end":region["end"],"line":region["line"],"sha256":region["sha256"]}
                if region["kind"] == "layout_or_comment":
                    classifications.append({**classification,"role":"layout_or_comment"})
                    continue
                if region["kind"] == "macro" and not pending:
                    match = re.search(r"%macro\s+(\w+)", fragment,re.I)
                    if not match or region.get("termination") != "%mend":
                        requirements.append({"code":"macro_definition_incomplete","source_id":source_id,"region_id":region["id"]})
                    else:
                        definitions[match[1].casefold()]={**classification,"text":fragment}
                    classifications.append({**classification,"role":"macro_definition"})
                    continue
                if not pending and re.match(r'\s*(?:options|goptions|libname|%global)\b',fragment,re.I):
                    # Declarations are preserved as context, not deleted or executed.
                    # Data-affecting options must be honored by subsequent translations.
                    assignments.append({**classification,'text':fragment,'role':'sas_environment_declaration'})
                    classifications.append({**classification,'role':'manual_connection_mapping' if re.match(r'\s*libname\b',fragment,re.I) else 'execution_environment_context'})
                    continue
                if not pending and region.get('procedure')=='format' and not re.search(r'\bcntl(?:in|out)\s*=',fragment,re.I):
                    assignments.append({**classification,'text':fragment,'role':'sas_format_definition'})
                    classifications.append({**classification,'role':'format_definition_context'})
                    continue
                quote_assignment=re.fullmatch(r"\s*%let\s+\w+\s*=\s*%(?:nrstr|str)\s*\(.*\)\s*;\s*",fragment,re.I|re.S)
                if not pending and (re.fullmatch(r"\s*%let\s+\w+\s*=\s*[^%&;]*;\s*",fragment,re.I|re.S) or quote_assignment):
                    assignments.append({**classification,"text":fragment})
                    classifications.append({**classification,"role":"compile_time_assignment"})
                    continue
                if not pending and region["kind"] == "boundary":
                    classifications.append({**classification,"role":"sas_step_boundary"})
                    continue
                external = EXTERNAL_ENV.fullmatch(fragment.strip()) if not pending else None
                if external:
                    name, environment = external.groups()
                    if name.casefold() not in {k.casefold() for k in project["plan"]["parameters"]}:
                        requirements.append({"code":"external_parameter_required","name":name,
                            "environment_variable":environment,"source_id":source_id,
                            "scenario_id":order["id"],"line":region["line"],"actions":["set_external_parameter"]})
                    assignments.append({**classification,"text":fragment,"role":"confirmed_external_assignment",
                                        "parameter":name})
                    classifications.append({**classification,"role":"external_parameter_context"})
                    continue
                pending.append((region,classification))
                # Masking prevents macro-quoted text, strings and comments from opening controls.
                from app.sas_lexer import lex_statements
                statements,_=lex_statements(fragment)
                control=" ".join(s["masked"] for s in statements)
                depth += len(re.findall(r"%do\b",control,re.I))-len(re.findall(r"%end\b",control,re.I))
                if depth > 0: continue
                next_region=next((r for r in regions[region_index+1:] if r['kind']!='layout_or_comment'),None)
                if len(pending)==1 and region.get('procedure')=='sort' and next_region and next_region['kind']=='data':
                    following=' '.join(s['masked'] for s in analyses[source_id]['statements'] if next_region['start']<=s['offset']<next_region['end'])
                    if re.search(r'\bby\b|\b(?:first|last)\.',following,re.I):continue
                if depth < 0:
                    requirements.append({"code":"unbalanced_macro_control","source_id":source_id,"region_id":region["id"]})
                first,last=pending[0][0],pending[-1][0]
                task_source=text[first["start"]:last["end"]]
                included=[]
                include_requirements = len(requirements)
                for stmt in analyses[source_id]['statements']:
                    if first['start']<=stmt['offset']<last['end'] and re.match(r'%include\b',stmt['masked'].strip(),re.I):
                        included.extend(include_context(source,stmt))
                wanted={m.casefold() for m in re.findall(r"%([A-Za-z_]\w*)",control)}
                for unknown in wanted-definitions.keys()-MACRO_BUILTINS:
                    requirements.append({"code":"macro_definition_required","source_id":source_id,"name":unknown,
                        "region_id":first["id"],"scenario_id":order["id"]})
                selected={}
                while wanted - selected.keys():
                    name=(wanted-selected.keys()).pop()
                    if name not in definitions:
                        wanted.remove(name);continue
                    definition=definitions[name];selected[name]=definition
                    definition_stmts,_=lex_statements(definition['text'])
                    invoked={m.casefold() for m in re.findall(r'%([A-Za-z_]\w*)',' '.join(s['masked'] for s in definition_stmts))}
                    for missing in invoked-definitions.keys()-MACRO_BUILTINS:
                        requirements.append({'code':'macro_definition_required','source_id':definition['source_id'],'name':missing,
                            'region_id':definition['region_id'],'scenario_id':order['id']})
                    wanted.update(invoked & definitions.keys())
                for definition in selected.values():
                    for stmt in analyses[definition['source_id']]['statements']:
                        if definition['start']<=stmt['offset']<definition['end'] and re.match(r'%include\b',stmt['masked'].strip(),re.I):
                            included.extend(include_context(sources[definition['source_id']],stmt))
                pure_include=bool(included) and len(requirements)==include_requirements and len(pending)==1 and re.match(r'\s*%include\b',task_source,re.I)
                for child in included:
                    child_analysis=analyses[child['id']]
                    if any(r['kind'] not in {'macro','layout_or_comment','boundary'}
                           and not re.fullmatch(r"\s*%include\s+(['\"]).*?\1\s*;\s*",child['text'][r['start']:r['end']],re.I|re.S)
                           for r in child_analysis['regions']):pure_include=False
                if pure_include:
                    classifications.extend({**c,'role':'included_definition_context'} for _,c in pending)
                    for child,r in included_regions(included):
                        child_fragment=child['text'][r['start']:r['end']]
                        role = 'included_macro_definition' if r['kind']=='macro' else (
                            'included_definition_context' if child_fragment.lstrip().lower().startswith('%include') else 'layout_or_comment')
                        classifications.append({'scenario_id':order['id'],'source_id':child['id'],'region_id':r['id'],
                            'start':r['start'],'end':r['end'],'sha256':r['sha256'],'role':role})
                        if r['kind']=='macro':
                            name=re.search(r'%macro\s+(\w+)',child_fragment,re.I)
                            if name:definitions[name[1].casefold()]={**r,'source_id':child['id'],'region_id':r['id'],'text':child_fragment}
                    pending=[];depth=0;continue
                unit={"scenario_id":order["id"],"source_id":source_id,"source_hash":source["sha256"],
                    "start":first["start"],"end":last["end"],"line":first["line"],"end_line":last["end_line"],
                    "source":task_source,"obligations":[r["id"] for r,_ in pending],
                    "definitions":[selected[k] for k in sorted(selected)],"parameter_assignments":list(assignments),"includes":included,
                    "dependencies":[previous] if previous else [],"kind":first["kind"]}
                unit["id"]="unit-"+digest({k:v for k,v in unit.items() if k!="source"})[:24]
                statement_map=[]
                for stmt in analyses[source_id]["statements"]:
                    if first["start"]<=stmt["offset"]<last["end"]:
                        statement_map.append({"id":unit["id"]+":s:"+str(stmt["offset"]),"source_id":source_id,
                            "start":stmt["offset"],"end":stmt["end_offset"],"line":stmt["line"],"role":"source_statement"})
                for name,definition in selected.items():
                    macro_statements,_=lex_statements(definition["text"])
                    for stmt in macro_statements:
                        statement_map.append({"id":unit["id"]+":macro:"+name+":"+str(stmt["offset"]),
                            "source_id":definition["source_id"],"start":definition["start"]+stmt["offset"],
                            "end":definition["start"]+stmt["end_offset"],"line":definition["line"]+stmt["line"]-1,
                            "role":"macro_invocation_context"})
                for child in included:
                    child_stmts,_=lex_statements(child['text'])
                    statement_map.extend({'id':unit['id']+':include:'+child['id']+':'+str(s['offset']),
                        'source_id':child['id'],'start':s['offset'],'end':s['end_offset'],'line':s['line'],'role':'included_source_statement'} for s in child_stmts)
                unit["statement_map"]=statement_map
                unit["obligations"]=[s["id"] for s in statement_map]
                tasks.append(unit);previous=unit["id"]
                classifications.extend({**c,"role":"translation","task_id":unit["id"]} for _,c in pending)
                # Include definitions become context at this exact execution point.
                for child in included:
                    child_analysis=analyze_source(child,wb.vault)
                    for region_child in child_analysis['regions']:
                        if region_child['kind']=='macro':
                            fragment_child=child['text'][region_child['start']:region_child['end']]
                            macro_name=re.search(r'%macro\s+(\w+)',fragment_child,re.I)
                            if macro_name:definitions[macro_name[1].casefold()]={**region_child,'source_id':child['id'],'region_id':region_child['id'],'text':fragment_child}
                pending=[];depth=0
            if pending:
                requirements.append({"code":"unclosed_control_group","source_id":source_id,"scenario_id":order["id"]})
    represented={(c["scenario_id"],c["source_id"],c["region_id"]) for c in classifications}
    from app.sas_lexer import lex_statements
    supplied={p.casefold() for p in project["plan"]["parameters"]}
    known_by_scenario={}
    for unit in tasks:
        raw='\n'.join(d['text'] for d in unit['parameter_assignments']+unit['definitions']+unit.get('includes',[]))+'\n'+unit['source']
        statements,_=lex_statements(raw);masked='\n'.join(s['masked'] for s in statements)
        persistent={m.casefold() for m in re.findall(r'%(?:let|do)\s+([A-Za-z_]\w*)\s*=',masked,re.I)}
        persistent.update(m.casefold() for m in re.findall(r':([A-Za-z_]\w*)',masked))
        persistent.update(m.casefold() for m in re.findall(r"\bcall\s+symputx?\s*\(\s*['\"](\w+)['\"]",raw,re.I))
        local=set()
        for header in re.findall(r'%macro\s+\w+\s*\((.*?)\)',masked,re.I|re.S):
            local.update(m.casefold() for m in re.findall(r'(?:^|,)\s*([A-Za-z_]\w*)',header))
        known=known_by_scenario.setdefault(unit['scenario_id'],set(supplied))
        for name in sorted({m.casefold() for m in re.findall(r'&([A-Za-z_]\w*)',masked)}-known-persistent-local):
            if not name.startswith('sys') and name not in {'sqlobs','sqlrc','sqlxrc','sqlxmsg','syserr'}:
                requirements.append({'id':'req-param-'+digest(name)[:16],'code':'external_parameter_required','name':name,
                    'source_id':unit['source_id'],'scenario_id':unit['scenario_id'],'line':unit['line'],'actions':['set_external_parameter']})
        known.update(persistent-local)
        if project['plan'].get('acceptance')=='code_review':
            names,_=declared_writes(unit,project['plan']['parameters'])
            mapped=set()
            for output in project['plan']['outputs']:
                physical='.'.join(output['name']['parts'])
                original=project['plan'].get('output_sources',{}).get(physical,physical).casefold()
                mapped.add(original[5:] if original.startswith('work.') else original)
            for name in names-mapped:
                requirements.append({'code':'output_contract_required','name':name,'source_id':unit['source_id'],
                    'scenario_id':unit['scenario_id'],'line':unit['line'],'actions':['update_contract']})
    for order in orders(project):
        for ident in order["source_ids"]:
            for region in analyses.get(ident,{}).get("regions",[]):
                if (order["id"],ident,region["id"]) not in represented:
                    requirements.append({"code":"uncovered_source_range","source_id":ident,"region_id":region["id"]})
    for node in project["snapshot"].get("unsupported_nodes",[]):
        requirements.append({"code":"project_node_source_required","source_id":node["id"],"kind":node["kind"]})
    return {"version":"semantic-tasks-1","tasks":tasks,"classifications":classifications,
        "requirements":requirements,"hash":digest([tasks,classifications,requirements])}


def task_payload(project, unit, target):
    from .dialect_facts import facts
    from .destinations import resolve
    resolved=resolve(project['plan']);plan=resolved['plan']
    original={i["id"]:i["original"] for i in project["context"]["inputs"]}
    # Metadata only; baseline tables, row values and source paths are not model context.
    contract={k:v for k,v in plan.items() if k in {"parameters","parameter_types","bindings","outputs","source_semantics","allowed_effects"}}
    contract.pop("validation_scope",None)
    payload={"unit":unit,"contract":contract,"target":target,
        'destination_policy':{'testifier':resolved['test_mode'],'outputs':resolved['registry']},
        'dialect_facts':facts(target),
        "target_version":plan.get("target_versions",{}).get(target,plan["target_version"] if target==plan["target"] else "unknown"),
        "source_bindings":[{**b,"original":original[b["source_id"]],
            "origin_scope":next(i['origin_scope'] for i in project['context']['inputs'] if i['id']==b['source_id']),
            "uses":next(i['uses'] for i in project['context']['inputs'] if i['id']==b['source_id'])} for b in plan["bindings"]]}
    payload["hash"]=digest(payload)
    return payload


def declared_writes(unit,parameters):
    """Lower-bound original write names for contract selection; unknown is explicit."""
    from app.sas_lexer import lex_statements
    values={k.casefold():v for k,v in parameters.items()}
    for assignment in unit['parameter_assignments']:
        m=re.fullmatch(r'\s*%let\s+(\w+)\s*=\s*([^%&;]*);\s*',assignment['text'],re.I|re.S)
        if m:values[m[1].casefold()]=m[2].strip()
    names=set();unknown=False
    statements,cutoff=lex_statements('\n'.join(d['text'] for d in unit['definitions']+unit.get('includes',[]))+'\n'+unit['source'])
    unknown = cutoff is not None
    procedure = None
    for stmt in statements:
        text=stmt['masked'].strip()
        targets=[]
        header = re.match(r"proc\s+(\w+)", text, re.I)
        if header:
            procedure = header[1].casefold()
        elif re.match(r"(?:run|quit|data)\b",text,re.I):
            procedure = None
        if re.match(r'data\s+',text,re.I):targets,complete=dataset_list(re.sub(r'^data\s+','',text,flags=re.I));unknown|=not complete
        targets += re.findall(r'\b(?:create\s+(?:table|view)|insert\s+into|update|delete\s+from)\s+([\w&.]+)',text,re.I)
        if re.match(r'proc\s+(?:sort|summary|means|transpose|append)\b',text,re.I):
            targets+=re.findall(r'\b(?:out|base)\s*=\s*([\w&.]+)',text,re.I)
        if procedure in {"summary","means"} and re.match(r"output\b",text,re.I):
            for option in re.finditer(r"\bout\s*=\s*",text,re.I):
                target = re.match(r"[\w&.]+(?=\s|\(|;|$)",text[option.end():])
                if target and (not target[0].endswith(".") or "&" in target[0]):
                    targets.append(target[0])
                else:
                    unknown = True
        for name in targets:
            name=re.sub(r'&([A-Za-z_]\w*)\.?',lambda m:values.get(m[1].casefold(),m[0]),name)
            if '&' in name:unknown=True;continue
            if not re.fullmatch(r"\w+(?:\.\w+)?",name):
                unknown=True
                continue
            name=name.casefold()
            if name.startswith('work.'):name=name[5:]
            if name!='_null_':names.add(name)
    return names,unknown


def sorted_output(unit, *, allow_prefix=False):
    """Proven logical order for a standalone, explicit PROC SORT OUT=."""
    from app.sas_lexer import lex_statements
    statements, cutoff = lex_statements(unit["source"])
    if cutoff or len(statements) < 3 or (not allow_prefix and len(statements) != 3):
        return None
    header, by, end = (s["masked"].strip().casefold() for s in statements[:3])
    dataset = r"([a-z_]\w*(?:\.[a-z_]\w*)?)"
    match = re.fullmatch(r"proc\s+sort\s+data\s*=\s*" + dataset + r"\s+out\s*=\s*" + dataset + r"\s*;", header)
    keys = re.fullmatch(r"by\s+((?:(?:descending\s+)?[a-z_]\w*\s*)+)\s*;", by)
    if not match or not keys or end != "run;":
        return None
    terms = re.findall(r"(?:(descending)\s+)?([a-z_]\w*)", keys[1])
    return match[2].removeprefix("work."), [
        {"column": column, "descending": bool(direction)} for direction, column in terms], match[1]


def lag_input(unit):
    """Identify an unmodified single SET source for the simple LAG contract."""
    from app.sas_lexer import lex_statements
    statements, cutoff = lex_statements(unit["source"])
    if cutoff or unit["kind"] != "data":
        return None
    sas = " ".join(s["masked"].strip().casefold() for s in statements)
    if not re.search(r"\blag\d*\s*\(", sas) or re.search(r"\b(?:merge|modify|update)\s", sas):
        return None
    sets = re.findall(r"\bset\s+([a-z_]\w*(?:\.[a-z_]\w*)?)\s*;", sas)
    return sets[0].removeprefix("work.") if len(sets) == 1 else None
