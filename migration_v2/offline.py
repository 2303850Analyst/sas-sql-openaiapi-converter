"""Bounded offline compiler: complete simple CTAS/DATA copies, never partial success.

Complex SAS remains a structured requirement; original source is always retained.
"""
from datetime import date
import re
import sqlglot
from sqlglot import exp
from app.macro_processor import MacroProcessor
from app.sas_lexer import lex_statements
from .contracts import digest, sas_object


def compile_project(wb, project):
    if not project.get("task") or not project.get("gate1"):
        raise ValueError("Approve the current plan before offline compilation")
    plan = project["plan"]
    if plan["mode"] != "fixed_run" or plan["target"] == "clickhouse":
        raise ValueError("Offline compiler supports fixed PostgreSQL/Greenplum CTAS drafts only")
    processor = MacroProcessor()
    processor.seed(plan["parameters"])
    bindings = {}
    inputs = {x["id"]: x for x in project["context"]["inputs"]}
    for binding in plan["bindings"]:
        original = sas_object(inputs[binding["source_id"]]["original"]).key
        if original in bindings and bindings[original] != binding["target"]["parts"]:
            raise ValueError("Scoped input mapping requires separate tasks")
        bindings[original] = binding["target"]["parts"]
    outputs = {sas_object(".".join(o["name"]["parts"])).key: o for o in plan["outputs"]}
    artifacts, requirements, traces = [], [], []
    created = set()
    for source_id in project["scenario"]["source_ids"]:
        source = wb.source_text(project, source_id)
        try:
            expanded = processor.expand_program(source_id, source)
        except ValueError:
            requirements.append({"code": "macro_expansion_failed", "source_id": source_id})
            continue
        traces.extend(expanded.source_map)
        requirements.extend(expanded.requirements)
        statements, cutoff = lex_statements(expanded.source)
        if cutoff: requirements.append({"code": "lexical_cutoff", "source_id": source_id})
        in_sql = False
        pending = None
        for stmt in statements:
            text = stmt["text"].strip()
            masked = stmt["masked"].strip()
            if re.fullmatch(r"proc\s+sql\s*;", masked, re.I):
                in_sql = True
                continue
            if re.fullmatch(r"(?:quit|run)\s*;", masked, re.I):
                in_sql = False
                if pending:
                    destination, incoming = pending
                    if incoming:
                        text = f"CREATE TABLE {destination} AS SELECT * FROM {incoming};"
                    else:
                        requirements.append({"code": "data_copy_source_required", "source_id": source_id, "line": stmt["line"]})
                        pending = None
                        continue
                    pending = None
                else: continue
            elif re.fullmatch(r"data\s+[\w.]+\s*;", masked, re.I):
                pending = (masked.split()[1].rstrip(";"), None)
                continue
            elif pending and re.fullmatch(r"set\s+[\w.]+\s*;", masked, re.I) and pending[1] is None:
                pending = (pending[0], masked.split()[1].rstrip(";"))
                continue
            elif not in_sql:
                requirements.append({"code": "offline_construct_not_supported", "source_id": source_id, "line": stmt["line"]})
                pending = None
                continue
            try:
                tree = sqlglot.parse_one(text, read="postgres")
                if not isinstance(tree, exp.Create) or tree.kind.upper() != "TABLE" or not isinstance(tree.this, exp.Table) or not isinstance(tree.expression, exp.Select):
                    raise ValueError("Only CTAS")
                select = tree.expression
                # No implicit SAS-to-SQL equivalence for functions, ordering, joins,
                # missing comparisons or computed expressions in this initial adapter.
                if any(select.args.get(k) for k in ("joins", "group", "having", "order", "limit", "with_")):
                    raise ValueError("Requires a semantic lowering rule")
                if any(not isinstance(e, (exp.Column, exp.Star)) for e in select.expressions):
                    raise ValueError("Computed projection requires a semantic lowering rule")
                where = select.args.get("where")
                if where:
                    predicate = where.this
                    if not (isinstance(predicate, exp.EQ) and isinstance(predicate.left, exp.Column)
                            and isinstance(predicate.right, exp.Literal) and not predicate.right.is_string):
                        raise ValueError("Predicate requires a semantic lowering rule")
                    columns = [c for b in plan["bindings"] for c in b["columns"] if c["name"] == predicate.left.name]
                    if len(columns) != 1 or not re.match(r"(?:INTEGER|INT|BIGINT|SMALLINT|NUMERIC|DECIMAL|FLOAT|DOUBLE)\b", columns[0]["data_type"], re.I):
                        raise ValueError("Predicate type is not established")
                destination = sas_object(tree.this.sql()).key
                if destination not in outputs or destination in created:
                    raise ValueError("Unapproved/repeated output")
                for table in tree.expression.find_all(exp.Table):
                    key = sas_object(".".join(filter(None, [table.db, table.name]))).key
                    parts = bindings.get(key) if key not in created else outputs[key]["name"]["parts"]
                    if not parts: raise ValueError("Unresolved input")
                    original_name, original_db = table.name, table.db
                    for column in tree.expression.find_all(exp.Column):
                        if column.table == original_name and (not column.db or column.db == original_db) and not table.alias:
                            column.set("table", exp.to_identifier(parts[-1]))
                            column.set("db", exp.to_identifier(parts[0]) if len(parts) == 2 else None)
                    replacement = exp.Table(this=exp.to_identifier(parts[-1]), db=exp.to_identifier(parts[0]) if len(parts) == 2 else None)
                    replacement.set("alias", table.args.get("alias"))
                    table.replace(replacement)
                output = outputs[destination]
                tree.set("this", exp.Table(this=exp.to_identifier(output["name"]["parts"][-1]),
                    db=exp.to_identifier(output["name"]["parts"][0]) if len(output["name"]["parts"]) == 2 else None))
                if output["storage"] == "temporary":
                    tree.set("properties", exp.Properties(expressions=[exp.TemporaryProperty()]))
                artifacts.append({"name": f"{len(artifacts)+1:03d}.sql", "sql": tree.sql(dialect="postgres") + ";"})
                created.add(destination)
            except Exception:
                requirements.append({"code": "offline_ctas_contract_unresolved", "source_id": source_id, "line": stmt["line"]})
        if pending:
            requirements.append({"code": "unterminated_data_step", "source_id": source_id})
    for name in outputs.keys() - created:
        requirements.append({"code": "missing_output", "output": name})
    result = {"generator": "bounded-offline-1", "requirements": requirements,
        "candidate": None, "trace": traces, "api_calls": 0}
    if not requirements and artifacts:
        result["candidate"] = {"schema_version": 1, "task_hash": project["task"]["hash"], "artifacts": artifacts,
            "source_claims": [r["id"] for r in project["context"]["regions"] if r["kind"] != "layout_or_comment"], "assumptions": []}
    return result
