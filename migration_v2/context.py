"""Lossless structural context with conservative, explicitly reviewed effect inventory.

This adapter reuses statement boundaries, never old display-filtered code or SQL.
Detected reads are a lower bound; macros/unknown statements are not empty effects.
"""
from __future__ import annotations

import re

from app.sas_lexer import lex_statements
from app.sas_blocks import partition
from tools.inspect_sources import mask_noncode, LexicalError
from .contracts import digest, sas_object
from .dataset_syntax import dataset_list

NAME = r"[A-Za-z_&][\w&.]*:?"
KEYWORDS = {"select", "connection", "dictionary", "_null_"}
AI_HANDLED_ISSUES={"macro_function_unsupported", "sysfunc_unsupported", "sql_into_runtime_required",
    "macro_condition_unknown", "macro_loop_bounds", "macro_loop_condition", "macro_do_variant",
    "macro_runtime_effect", "macro_value_unknown", "macro_variable_unknown", "run_date_required",
    "catalog_completeness_required", "macro_expansion_failed", "macro_resolution_limit",
    "macro_expansion_limit", "macro_loop_limit", "macro_call_depth", "external_code_contract_required",
    "sql_into_range_unsupported", "macro_argument_unknown", "macro_if_syntax", "macro_let_syntax",
    "effect_inventory_review", "macro_definition_ambiguity"}


def issue(ident, code, source_id, *, hard=False, detail=""):
    return {"id": ident, "code": code, "source_id": source_id,
            "hard": hard, "detail": detail}


def analyze_source(source: dict, vault) -> dict:
    if source["status"] != "available":
        return {"id": source["id"], "regions": [], "statements": [], "macros": [],
                "issues": [issue(source["id"] + ":missing", "missing_source", source["id"], hard=True)]}
    text = vault.text(source)
    statements, cutoff = lex_statements(text)
    nodes = partition(statements, cutoff)
    regions, cursor = [], 0
    for n in nodes:
        if n["offset"] < cursor:
            raise ValueError("Overlapping source ranges")
        if n["offset"] > cursor:
            regions.append({"start": cursor, "end": n["offset"], "kind": "gap"})
        regions.append({"start": n["offset"], "end": n["end_offset"], "kind": n["kind"],
                        "procedure": n["procedure"], "termination": n["termination"]})
        cursor = n["end_offset"]
    if cursor < len(text):
        regions.append({"start": cursor, "end": len(text), "kind": "gap"})
    issues, macros = [], []
    for i, region in enumerate(regions):
        fragment = text[region["start"]:region["end"]]
        region.update(id=source["id"] + ":region:" + str(i), sha256=digest(fragment.encode("utf-8")),
                      coordinate_system="unicode_codepoints", source_sha256=source["sha256"],
                      line=text.count("\n", 0, region["start"]) + 1,
                      end_line=text.count("\n", 0, region["end"]) + 1)
        if region["kind"] == "macro":
            m = re.search(r"%macro\s+(\w+)", fragment, re.I)
            macros.append({"name": m[1].casefold() if m else "unknown", "region_id": region["id"],
                           "start": region["start"], "end": region["end"], "source_id": source["id"]})
        if region["kind"] == "gap":
            try:
                if mask_noncode(fragment, sas=True).strip():
                    issues.append(issue(region["id"] + ":gap", "unparsed_text", source["id"], hard=True))
                else:
                    region["kind"] = "layout_or_comment"
            except LexicalError:
                issues.append(issue(region["id"] + ":gap", "unparsed_text", source["id"], hard=True))
    if cutoff:
        issues.append(issue(source["id"] + ":lexical", "lexical_cutoff", source["id"], hard=True,
                            detail=f"line={cutoff}; original suffix preserved"))
    # A reviewer must inspect the complete original, not merely the detected names.
    issues.append(issue(source["id"] + ":effects", "effect_inventory_review", source["id"],
                        detail="Detected table references are a lower bound; review full source, macro effects and outputs"))
    assert "".join(text[r["start"]:r["end"]] for r in regions) == text
    return {"id": source["id"], "regions": regions, "statements": statements,
            "macros": macros, "issues": issues, "cutoff": cutoff}


def task_context(snapshot: dict, scenario: dict, vault, parameters=None, manual_inputs=(), *, includes=(), _expanded=False) -> dict:
    from .includes import include_index, inline_includes
    attached = include_index(includes, parameters or {})
    parameters = {key.casefold(): value for key, value in (parameters or {}).items()}

    def expand_name(name):
        # Explicit single-indirection identifier parameters only, not SAS execution.
        if "&&" in name:
            return name
        return re.sub(r"&([A-Za-z_]\w*)\.?", lambda m: parameters.get(m[1].casefold(), m[0]), name)
    sources = {s["id"]: s for s in snapshot["sources"]}
    analyses = {s["id"]: analyze_source(s, vault) for s in snapshot["sources"]}
    selected = scenario["source_ids"]
    issues, reads, writes, definitions, references = [], {}, [], [], []
    versions, uncertain = {}, False
    effect_epoch, write_epochs = 0, {}
    for source_id in selected:
        if source_id not in sources:
            issues.append(issue(source_id + ":missing", "missing_source", source_id, hard=True))
            uncertain = True
            effect_epoch += 1
            continue
        analysis = analyses[source_id]
        issues.extend(analysis["issues"])
        if sources[source_id]["status"] != "available":
            uncertain = True
            effect_epoch += 1
            continue
        text = vault.text(sources[source_id])
        macros = analysis["macros"]
        pending = []

        def record_read(name, stmt):
            nonlocal uncertain
            if name.casefold() in KEYWORDS:
                return
            original_name = name
            name = expand_name(name)
            dynamic = "&" in name or name.endswith(":")
            try:
                key = name.casefold() if dynamic else sas_object(name).key
            except ValueError:
                issues.append(issue(source_id + ":identifier:" + str(stmt["offset"]),
                                    "unsupported_identifier", source_id, hard=True))
                uncertain = True
                return
            version = versions.get(key, 0)
            ref = {"source_id": source_id, "line": stmt["line"], "name": name,
                   "version": version, "uncertain_order": uncertain,'offset':stmt['offset'],
                   'coordinate_basis':'expanded' if _expanded else 'original'}
            references.append(ref)
            if version == 0 or write_epochs.get(key) != effect_epoch:
                # Do not merge the same spelling across unknown connection contexts.
                identity = {"scope": source_id, "name": original_name.casefold()}
                if effect_epoch:identity['effect_epoch']=effect_epoch
                ident = digest(identity)[:24]
                entry = reads.setdefault(ident, {"id": ident, "original": original_name,
                        "name": None if dynamic else list(sas_object(name).parts),
                        "origin_scope": source_id, "dynamic": dynamic, "uses": [],
                        "effect_epoch":effect_epoch,
                        "provenance": "structural_detection_not_complete", "context_uncertain": uncertain})
                entry["uses"].append(ref)
                if dynamic and not any(i["id"] == ident + ":dynamic" for i in issues):
                    issues.append(issue(ident + ":dynamic", "dynamic_source", source_id, hard=True, detail=name))

        def record_write(name, stmt):
            nonlocal uncertain
            name = expand_name(name)
            if name.casefold() == "_null_":
                return
            if "&" in name or name.endswith(":"):
                issues.append(issue(source_id + ":dynamic-write:" + str(stmt["offset"]),
                                    "dynamic_destination", source_id, hard=True))
                uncertain = True
                return
            try:
                key = sas_object(name).key
            except ValueError:
                issues.append(issue(source_id + ":write-identifier:" + str(stmt["offset"]),
                                    "unsupported_identifier", source_id, hard=True))
                uncertain = True
                return
            versions[key] = versions.get(key, 0) + 1
            write_epochs[key] = effect_epoch
            writes.append({"source_id": source_id, "line": stmt["line"], "name": key,
                           "version": versions[key], "storage": "temporary" if "." not in key else "persistent"})

        for stmt in analysis["statements"]:
            if any(m["start"] <= stmt["offset"] < m["end"] for m in macros):
                continue  # Definition is context, not an invocation.
            masked = stmt["masked"].strip()
            if re.match(r"%include\b", masked, re.I):
                issues.append(issue(source_id + ":include:" + str(stmt["offset"]),
                                    "external_code_contract_required", source_id, hard=True))
            if re.match(r"(?:proc|data)\b", masked, re.I):
                for n, st in pending:
                    record_write(n, st)
                pending = []
            if re.match(r"data\b", masked, re.I):
                names, complete = dataset_list(re.sub(r"^data\s+", "", masked, flags=re.I))
                pending.extend((name, stmt) for name in names)
                if not complete:
                    issues.append(issue(source_id + ":data-list:" + str(stmt["offset"]), "dataset_list_unresolved", source_id, hard=True))
            if re.match(r"proc\s+append\b", masked, re.I):
                for option in ("base", "data"):
                    match = re.search(r"\b" + option + r"\s*=\s*(" + NAME + ")", masked, re.I)
                    if match:
                        record_read(match[1], stmt)
                        if option == "base": pending.append((match[1], stmt))
            if re.match(r"proc\s+sort\b", masked, re.I):
                incoming = re.search(r"\bdata\s*=\s*(" + NAME + ")", masked, re.I)
                outgoing = re.search(r"\bout\s*=\s*(" + NAME + ")", masked, re.I)
                duplicates = re.search(r"\bdupout\s*=\s*(" + NAME + ")", masked, re.I)
                if incoming:
                    record_read(incoming[1], stmt)
                    pending.append(((outgoing or incoming)[1], stmt))
                if duplicates:
                    pending.append((duplicates[1], stmt))
            if re.match(r"proc\s+(?:summary|means)\b", masked, re.I):
                depth = 0
                for token in re.finditer(r"[()]|(?<![\w.])data\s*=\s*(" + NAME + ")", masked, re.I):
                    if token[0] == "(":
                        depth += 1
                    elif token[0] == ")":
                        depth -= 1
                    elif depth == 0:
                        record_read(token[1], stmt)
                        break
            for match in re.finditer(r"\b(?:from|join)\s+(" + NAME + ")", masked, re.I):
                # SET assignments in PROC SQL are not proven dataset reads.
                if match[0].casefold().startswith("set ") and re.search(r"\bupdate\b", masked, re.I):
                    continue
                record_read(match[1], stmt)
            if re.match(r"(?:set|merge)\b", masked, re.I):
                names, complete = dataset_list(re.sub(r"^(?:set|merge)\s+", "", masked, flags=re.I))
                for name in names:
                    record_read(name, stmt)
                if not complete:
                    issues.append(issue(source_id + ":dataset-options:" + str(stmt["offset"]),
                                        "dataset_list_unresolved", source_id, hard=True))
            if re.search(r"\bselect\b", masked, re.I):
                # SQLGlot identifies comma sources and nested selects; SAS-only SQL
                # remains explicitly reviewable if its grammar cannot be resolved.
                import sqlglot
                from sqlglot import exp
                try:
                    tree = sqlglot.parse_one(stmt["text"], read="postgres", error_message_context=0)
                    known_ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
                    for table in tree.find_all(exp.Table):
                        if table is tree.this or table.name.casefold() in known_ctes:
                            continue
                        name = ".".join(filter(None, [table.db, table.name]))
                        if not any(r["source_id"] == source_id and r["line"] == stmt["line"] and r["name"].casefold() == name.casefold() for r in references):
                            record_read(name, stmt)
                except Exception:
                    issues.append(issue(source_id + ":sql-effects:" + str(stmt["offset"]), "sql_effects_review", source_id))
            for match in re.finditer(r"\b(?:create\s+(?:table|view)|insert\s+into|update)\s+(" + NAME + ")", masked, re.I):
                record_write(match[1], stmt)
            if re.match(r"(?:run|quit)\s*;", masked, re.I):
                for n, st in pending:
                    record_write(n, st)
                pending = []
            if (masked.startswith("%") and not re.match(r"%(?:let|global|local)\b", masked, re.I)) or re.search(r"\b(?:execute|call\s+execute|libname)\b", masked, re.I):
                uncertain = True
                effect_epoch += 1
            if re.search(r"\b(?:delete\s+from|drop\s+table|alter\s+table)\b", masked, re.I):
                uncertain = True
                effect_epoch += 1
        for n, st in pending:
            record_write(n, st)
        uncertain |= bool(analysis["cutoff"])
    # Keep all definitions, including potential helper programs outside selected order.
    # Ambiguity remains visible; none is executed or implicitly selected.
    for analysis in analyses.values():
        definitions.extend(analysis["macros"])
    by_name = {}
    for definition in definitions:
        by_name.setdefault(definition["name"], []).append(definition)
    for name, defs in by_name.items():
        if len(defs) > 1:
            issues.append(issue("macro:" + name, "macro_definition_ambiguity", "project"))
    source_ids = list(dict.fromkeys(selected + [d["source_id"] for d in definitions]))
    for declaration in manual_inputs:
        origin = declaration["origin_source_id"]
        if origin not in sources or origin not in source_ids:
            raise ValueError("Manual input must belong to an included source")
        name = sas_object(".".join(declaration["name"]["parts"]))
        ident = digest({"scope": origin, "name": name.key})[:24]
        if ident not in reads and not any(e['origin_scope']==origin and e['name']==list(name.parts) for e in reads.values()):
            reads[ident] = {"id": ident, "original": name.key, "name": list(name.parts),
                           "origin_scope": origin, "dynamic": False, "uses": [],
                           "provenance": "human_declared", "context_uncertain": True,
                           "rationale": declaration["rationale"]}
    regions = [r for ident in selected if ident in analyses for r in analyses[ident]["regions"]]
    result = {"schema_version": 1, "scenario": scenario, "source_ids": source_ids,
            "inventory_complete": False, "inputs": list(reads.values()), "writes": writes,
            "references": references, "definitions": definitions, "regions": regions,
            "issues": issues, "source_hashes": {s: sources[s].get("sha256") for s in source_ids if s in sources},
            "analysis_version": "lossless-structural-review-1"}
    if not _expanded and any(sources.get(i, {}).get("status") == "available" and
                             re.search(r"%|&[A-Za-z_]", vault.text(sources[i])) for i in selected):
        from app.macro_processor import MacroProcessor
        processor = MacroProcessor()
        processor.seed(parameters)
        expanded_sources, expanded_texts, expansion_trace, failures = [], {}, [], []
        for ident in selected:
            if sources.get(ident, {}).get("status") != "available":
                failures.append(issue(ident + ":expansion-missing", "missing_source", ident, hard=True))
                continue
            try:
                expansion = processor.expand_program(ident, inline_includes(vault, attached, sources[ident]))
                expanded_texts[ident] = expansion.source
                expanded_sources.append({**sources[ident], "sha256": expansion.expanded_hash})
                expansion_trace.extend(expansion.source_map)
                for index, req in enumerate(expansion.requirements):
                    failures.append(issue(ident + ":macro:" + str(index), req["code"], ident,
                        hard=bool(req["blocking"]), detail=req["message"] + "; line=" + str(req["line"])))
            except ValueError:
                failures.append(issue(ident + ":expansion", "macro_expansion_failed", ident, hard=True))
        result["expansion_trace"] = expansion_trace
        result["issues"].extend(failures)
        if not failures:
            class ExpansionVault:
                def text(self, source): return expanded_texts[source["id"]]
            expanded_context = task_context({"sources": expanded_sources}, scenario, ExpansionVault(),
                parameters, [d for d in manual_inputs if d["origin_source_id"] in selected], _expanded=True)
            for entry in expanded_context["inputs"]:
                matches = [old for old in result["inputs"] if old["origin_scope"] == entry["origin_scope"]
                           and old["name"] == entry["name"]]
                if len(matches) == 1:
                    entry["id"], entry["original"] = matches[0]["id"], matches[0]["original"]
            for entry in result["inputs"]:
                if entry.get("provenance") == "human_declared" and not any(e["id"] == entry["id"] for e in expanded_context["inputs"]):
                    expanded_context["inputs"].append(entry)
            for key in ("inputs", "writes", "references"):
                result[key] = expanded_context[key]
            result["issues"].extend(i for i in expanded_context["issues"]
                                    if i["hard"] and i["id"] not in {x["id"] for x in result["issues"]})
            result["analysis_version"] = "macro-expanded-structural-review-2"
    return result


def plan_issues(plan, context):
    problems = []
    bindings = {b.source_id: b for b in plan.bindings}
    registry = {s["id"]: s for s in context["inputs"]}
    internal=set(plan.intermediate_inputs)
    if len(internal)!=len(plan.intermediate_inputs) or internal-set(registry):problems.append('invalid_intermediate_input_registry')
    if set(bindings) != set(registry)-internal:
        problems.append("bindings_must_cover_exact_input_registry")
    for ident, binding in bindings.items():
        if plan.acceptance=='code_review' and (not binding.schema_complete or binding.metadata_status!='user_supplied'):
            problems.append('complete_source_schema_required:'+ident)
        if not binding.columns:
            problems.append("column_types_required:" + ident)
        entry = registry.get(ident)
        if not entry:
            continue
        if entry["dynamic"] and not binding.members:
            problems.append("dynamic_source:" + ident)
        if binding.members:
            if not entry['original'].endswith(':'):problems.append('members_require_prefix_source:'+ident)
            template=re.sub(r'&\w+\.?','*',entry['original'].rstrip(':')).casefold()
            if template.startswith('work.'):template=template[5:]
            pattern='^'+re.escape(template).replace(r'\*',r'\w*')+r'\w*$'
            if any(not re.fullmatch(pattern,m.original.casefold().removeprefix('work.')) for m in binding.members):problems.append('member_outside_source_prefix:'+ident)
        if binding.decision == "keep_original" and tuple(entry["name"] or ()) != binding.target.parts:
            problems.append("keep_original_mismatch:" + ident)
    targets = [b.target.key for b in plan.bindings]
    shared = {t for t in targets if targets.count(t) > 1}
    for target in shared:
        if len(plan.shared_target_rationale.get(target, "").strip()) < 12:
            problems.append("input_target_collision_requires_confirmation:" + target)
        schemas = [{(c.name, c.data_type) for c in b.columns} for b in plan.bindings if b.target.key == target]
        if any(s != schemas[0] for s in schemas):
            problems.append("incompatible_shared_target_columns:" + target)
    if set(plan.shared_target_rationale) - shared:
        problems.append("unknown_shared_target_confirmation")
    for target in set(targets).intersection(o.name.key for o in plan.outputs):
        mutation=plan.acceptance=='code_review' and bool(set(plan.allowed_effects.get(target,())) & {'insert','update','delete','drop'})
        output=next(o for o in plan.outputs if o.name.key==target)
        schemas_match=all(tuple(c.name for c in b.columns)==output.columns for b in plan.bindings if b.target.key==target)
        if not mutation or not schemas_match:problems.append("input_output_collision")
    resolved = {r.issue_id for r in plan.resolutions}
    if resolved - {i["id"] for i in context["issues"]}:
        problems.append("unknown_review_resolution")
    for item in context["issues"]:
        if item['code']=='dynamic_source' and (item['id'].removesuffix(':dynamic') in internal or bindings.get(item['id'].removesuffix(':dynamic')) and bindings[item['id'].removesuffix(':dynamic')].members):continue
        if plan.acceptance == "code_review" and item["code"] in AI_HANDLED_ISSUES:
            continue # Original source is translated/reviewed by the AI path, not dropped.
        if item["hard"] or item["id"] not in resolved:
            problems.append(item["code"] + ":" + item["id"])
    if plan.mode == "reusable_etl":
        problems.append("reusable_runner_not_implemented")
    if plan.target == "clickhouse" and plan.acceptance != "code_review":
        problems.append("clickhouse_static_adapter_not_implemented")
    return sorted(set(problems))
