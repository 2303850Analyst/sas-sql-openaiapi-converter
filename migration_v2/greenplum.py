"""Typed Greenplum table creation. The first INSERT is the data part of CREATE."""
import re

import sqlglot
from sqlglot import exp

from .verification import table_key

TYPE = re.compile(r"(?:SMALLINT|INTEGER|BIGINT|REAL|DOUBLE PRECISION|BOOLEAN|TEXT|DATE|"
                  r"TIMESTAMP(?:\([0-6]\))?|TIMESTAMPTZ(?:\([0-6]\))?|"
                  r"(?:VAR)?CHAR(?:\([1-9]\d{0,6}\))?|NUMERIC(?:\([1-9]\d{0,3},\s*\d{1,4}\))?)")
QUERY = (exp.Select, exp.Union, exp.Intersect, exp.Except)


def physical_type(value):
    value = " ".join(value.upper().split())
    value = re.sub(r"^TIMESTAMP(\(\d+\))? WITH TIME ZONE$", r"TIMESTAMPTZ\1", value)
    value = value.removesuffix(" WITHOUT TIME ZONE")
    value = value.replace("CHARACTER VARYING", "VARCHAR").replace("CHARACTER", "CHAR")
    value = {"INT": "INTEGER", "DECIMAL": "NUMERIC", "FLOAT": "DOUBLE PRECISION"}.get(value, value)
    value = value.replace("DECIMAL(", "NUMERIC(").replace(", ", ",")
    if value == "CHAR":
        value = "CHAR(1)"
    if re.fullmatch(r"NUMERIC\(\d+\)", value):
        value = value[:-1] + ",0)"
    if not TYPE.fullmatch(value):
        raise ValueError("Unsupported Greenplum physical type: " + value)
    return value


def columns_for(output):
    metadata = {c["name"]: c for c in output.get("column_metadata", ())}
    return [metadata[c] for c in output["columns"]] if set(metadata) == set(output["columns"]) else []


def character_transport(query, columns, semantics):
    # Canonicalize character missing only. Preserve the exact raw spelling of
    # nonblank values, including leading/trailing spaces and non-ASCII padding.
    if (semantics.get("character_missing") != "null_empty_ascii_spaces"
            or semantics.get("character_storage") != "unpadded_varchar"
            or query.named_selects != [c["name"] for c in columns]):
        return query.copy()
    variable_text = {c["name"] for c in columns
                     if physical_type(c["data_type"]).startswith("VARCHAR") or physical_type(c["data_type"]) == "TEXT"}
    if not variable_text:
        return query.copy()
    source = "_sas_char_transport"
    projection = []
    for c in columns:
        name = c["name"]
        value = exp.column(name, table=source, quoted=True)
        if name in variable_text:
            blank = exp.EQ(this=exp.Trim(this=value.copy(), position="TRAILING",
                expression=exp.Literal.string(" ")), expression=exp.Literal.string(""))
            value = exp.Case(ifs=[exp.If(this=blank, true=exp.Null())], default=value)
        projection.append(exp.alias_(value,name,quoted=True))
    return exp.select(*projection).from_(query.copy().subquery(source))


def lower_tables(artifacts, plan):
    outputs = {".".join(o["name"]["parts"]): o for o in plan["outputs"]}
    result = []
    for artifact in artifacts:
        try:
            trees = sqlglot.parse(artifact["sql"], read="postgres", error_level="RAISE")
            if any(tree is None or isinstance(tree, exp.Command) for tree in trees):
                return artifacts
            statements = []
            changed = False
            for tree in trees:
                if (not isinstance(tree, exp.Create) or tree.args.get("kind") != "TABLE"
                        or not isinstance(tree.this, exp.Table) or not isinstance(tree.expression, QUERY)
                        or any(v for k, v in tree.args.items() if k not in {"this", "kind", "expression", "properties"})
                        or any(not isinstance(p, exp.TemporaryProperty) for p in
                               (tree.args["properties"].expressions if tree.args.get("properties") else []))):
                    statements.append(tree)
                    continue
                output = outputs.get(table_key(tree.this))
                columns = columns_for(output) if output else []
                if not columns:
                    statements.append(tree)
                    continue
                definitions = [exp.ColumnDef(this=exp.to_identifier(c["name"], quoted=True),
                    kind=exp.DataType.build(physical_type(c["data_type"]), dialect="postgres"),
                    constraints=[exp.ColumnConstraint(kind=exp.NotNullColumnConstraint())]
                    if c.get("nullable") is False else []) for c in columns]
                create = tree.copy()
                create.set("this", exp.Schema(this=tree.this.copy(), expressions=definitions))
                create.set("expression", None)
                insert = exp.Insert(this=exp.Schema(this=tree.this.copy(),
                    expressions=[d.this.copy() for d in definitions]),
                    expression=character_transport(tree.expression, columns, plan.get("source_semantics", {})))
                statements.extend((create, insert))
                changed = True
            result.append({**artifact, "sql": "\n".join(t.sql(dialect="postgres",
                unsupported_level=sqlglot.ErrorLevel.RAISE) + ";" for t in statements)} if changed else artifact)
        except (ValueError, sqlglot.errors.SqlglotError):
            return artifacts
    return result


def paired_statements(trees):
    """Represent only a plain, adjacent CREATE and initial INSERT as one checked effect."""
    index = 0
    while index < len(trees):
        tree = trees[index]
        following = trees[index + 1] if index + 1 < len(trees) else None
        paired = False
        if (isinstance(tree, exp.Create) and tree.args.get("kind") == "TABLE" and tree.expression is None
                and isinstance(tree.this, exp.Schema) and isinstance(tree.this.this, exp.Table)
                and not any(v for k, v in tree.args.items() if k not in {"this","kind","properties"})
                and isinstance(following, exp.Insert) and isinstance(following.this, exp.Schema)
                and isinstance(following.this.this, exp.Table) and isinstance(following.expression, QUERY)
                and not any(v for k, v in following.args.items() if k not in {"this","expression"})
                and tree.this.this == following.this.this
                and all(isinstance(d, exp.ColumnDef) for d in tree.this.expressions)
                and all(isinstance(c, exp.Identifier) for c in following.this.expressions)
                and [d.this for d in tree.this.expressions] == following.this.expressions):
            tree = tree.copy()
            tree.set("expression", following.expression.copy())
            paired = True
        yield index, tree, paired
        index += 2 if paired else 1


def valid_definition(tree, columns):
    if not isinstance(tree.this, exp.Schema) or len(tree.this.expressions) != len(columns):
        return False
    for definition, column in zip(tree.this.expressions, columns):
        required = column.get("nullable") is False
        if definition.name != column["name"] or not isinstance(definition.kind, exp.DataType):
            return False
        try:
            if physical_type(definition.kind.sql(dialect="postgres")) != physical_type(column["data_type"]):
                return False
        except ValueError:
            return False
        constraints = definition.constraints
        if required:
            if (len(constraints) != 1 or constraints[0].this
                    or not isinstance(constraints[0].kind, exp.NotNullColumnConstraint)
                    or constraints[0].kind.args.get("allow_null")):
                return False
        elif constraints:
            return False
    return True
