"""Independent bounded static verifier. It never executes submitted SQL.

The supported local pilot package consists of CTAS SELECT statements. Other SQL
is explicit unsupported, not stripped out or accepted through a generic parser.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from .contracts import Candidate, Plan, digest

ALLOWED = set("""
Select From Table Identifier TableAlias Column Alias Star Literal Null Boolean
Where And Or Not Is EQ NEQ GT GTE LT LTE NullSafeEQ NullSafeNEQ In Between Like ILike
Add Sub Mul Div Mod Neg Paren Case If Cast TryCast DataType DataTypeParam
Join Group Having Order Ordered Limit Offset Distinct Union Intersect Except
With CTE Subquery Window WindowSpec Partition RowNumber Rank DenseRank Count Sum Min Max Avg
Coalesce Nullif Upper Lower Trim Substring Length Concat DPipe Abs Round
Extract Date CurrentDate Interval Tuple Values TimestampTrunc DateTrunc Lag Lead
""".split())


def table_key(table):
    parts = []
    for part in table.parts:
        if not isinstance(part, exp.Identifier):
            raise ValueError("Nonliteral SQL object")
        parts.append(part.name if part.args.get("quoted") else part.name.casefold())
    if not 1 <= len(parts) <= 2:
        raise ValueError("Cross-database SQL object")
    return ".".join(parts)


def allowed_node(node):
    if isinstance(node, exp.Pad):
        length = node.args.get("expression")
        fill = node.args.get("fill_pattern")
        return (isinstance(length, exp.Literal) and not length.is_string
                and str(length.this).isdigit() and 0 <= int(length.this) <= 4096
                and (fill is None or isinstance(fill, exp.Literal) and fill.is_string))
    if isinstance(node, exp.Var):
        return (isinstance(node.parent, (exp.TimestampTrunc, exp.DateTrunc, exp.Extract, exp.Interval))
                and node.arg_key in {"unit", "this"} and str(node.this).upper() in
                {"MICROSECOND", "MILLISECOND", "SECOND", "MINUTE", "HOUR", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR", "DOW", "DOY", "EPOCH"})
    if isinstance(node, exp.DataType):
        return node.this.name in {"BOOLEAN", "TINYINT", "SMALLINT", "INT", "BIGINT", "FLOAT", "DOUBLE", "DECIMAL",
                                  "CHAR", "VARCHAR", "TEXT", "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ", "INTERVAL"}
    return type(node).__name__ in ALLOWED


def verify(task_hash: str, plan: Plan, candidate: Candidate, obligations: list[str]) -> dict:
    errors, notes = [], []
    checks = {"envelope": "passed", "sql_static": "not_run", "object_binding": "not_run",
              "column_binding": "not_run", "output_contract": "not_run",
              "execution": "not_run", "equivalence": "unavailable"}
    if candidate.task_hash != task_hash:
        errors.append("stale_task_hash")
    if set(candidate.source_claims) != set(obligations) or len(set(candidate.source_claims)) != len(candidate.source_claims):
        errors.append("source_claim_set_mismatch")
    if candidate.assumptions:
        errors.append("candidate_has_unresolved_assumptions")
    if errors:
        checks["envelope"] = "failed"
        return report(task_hash, candidate, checks, errors, notes)
    if plan.target not in {"greenplum", "postgresql"}:
        errors.append("unsupported_static_target")
        checks["sql_static"] = "failed"
        return report(task_hash, candidate, checks, errors, notes)
    available = {b.target.key: {c.name: c.data_type for c in b.columns} for b in plan.bindings}
    outputs = {o.name.key: o for o in plan.outputs}
    created, parsed = set(), []
    for artifact in candidate.artifacts:
        try:
            trees = sqlglot.parse(artifact.sql, read="postgres", error_level="RAISE")
        except Exception:
            errors.append("sql_parse_error:" + artifact.name)
            continue
        if not trees or any(t is None for t in trees):
            errors.append("empty_sql_statement:" + artifact.name)
            continue
        for index, tree in enumerate(trees):
            location = f"{artifact.name}:{index + 1}"
            if not isinstance(tree, exp.Create) or tree.args.get("kind") != "TABLE" or not isinstance(tree.this, exp.Table):
                errors.append("unsupported_statement:" + location)
                continue
            if any(tree.args.get(k) for k in ("replace", "exists", "indexes", "clone", "concurrently")):
                errors.append("unsupported_create_option:" + location)
            props = tree.args.get("properties")
            if props and any(not isinstance(p, exp.TemporaryProperty) for p in props.expressions):
                errors.append("unsupported_table_property:" + location)
            try:
                name = table_key(tree.this)
            except ValueError:
                errors.append("unsupported_destination:" + location)
                continue
            if name not in outputs:
                errors.append("undeclared_write:" + name)
                continue
            if name in available or name in created:
                errors.append("duplicate_or_input_write:" + name)
                continue
            output = outputs[name]
            temporary = bool(props and any(isinstance(p, exp.TemporaryProperty) for p in props.expressions))
            if temporary != (output.storage == "temporary"):
                errors.append("wrong_storage:" + name)
            query = tree.expression
            if not isinstance(query, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                errors.append("ctas_query_required:" + name)
                continue
            unsupported = sorted({type(n).__name__ for n in query.walk() if not allowed_node(n)})
            if unsupported:
                errors.append("unsupported_expression:" + ",".join(unsupported))
            if any(n.args.get("recursive") for n in query.find_all(exp.With)):
                errors.append("recursive_query_not_supported:" + name)
            if any(n.args.get("into") or n.args.get("locks") for n in query.find_all(exp.Select)):
                errors.append("query_effect_not_allowed:" + name)
            try:
                for scope in traverse_scope(query):
                    for _, (_, source) in scope.selected_sources.items():
                        if isinstance(source, exp.Table):
                            key = table_key(source)
                            if key not in available:
                                errors.append("unbound_read:" + key)
                            elif not available[key]:
                                errors.append("input_columns_unknown:" + key)
                # SQLGlot qualifies a copy against only the approved inputs and earlier outputs.
                # Unknown columns and unresolvable SELECT * must not pass a column check.
                schema = {}
                for key, cols in available.items():
                    parts = key.split(".")
                    if len(parts) == 1:
                        schema.setdefault("", {})[parts[0]] = cols
                    else:
                        schema.setdefault(parts[0], {})[parts[1]] = cols
                resolved = qualify(query.copy(), dialect="postgres", schema=schema,
                                   validate_qualify_columns=True, identify=False)
                names = resolved.named_selects
                if names != list(output.columns) or len(names) != len(set(names)):
                    errors.append("output_columns_mismatch:" + name)
                # Do not fabricate result types for later stages. Known types can be
                # supplied only by real metadata; UNKNOWN still permits name binding.
                available[name] = {column: "UNKNOWN" for column in output.columns}
            except Exception:
                errors.append("column_resolution_failed:" + name)
            created.add(name)
            parsed.append({"artifact": artifact.name, "statement": index + 1, "output": name})
    for name in set(outputs) - created:
        errors.append("missing_output:" + name)
    for check in ("sql_static", "object_binding", "column_binding", "output_contract"):
        checks[check] = "failed" if errors else "passed"
    notes.append("Static PG grammar only; exact target-version execution has not occurred")
    notes.append("Source claims establish traceability, not source-semantic equivalence")
    result = report(task_hash, candidate, checks, errors, notes)
    result["statement_inventory"] = parsed
    result.pop("hash")
    result["hash"] = digest(result)
    return result


def report(task_hash, candidate, checks, errors, notes):
    result = {"schema_version": 1, "verifier_version": "complete-schema-exact-number-3", "task_hash": task_hash,
              "candidate_hash": digest(candidate), "checks": checks,
              "errors": sorted(set(errors)), "notes": notes,
              "static_passed": not errors, "release_allowed": False}
    result["hash"] = digest(result)
    return result
