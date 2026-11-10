"""Preserve explicit missing-value semantics in bounded ClickHouse projections."""
import re

import sqlglot
from sqlglot import exp

from .verification import table_key


def nonnull_filters(value, negated=False):
    """Fields proven present in every row satisfying this Boolean predicate."""
    if isinstance(value, exp.Paren):
        return nonnull_filters(value.this, negated)
    if isinstance(value, exp.Not):
        return nonnull_filters(value.this, not negated)
    if isinstance(value, (exp.And, exp.Or)):
        left = nonnull_filters(value.this, negated)
        right = nonnull_filters(value.expression, negated)
        return left | right if isinstance(value, exp.And) != negated else left & right
    if negated and isinstance(value, exp.Is) and isinstance(value.this, exp.Column) and isinstance(value.expression, exp.Null):
        return {(value.this.table, value.this.name)}
    return set()


def projection_nullability(query, known_nullable, known_nonnull, join_nulls):
    if not isinstance(query, exp.Select) or query.args.get("with_"):
        return set(), set()
    relation = query.args.get("from_")
    if not relation or not isinstance(relation.this, exp.Table):
        return set(), set()
    sources = {relation.this.alias_or_name: table_key(relation.this)}
    null_extended = set()
    for join in query.args.get("joins", []):
        if not isinstance(join.this, exp.Table):
            return set(), set()
        alias = join.this.alias_or_name
        if join_nulls and join.side in {"RIGHT", "FULL"}:
            null_extended.update(sources)
        if join_nulls and join.side in {"LEFT", "FULL"}:
            null_extended.add(alias)
        sources[alias] = table_key(join.this)

    where = query.args.get("where")
    present = nonnull_filters(where.this) if where else set()

    def nullable(value):
        if isinstance(value, exp.Null):
            return True
        if isinstance(value, exp.Column):
            alias = value.table or (next(iter(sources)) if len(sources) == 1 else "")
            if (value.table, value.name) in present:
                return False
            table = sources.get(alias)
            if alias in null_extended or value.name in known_nullable.get(table, ()):
                return True
            if value.name in known_nonnull.get(table, ()):
                return False
            return None
        if isinstance(value, (exp.Date, exp.TimestampTrunc, exp.DateTrunc, exp.Paren)):
            return nullable(value.this)
        if isinstance(value, exp.Cast) and value.to.args.get("nullable") is True:
            return nullable(value.this)
        return None

    facts = {term.alias_or_name: nullable(term.unalias()) for term in query.selects}
    return {field for field, missing in facts.items() if missing is True}, {
        field for field, missing in facts.items() if missing is False}



PRIMITIVE = re.compile(r"(?:u?int(?:8|16|32|64)|float(?:32|64)|bool|date(?:32)?|datetime(?:64)?|"
                       r"string|varchar|char|fixedstring|decimal(?:32|64|128|256)?|numeric|integer|bigint|smallint)"
                       r"(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?", re.I)


def preserve_clickhouse_nullable(artifacts, linked):
    """Wrap audited primitive DDL fields, preserving every query byte."""
    affected = {(row["artifact"], row["statement"]): set(row["nullable_ddl_fields"])
                for row in linked["statements"] if row.get("nullable_ddl_fields")}
    if not affected:
        return artifacts
    edited = []
    for artifact in artifacts:
        replacements = []
        sql = artifact["sql"]
        if any(name == artifact["name"] for name, _ in affected):
            for index, tree in enumerate(sqlglot.parse(sql, read="clickhouse"), start=1):
                fields = affected.get((artifact["name"], index))
                if not fields or not isinstance(tree, exp.Create) or not isinstance(tree.this, exp.Schema):
                    continue
                for col in tree.this.expressions:
                    if not isinstance(col, exp.ColumnDef) or col.name not in fields or col.args.get("constraints"):
                        continue
                    end = col.this.meta.get("end")
                    if end is None:
                        continue
                    start = end + 1
                    while start < len(sql) and sql[start].isspace():
                        start += 1
                    match = PRIMITIVE.match(sql, start)
                    if match and sql[match.end():].lstrip().startswith((",", ")")):
                        replacements.append((start, match.end(), "Nullable(" + match[0] + ")"))
        for start, end, value in sorted(replacements, reverse=True):
            sql = sql[:start] + value + sql[end:]
        edited.append({**artifact, "sql": sql})
    return edited

