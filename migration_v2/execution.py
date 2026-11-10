"""Restricted PostgreSQL test execution, one transaction/session, always rolled back.

Persistent destinations are mapped to isolated TEMP tables in the execution copy.
The reviewed/exported SQL is preserved. No arbitrary test SQL is accepted.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal
import json
import os
import uuid

import psycopg
from psycopg import sql
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .contracts import Plan, Candidate, parse_json, digest
from .verification import verify, table_key


def canonical(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Nonfinite numeric value requires an explicit comparison policy")
        # as_tuple is exact and does not consult the process Decimal context.
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        if not any(digits):
            return ["number", "0"]
        while digits[-1] == 0:
            digits.pop()
            exponent += 1
        return ["number", str(sign) + ":" + "".join(map(str, digits)) + "e" + str(exponent)]
    if isinstance(value, (datetime, date, time)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, str):
        return ["text", value]
    raise ValueError("Unsupported comparison value type")


def row_multiset(rows):
    return Counter(json.dumps([canonical(v) for v in row], ensure_ascii=False, separators=(",", ":")) for row in rows)


def relation(cursor, name, columns, max_rows):
    cursor.execute(sql.SQL("SELECT {} FROM {} LIMIT %s").format(
        sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(*name)), (max_rows + 1,))
    rows = cursor.fetchall()
    if len(rows) > max_rows:
        raise ValueError("Comparison row limit exceeded; this run cannot be accepted")
    schema = [{"name": col.name, "oid": col.type_code} for col in cursor.description]
    # The values stay in memory and are not included in evidence, logs or the model request.
    return rows, schema


def complete_columns(cursor, name):
    cursor.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(*name)))
    return tuple(c.name for c in cursor.description)


def isolate_statement(tree, outputs):
    """Rewrite bound tables AND qualified columns, preserving CTE/alias scope."""
    tree = tree.copy()
    original_name = table_key(tree.this)
    for scope in traverse_scope(tree.expression):
        for alias, (_, source) in scope.selected_sources.items():
            if not isinstance(source, exp.Table) or table_key(source) not in outputs:
                continue
            original_db = source.db
            original_table = source.name
            for column in scope.columns:
                if column.table == alias and (not column.db or column.db == original_db):
                    column.set("db", None)
                    column.set("catalog", None)
                elif column.table == original_table and column.db == original_db and not source.alias:
                    column.set("db", None)
                    column.set("catalog", None)
            replacement = exp.to_table(outputs[table_key(source)][0])
            replacement.set("alias", source.args.get("alias") or exp.TableAlias(this=exp.to_identifier(source.alias_or_name)))
            source.replace(replacement)
    tree.set("this", exp.to_table(outputs[original_name][0]))
    tree.set("properties", exp.Properties(expressions=[exp.TemporaryProperty()]))
    return tree


def execute(wb, project, profile, cancelled=lambda: False, connection_holder=None):
    if profile is None:
        raise ValueError("Configure a restricted test database locally first")
    plan = parse_json(Plan, json.dumps(project["plan"]))
    candidate = parse_json(Candidate, json.dumps(project["candidate"]))
    obligations = list(dict.fromkeys(r["id"] for r in project["context"]["regions"] if r["kind"] != "layout_or_comment"))
    evidence = verify(project["task"]["hash"], plan, candidate, obligations)
    if not evidence["static_passed"]:
        raise ValueError("SQL must pass the independent static verifier before execution")
    if plan.target != profile.target:
        raise ValueError("Configured execution target differs from the approved contract")
    finals = {o.name.key for o in plan.outputs if o.role == "final"}
    comparisons = {c.output.key: c for c in plan.comparisons}
    if not finals <= set(comparisons):
        raise ValueError("Independent references are required for every final output")
    for c in plan.comparisons:
        if len(c.reference.parts) != 2 or c.reference.parts[0] not in profile.reference_schemas:
            raise ValueError("Reference is outside approved read-only reference schemas")
        if c.reference.key in {o.name.key for o in plan.outputs}:
            raise ValueError("Output cannot be its own reference")
    kwargs = dict(host=profile.host, port=profile.port, dbname=profile.dbname, user=profile.user,
                  password=os.environ.get(profile.password_env, ""), sslmode=profile.sslmode,
                  connect_timeout=10, application_name="SAS_Migration_V2_validation")
    if profile.sslrootcert:
        kwargs["sslrootcert"] = profile.sslrootcert
    run_id = uuid.uuid4().hex
    outputs = {o.name.key: ["mv2_" + run_id[:12] + "_" + str(i)] for i, o in enumerate(plan.outputs)}
    checks, steps, fingerprints = [], [], {}
    conn = None
    try:
        conn = psycopg.connect(**kwargs)
        if connection_holder is not None:
            connection_holder["connection"] = conn
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout','2000',true), set_config('search_path','pg_temp,pg_catalog',true)", (str(profile.statement_timeout_ms),))
        cur.execute("SELECT current_setting('server_version'), version(), rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user")
        version, product, *privileged = cur.fetchone()
        if any(privileged):
            raise ValueError("Test database role must not have administrative or bypass-RLS privileges")
        if "Greenplum" in product or not version.startswith(profile.version_prefix) or not (version == plan.target_version or version.startswith(plan.target_version.rstrip('.') + '.')):
            raise ValueError("Actual database version does not match the approved target")
        cur.execute("SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg_temp_%' AND nspname NOT LIKE 'pg_toast%' AND has_schema_privilege(oid,'CREATE')")
        if cur.fetchall():
            raise ValueError("Test role may create only temporary objects, not persistent schema objects")
        for binding in plan.bindings:
            rows, schema = relation(cur, binding.target.parts, [c.name for c in binding.columns], profile.max_rows)
            for column, actual in zip(binding.columns, schema):
                if declared_family(column.data_type) != type_family(actual["oid"]):
                    raise ValueError("Input type differs from the approved contract: " + binding.target.key + "." + column.name)
            positions = {c.name: i for i, c in enumerate(binding.columns)}
            if any(row[i] is None for i, c in enumerate(binding.columns) if c.nullable is False for row in rows):
                raise ValueError("Input violates the approved NOT NULL contract: " + binding.target.key)
            for key in binding.unique_keys:
                keys = [tuple(row[positions[n]] for n in key) for row in rows]
                if len(keys) != len(set(keys)):
                    raise ValueError("Input violates approved uniqueness: " + binding.target.key)
            fingerprints[binding.target.key] = digest({"schema": schema, "rows": sorted(row_multiset(rows).items())})
        input_hash = digest(fingerprints)
        for artifact in candidate.artifacts:
            for index, tree in enumerate(sqlglot.parse(artifact.sql, read="postgres")):
                if cancelled():
                    raise ValueError("Job cancelled")
                original_name = table_key(tree.this)
                tree = isolate_statement(tree, outputs)
                cur.execute(tree.sql(dialect="postgres"))
                steps.append({"artifact": artifact.name, "statement": index + 1, "output": original_name, "status": "passed"})
        for output in plan.outputs:
            if cancelled():
                raise ValueError("Job cancelled")
            if output.name.key not in comparisons:
                continue
            c = comparisons[output.name.key]
            declared = tuple(output.columns)
            if complete_columns(cur, outputs[output.name.key]) != declared or complete_columns(cur, c.reference.parts) != declared:
                raise ValueError("Complete output/reference schema differs from approved columns: " + output.name.key)
            actual, actual_schema = relation(cur, outputs[output.name.key], output.columns, profile.max_rows)
            expected, expected_schema = relation(cur, c.reference.parts, output.columns, profile.max_rows)
            a, e = row_multiset(actual), row_multiset(expected)
            # Numeric width differences are reported separately; values are exact, no float tolerances.
            compatible = all(type_family(x["oid"]) == type_family(y["oid"]) for x, y in zip(actual_schema, expected_schema))
            provenance_matches = (c.provenance == "sas_reference" and c.input_snapshot_hash == input_hash and
                                  c.source_snapshot_hash == project["snapshot"]["hash"] and
                                  c.parameters_hash == digest(plan.parameters))
            checks.append({"output": output.name.key, "reference": c.reference.key,
                           "provenance": c.provenance, "reference_run_id": c.reference_run_id,
                           "source_and_inputs_match_reference": provenance_matches,
                           "rows_actual": len(actual), "rows_expected": len(expected),
                           "extra_rows": sum((a - e).values()), "missing_rows": sum((e - a).values()),
                           "schema_compatible": compatible, "actual_schema": actual_schema,
                           "expected_schema": expected_schema,
                           "actual_hash": digest(sorted(a.items())), "reference_hash": digest(sorted(e.items())),
                           "passed": compatible and a == e})
        passed = all(c["passed"] for c in checks)
        registry_valid = False
        if project.get("reference_registry"):
            from .reference import ReferenceBundle
            registry = parse_json(ReferenceBundle, json.dumps(project["reference_registry"]))
            registry.check_plan(plan, project["snapshot"]["hash"])
            registry_valid = all(c.reference_run_id == registry.run_id for c in plan.comparisons)
        equivalent = passed and registry_valid and all(c["source_and_inputs_match_reference"] for c in checks)
        evidence["checks"].update(execution="passed", comparison="passed" if passed else "failed",
                                  equivalence="passed" if equivalent else "unavailable")
        evidence.update(run_id=run_id, target_version=version, execution_mode="isolated_temporary_rollback",
                        input_snapshot_hash=input_hash, input_fingerprints=fingerprints,
                        comparison=checks, steps=steps, release_allowed=equivalent,
                        reference_registry_hash=digest(project.get("reference_registry")),
                        reference_scope="operator_attested_sas_run" if equivalent else "synthetic_or_unverified_reference")
        evidence["notes"] = [n for n in evidence["notes"] if "execution has not occurred" not in n]
        evidence["notes"].append("All statements executed in one transaction; all test changes rolled back. No production publication.")
        evidence.pop("hash", None)
        evidence["hash"] = digest(evidence)
        return evidence
    except psycopg.Error as error:
        # SQLSTATE is useful; database error text can echo row values or credentials.
        raise ValueError("Database validation failed (SQLSTATE " + (error.sqlstate or "connection") + "); transaction rolled back") from None
    finally:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()
        if connection_holder is not None:
            connection_holder.pop("connection", None)


def type_family(oid):
    if oid in {20, 21, 23, 700, 701, 1700}:
        return "numeric"
    if oid in {18, 19, 25, 1042, 1043}:
        return "text"
    return oid


def declared_family(data_type):
    """Resolve only known built-in types; never interpolate operator metadata into SQL."""
    try:
        kind = exp.DataType.build(data_type, dialect="postgres").this.name
    except Exception:
        raise ValueError("Unsupported declared input type") from None
    if kind in {"TINYINT", "SMALLINT", "INT", "BIGINT", "FLOAT", "DOUBLE", "DECIMAL"}:
        return "numeric"
    if kind in {"CHAR", "VARCHAR", "TEXT"}:
        return "text"
    known = {"BOOLEAN": 16, "DATE": 1082, "TIME": 1083, "TIMESTAMP": 1114,
             "TIMESTAMPTZ": 1184, "INTERVAL": 1186}
    if kind not in known:
        raise ValueError("Unsupported declared input type")
    return known[kind]
