"""Deterministic whole-procedure lowering and explicit orchestration plans."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp

from .data_rules import TABLE
from .procedure_rules import by_fields


@dataclass(frozen=True)
class OrchestratorPlan:
    action: str
    requirements: tuple[str, ...]


def _table(value: str) -> exp.Table:
    from .compiler import parse_one
    return parse_one("SELECT * FROM " + value).args["from_"].this


def _entry(name: str, contracts: dict) -> dict:
    entry = contracts.get(name.casefold())
    if not isinstance(entry, dict) or not entry.get("columns"):
        raise ValueError(f"{name}: нужен полный каталог колонок")
    return entry


def _actual(entry: dict, name: str) -> str:
    found = {column.casefold(): column for column in entry["columns"]}.get(name.casefold())
    if not found:
        raise ValueError(f"Неизвестная колонка {name}")
    return found


def compile_append(items: list[dict], contracts: dict):
    header = items[0]["masked"].strip()
    match = re.fullmatch(r"proc\s+append\s+(.+);", header, re.I | re.S)
    if not match:
        raise ValueError("APPEND: нужны BASE= и DATA=")
    options = match.group(1)
    values = {key.lower(): value for key, value in re.findall(r"\b(base|data)\s*=\s*(" + TABLE + r")\b", options, re.I)}
    rest = re.sub(r"\b(?:base|data)\s*=\s*" + TABLE, " ", options, flags=re.I).split()
    if set(values) != {"base", "data"} or any(value.lower() not in {"force", "nowarn"} for value in rest):
        raise ValueError("APPEND: поддержаны BASE=, DATA=, FORCE и NOWARN")
    if any(not re.fullmatch(r"(?:run|quit)\s*;", item["masked"].strip(), re.I) for item in items[1:]):
        raise ValueError("APPEND: дополнительные statements требуют отдельного правила")
    base, source = values["base"], values["data"]
    base_entry, source_entry = _entry(base, contracts), _entry(source, contracts)
    source_names = {column.casefold(): column for column in source_entry["columns"]}
    if set(source_names) != {column.casefold() for column in base_entry["columns"]}:
        raise ValueError("APPEND: FORCE с несовпадающей схемой требует правил типов, длин и missing")
    base_types, source_types = base_entry.get("column_types", {}), source_entry.get("column_types", {})
    if any(not base_types.get(name.casefold()) or base_types.get(name.casefold()) != source_types.get(name.casefold())
           for name in base_entry["columns"]):
        raise ValueError("APPEND: нужны совпадающие типы и длины всех колонок BASE и DATA")
    base_lengths, source_lengths = base_entry.get("column_lengths", {}), source_entry.get("column_lengths", {})
    if any(base_lengths.get(name.casefold()) is None or base_lengths.get(name.casefold()) != source_lengths.get(name.casefold())
           for name in base_entry["columns"] if base_types[name.casefold()] == "character"):
            raise ValueError("APPEND: нужны совпадающие типы и длины всех колонок BASE и DATA")
    projections = [exp.column(source_names[column.casefold()], quoted=True) for column in base_entry["columns"]]
    query = exp.select(*projections).from_(_table(source))
    tree = exp.Insert(this=_table(base), expression=query)
    return tree, {}, False, ["proc.append.insert_select"]


def compile_transpose(items: list[dict], contracts: dict):
    header = items[0]["masked"].strip()
    match = re.fullmatch(r"proc\s+transpose\s+(.+);", header, re.I | re.S)
    if not match:
        raise ValueError("TRANSPOSE: нужны DATA= и OUT=")
    options = match.group(1)
    values = {key.lower(): value for key, value in re.findall(r"\b(data|out|prefix)\s*=\s*(" + TABLE + r")\b", options, re.I)}
    rest = re.sub(r"\b(?:data|out|prefix)\s*=\s*" + TABLE, " ", options, flags=re.I).split()
    if "data" not in values or "out" not in values or rest:
        raise ValueError("TRANSPOSE: поддержаны DATA=, OUT= и PREFIX= без LET/NAME/SUFFIX")
    by: list[str] = []
    id_name = var_name = None
    for item in items[1:]:
        text = item["masked"].strip()
        if re.fullmatch(r"(?:run|quit)\s*;", text, re.I):
            continue
        if re.match(r"by\b", text, re.I) and not by:
            parsed = by_fields(item["text"].strip()[2:])
            if any(descending for _, descending in parsed):
                raise ValueError("TRANSPOSE: DESCENDING BY требует отдельного порядка")
            by = [name for name, _ in parsed]
        elif re.fullmatch(r"id\s+([A-Za-z_]\w*)\s*;", text, re.I) and id_name is None:
            id_name = re.fullmatch(r"id\s+([A-Za-z_]\w*)\s*;", text, re.I).group(1)
        elif re.fullmatch(r"var\s+([A-Za-z_]\w*)\s*;", text, re.I) and var_name is None:
            var_name = re.fullmatch(r"var\s+([A-Za-z_]\w*)\s*;", text, re.I).group(1)
        else:
            raise ValueError("TRANSPOSE: поддержаны по одному BY, ID и VAR")
    if not id_name or not var_name:
        raise ValueError("TRANSPOSE: обязательны ID и VAR")
    source, output = values["data"], values["out"]
    entry = _entry(source, contracts)
    id_actual, var_actual = _actual(entry, id_name), _actual(entry, var_name)
    by_actual = [_actual(entry, name) for name in by]
    domain = entry.get("categorical_domains", {}).get(id_actual.casefold())
    if not domain:
        raise ValueError("TRANSPOSE: нужен categorical_domains для ID")
    required_key = {name.casefold() for name in [*by_actual, id_actual]}
    if required_key not in [set(key) for key in entry.get("unique_keys", [])]:
        raise ValueError("TRANSPOSE: unique_keys должен подтверждать уникальность BY + ID")
    prefix = values.get("prefix", "")
    names: list[str] = []
    projections: list[exp.Expression] = [exp.column(name, quoted=True) for name in by_actual]
    projections.append(exp.alias_(exp.Literal.string(var_actual), "_NAME_", quoted=True))
    for value in domain:
        output_name = prefix + str(value)
        if not re.fullmatch(r"[A-Za-z_]\w{0,127}", output_name) or output_name.casefold() in {name.casefold() for name in names}:
            raise ValueError("TRANSPOSE: значения ID должны образовывать уникальные SQL-имена; нужен явный mapping")
        names.append(output_name)
        literal = exp.Literal.string(value) if isinstance(value, str) else exp.Literal.number(value)
        condition = exp.EQ(this=exp.column(id_actual, quoted=True), expression=literal)
        case = exp.Case(ifs=[exp.If(this=condition, true=exp.column(var_actual, quoted=True))], default=exp.Null())
        projections.append(exp.alias_(exp.Max(this=case), output_name, quoted=True))
    query = exp.select(*projections).from_(_table(source))
    if by_actual:
        query.set("group", exp.Group(expressions=[exp.column(name, quoted=True) for name in by_actual]))
    tree = exp.Create(this=_table(output), kind="TABLE", expression=query)
    return tree, {}, False, ["proc.transpose.conditional_aggregate"]


def compile_summary(items: list[dict], contracts: dict):
    header = items[0]["masked"].strip()
    match = re.fullmatch(r"proc\s+(?:summary|means)\s+(.+);", header, re.I | re.S)
    if not match:
        raise ValueError("SUMMARY/MEANS: нужен DATA= и NWAY")
    options = match.group(1)
    source_match = re.search(r"\bdata\s*=\s*(" + TABLE + r")\b", options, re.I)
    rest = re.sub(r"\bdata\s*=\s*" + TABLE, " ", options, flags=re.I).split()
    lowered = {value.lower() for value in rest}
    if not source_match or "nway" not in lowered or lowered - {"nway", "missing", "noprint"}:
        raise ValueError("SUMMARY/MEANS: поддержаны DATA=, NWAY, MISSING и NOPRINT")
    source = source_match.group(1)
    entry = _entry(source, contracts)
    groups: list[str] = []
    class_names: list[str] = []
    variables: list[str] = []
    output = None
    stats: list[tuple[str, list[str]]] = []
    drop_internal: set[str] = set()
    for item in items[1:]:
        text = item["masked"].strip()
        if re.fullmatch(r"(?:run|quit)\s*;", text, re.I):
            continue
        statement = re.fullmatch(r"(class|by|var)\s+(.+);", text, re.I | re.S)
        if statement:
            names = re.findall(r"[A-Za-z_]\w*", statement.group(2))
            if not names:
                raise ValueError("SUMMARY/MEANS: пустой список полей")
            if statement.group(1).lower() in {"class", "by"}:
                groups.extend(names)
                if statement.group(1).lower() == "class":
                    class_names.extend(names)
            else:
                variables.extend(names)
            continue
        out = re.fullmatch(r"output\s+out\s*=\s*(" + TABLE + r")(?:\s*\(\s*drop\s*=\s*([^)]*)\))?\s+(.+);", text, re.I | re.S)
        if out and output is None:
            output = out.group(1)
            drop_internal = {name.casefold() for name in re.findall(r"[A-Za-z_]\w*", out.group(2) or "")}
            tail = out.group(3)
            matches = list(re.finditer(r"\b(sum|mean|min|max|n)\s*=\s*(.*?)(?=\b(?:sum|mean|min|max|n)\s*=|$)", tail, re.I | re.S))
            cursor = 0
            valid = bool(matches)
            for found in matches:
                if tail[cursor:found.start()].strip() or not re.fullmatch(r"[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s*", found.group(2)):
                    valid = False
                    break
                stats.append((found.group(1).lower(), re.findall(r"[A-Za-z_]\w*", found.group(2))))
                cursor = found.end()
            if cursor < len(tail) and tail[cursor:].strip():
                valid = False
            if not valid:
                raise ValueError("SUMMARY/MEANS: поддержаны явные SUM=/MEAN=/MIN=/MAX=/N= псевдонимы")
            continue
        raise ValueError("SUMMARY/MEANS: statement требует отдельного правила")
    if not output or not variables:
        raise ValueError("SUMMARY/MEANS: обязательны VAR и OUTPUT OUT=")
    groups_actual = [_actual(entry, name) for name in groups]
    variables_actual = [_actual(entry, name) for name in variables]
    projections: list[exp.Expression] = [exp.column(name, quoted=True) for name in groups_actual]
    if "_type_" not in drop_internal:
        projections.append(exp.alias_(exp.Literal.number((1 << len(class_names)) - 1), "_TYPE_", quoted=True))
    if "_freq_" not in drop_internal:
        projections.append(exp.alias_(exp.Count(this=exp.Star()), "_FREQ_", quoted=True))
    functions = {"sum": exp.Sum, "mean": exp.Avg, "min": exp.Min, "max": exp.Max, "n": exp.Count}
    for stat, aliases in stats:
        if len(aliases) != len(variables_actual):
            raise ValueError("SUMMARY/MEANS: число псевдонимов должно совпадать с VAR")
        for variable, alias in zip(variables_actual, aliases):
            projections.append(exp.alias_(functions[stat](this=exp.column(variable, quoted=True)), alias, quoted=True))
    query = exp.select(*projections).from_(_table(source))
    if groups_actual:
        query.set("group", exp.Group(expressions=[exp.column(name, quoted=True) for name in groups_actual]))
        if "missing" not in lowered:
            condition = None
            for name in [_actual(entry, class_name) for class_name in class_names]:
                term = exp.Is(this=exp.column(name, quoted=True), expression=exp.Null()).not_()
                condition = term if condition is None else exp.and_(condition, term)
            if condition is not None:
                query.set("where", exp.Where(this=condition))
    return exp.Create(this=_table(output), kind="TABLE", expression=query), {}, False, ["proc.summary.nway"]


def compile_procedure(items: list[dict], procedure: str, contracts: dict):
    if procedure == "append":
        return compile_append(items, contracts)
    if procedure == "transpose":
        return compile_transpose(items, contracts)
    if procedure in {"summary", "means"}:
        return compile_summary(items, contracts)
    raise ValueError("Для процедуры нет SQL lowering")


def orchestrator_plan(procedure: str) -> OrchestratorPlan | None:
    plans = {
        "contents": OrchestratorPlan("read_source_catalog", ("source_connection", "metadata_access")),
        "format": OrchestratorPlan("publish_format_catalog", ("format_definition_export",)),
        "http": OrchestratorPlan("http_request", ("network_policy", "secret_binding", "response_contract")),
        "import": OrchestratorPlan("load_external_file", ("file_contract", "encoding", "schema")),
        "export": OrchestratorPlan("publish_external_file", ("file_contract", "encoding", "exposure_policy")),
        "datasets": OrchestratorPlan("dataset_lifecycle", ("library_mapping", "ddl_policy", "dependency_check")),
        "delete": OrchestratorPlan("dataset_delete", ("library_mapping", "destructive_action_approval")),
        "print": OrchestratorPlan("render_report", ("report_destination",)),
        "odstext": OrchestratorPlan("render_report", ("report_destination",)),
    }
    return plans.get(procedure)
