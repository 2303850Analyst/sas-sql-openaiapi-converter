"""Narrow physical lowering for a proven, explicitly typed ClickHouse AVG."""
import re

import sqlglot
from sqlglot import exp

from app.sas_lexer import lex_statements
from .contracts import digest


DECIMAL = re.compile(r"(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)",re.I)


def source_averages(source):
    statements,_=lex_statements(source)
    sas=" ".join(s["masked"] for s in statements)
    if re.search(r"\bproc\s+sql\b",sas,re.I):
        return {alias.casefold():column.casefold() for column,alias in re.findall(
            r"\bavg\s*\(\s*(\w+)\s*\)\s+as\s+(\w+)",sas,re.I)}
    if re.search(r"\bproc\s+(?:means|summary)\b",sas,re.I):
        single=re.search(r"\bvar\s+(\w+)\s*;",sas,re.I)
        mean=re.search(r"\bmean\s*=\s*(\w+)",sas,re.I)
        if single and mean:
            return {mean[1].casefold():single[1].casefold()}
    return {}


def is_repairable_average(expression, column):
    node=expression.unalias()
    while isinstance(node,(exp.Cast,exp.Paren)):
        node=node.this
    referenced={n.name.casefold() for n in node.find_all(exp.Column)}
    if referenced!={column}:
        return False
    if isinstance(node,(exp.Avg,exp.CombinedAggFunc,exp.Anonymous)):
        return isinstance(node,exp.Avg) or str(node.this).casefold() in {"avg","avgornull"}
    if isinstance(node,(exp.If,exp.Case)):
        functions={n.name.casefold() for n in node.find_all(exp.Anonymous)}
        return "dividedecimal" in functions and any(isinstance(n,exp.Count) for n in node.walk()) and not any(
            isinstance(n,exp.Subquery) for n in node.walk())
    return False


def lower_decimal_averages(candidate, source, plan):
    """Return a new candidate and provenance, or None if the proof is incomplete."""
    if plan.get("source_semantics",{}).get("numeric_arithmetic")!="exact_decimal_transport":
        return None
    averages=source_averages(source)
    if not averages or len(candidate.artifacts)!=1 or candidate.assumptions:
        return None
    original=candidate.artifacts[0]
    try:
        trees=sqlglot.parse(original.sql,read="clickhouse",error_message_context=0)
    except sqlglot.errors.ParseError:
        return None
    if len(trees)!=1 or not isinstance(trees[0],exp.Create):
        return None
    tree=trees[0]
    query=tree.expression
    destination=tree.this.this if isinstance(tree.this,exp.Schema) else tree.this
    if not isinstance(query,exp.Select) or not isinstance(destination,exp.Table):
        return None
    output=next((o for o in plan["outputs"] if o["name"]["parts"]==[p.name for p in destination.parts]),None)
    if output is None:
        return None
    source_clause=query.args.get("from_")
    origin=source_clause.this if source_clause else None
    if not isinstance(origin,exp.Table) or query.args.get("joins") or query.args.get("with_"):
        return None
    binding=next((b for b in plan["bindings"] if b.get("schema_complete") and b["target"]["parts"]==[p.name for p in origin.parts]),None)
    if binding is None:
        return None
    source_columns={c["name"].casefold():c for c in binding["columns"]}
    types={c["name"].casefold():c for c in output["column_metadata"]}
    edited=[]
    for projection in query.selects:
        alias=projection.alias_or_name.casefold()
        name=averages.get(alias)
        if not name or name not in source_columns or alias not in types or not is_repairable_average(projection,name):
            continue
        input_type=DECIMAL.fullmatch(source_columns[name]["data_type"])
        output_type=DECIMAL.fullmatch(types[alias]["data_type"])
        if not input_type or not output_type or types[alias].get("nullable") is False:
            continue
        precision,scale=map(int,output_type.groups())
        # Up to 20 digits in the native count. Preserve enough quotient digits
        # to decide the storage rounding even adjacent to a midpoint.
        internal=max(int(input_type[2]),scale)+21
        if not 1<=precision<=76 or not 0<=scale<=precision or internal>38:
            continue
        col=exp.to_identifier(source_columns[name]["name"],quoted=True).sql(dialect="clickhouse")
        nullable=f"Nullable(Decimal({precision}, {scale}))"
        computed=(f"CASE WHEN count({col})=0 THEN CAST(NULL AS {nullable}) ELSE "
            f"CAST(round(divideDecimal(sumOrNull({col}),toDecimal128(greatest(count({col}),1),0),{internal}),{scale}) "
            f"AS {nullable}) END")
        projection.set("this",sqlglot.parse_one(computed,read="clickhouse"))
        edited.append({"output":alias,"input":name,"division_scale":internal,"storage_scale":scale})
    if not edited:
        return None
    sql=tree.sql(dialect="clickhouse")
    if sql==original.sql:
        return None
    updated=candidate.model_copy(update={"artifacts":(original.model_copy(update={"sql":sql}),)})
    return updated,{"rule":"typed-decimal-average-1","original_sql_sha256":digest(original.sql.encode()),
                    "new_sql_sha256":digest(sql.encode()),"expressions":edited}
