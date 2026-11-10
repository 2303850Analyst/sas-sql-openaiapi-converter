"""Execute the exact linked Greenplum scenario in a sandbox and roll it back."""
import os
import re

import psycopg
from psycopg import sql

from .contracts import digest
from .destinations import resolve
from .execution import relation, row_multiset
from .greenplum import columns_for, physical_type
from .linker import assemble_sql, link


def _catalog(cur, name):
    cur.execute("""SELECT attname, format_type(atttypid, atttypmod), attnotnull
                   FROM pg_catalog.pg_attribute WHERE attrelid=%s::regclass
                   AND attnum>0 AND NOT attisdropped ORDER BY attnum""",
                (sql.Identifier(*name).as_string(cur),))
    return [{"name":n,"type":physical_type(t),"not_null":nn} for n,t,nn in cur.fetchall()]


def _read_only(cur, name):
    cur.execute("""SELECT has_table_privilege(%s, 'INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES')
                   OR pg_has_role(relowner,'MEMBER') FROM pg_catalog.pg_class WHERE oid=%s::regclass""",
                (name, name))
    if cur.fetchone()[0]:
        raise ValueError("Greenplum inputs and references must be read-only for the test role")


def _verify_session(cur, profile, approved_version):
    cur.execute("SELECT version()")
    product = cur.fetchone()[0]
    match = re.search(r"\(Greenplum Database (\d+(?:\.\d+)+)\b", product)
    if not match or not all(match[1] == prefix or match[1].startswith(prefix + ".")
                           for prefix in (profile.version_prefix, approved_version)):
        raise ValueError("Actual Greenplum version differs from the approved profile")
    cur.execute("""SELECT 1 FROM pg_catalog.pg_roles WHERE
                   (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication)
                   AND pg_has_role(oid,'MEMBER')""")
    if cur.fetchall():
        raise ValueError("Use a restricted Greenplum test role")
    cur.execute("""SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg_temp_%'
                   AND nspname NOT LIKE 'pg_toast%' AND has_schema_privilege(oid,'CREATE')""")
    if {row[0] for row in cur.fetchall()} - set(profile.write_schemas):
        raise ValueError("Greenplum test role can create objects outside sandbox schemas")
    return match[1]


def execute(wb, project, profile, cancelled, holder):
    plan = project["plan"]
    pipeline = project["pipeline"]
    result = pipeline.get("profiles", {}).get("greenplum", {})
    if not wb.code_review_current(project) or not result.get("passed") or not result.get("scenarios"):
        raise ValueError("Greenplum needs current linked code and independent review")
    approved = plan.get("target_versions", {}).get("greenplum", plan["target_version"])
    effective = resolve(plan)
    physical = effective["plan"]
    bindings = {tuple(entry["target"]["parts"]):entry for binding in physical["bindings"]
                for entry in (binding.get("members") or [binding])}
    comparisons = {".".join(c["output"]["parts"]): c for c in plan["comparisons"]}
    references = {tuple(c["reference"]["parts"]) for c in comparisons.values()}
    if any(len(name) != 2 or name[0] not in profile.reference_schemas for name in references):
        raise ValueError("Greenplum references must be in an approved read-only schema")
    if any(len(name) != 2 or name[0] in profile.write_schemas for name in bindings):
        raise ValueError("Greenplum input must be schema-qualified and outside the sandbox")
    destinations = {".".join(r["destination"]["parts"]):r for r in effective["registry"]}
    if any(row["storage"] == "persistent" and
           (not row["test_suffix"] or row["destination"]["parts"][0] not in profile.write_schemas)
           for row in destinations.values()):
        raise ValueError("Greenplum execution requires Testifier destinations in approved sandbox schemas")
    if any(r["logical"] not in comparisons for r in destinations.values()):
        raise ValueError("Supply a Greenplum reference for every output, including temporary tables")
    outputs = {".".join(o["name"]["parts"]): o for o in physical["outputs"]}
    if any(not columns_for(o) for o in outputs.values()):
        raise ValueError("Greenplum execution requires complete typed output metadata")
    kwargs = dict(host=profile.host, port=profile.port, dbname=profile.dbname, user=profile.user,
                  password=os.environ.get(profile.password_env, ""), sslmode=profile.sslmode,
                  connect_timeout=10, application_name="SAS_Migration_V2_GP_validation")
    if profile.sslrootcert:
        kwargs["sslrootcert"] = profile.sslrootcert
    checks, scenarios, input_hashes = [], {}, {}
    for sid, scenario in result["scenarios"].items():
        if cancelled():
            raise ValueError("Job cancelled")
        names = set(physical["scenario_outputs"].get(sid, outputs))
        subset = {**physical, "outputs": [o for k,o in outputs.items() if k in names]}
        entrypoint = assemble_sql(scenario["artifacts"], "greenplum")
        if not scenario["passed"] or entrypoint != scenario["entrypoint"] or not entrypoint["sql"]:
            raise ValueError("Greenplum scenario is incomplete or differs from its reviewed SQL")
        linked = link(subset, [{"name":"scenario.sql","sql":entrypoint["sql"]}], "greenplum")
        if not linked["static_passed"]:
            raise ValueError("Greenplum scenario failed current static checks")
        with psycopg.connect(**kwargs) as conn:
            if holder is not None:
                holder["connection"] = conn
            try:
                cur = conn.cursor()
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("""SELECT set_config('statement_timeout',%s,true), set_config('lock_timeout','2000',true),
                               set_config('search_path','pg_catalog,pg_temp',true), set_config('timezone','UTC',true)""",
                            (str(profile.statement_timeout_ms),))
                version = _verify_session(cur, profile, approved)
                for name in sorted(set(bindings) | references):
                    _read_only(cur, sql.Identifier(*name).as_string(cur))
                fingerprints = {}
                for name, binding in bindings.items():
                    actual = {c["name"]:c["type"] for c in _catalog(cur, name)}
                    columns = binding["columns"]
                    expected = {c["name"]:physical_type(c["data_type"]) for c in columns}
                    if (binding.get("schema_complete") and actual != expected
                            or any(actual.get(k) != t for k,t in expected.items())):
                        raise ValueError("Greenplum input schema differs from contract: " + ".".join(name))
                    fields = [c["name"] for c in columns]
                    rows, _ = relation(cur, name, fields, profile.max_rows)
                    if any(row[i] is None for row in rows for i,c in enumerate(columns) if c.get("nullable") is False):
                        raise ValueError("Greenplum input violates NOT NULL: " + ".".join(name))
                    for key in binding.get("unique_keys", ()):
                        positions = [fields.index(k) for k in key]
                        if len({tuple(r[i] for i in positions) for r in rows}) != len(rows):
                            raise ValueError("Greenplum input violates a declared unique key: " + ".".join(name))
                    fingerprints[".".join(name)] = digest({"schema":expected,"rows":sorted(row_multiset(rows).items())})
                input_hashes[sid] = digest(fingerprints)
                for name in names:
                    parts = outputs[name]["name"]["parts"]
                    if len(parts) == 2:
                        cur.execute("SELECT to_regclass(%s)", (sql.Identifier(*parts).as_string(cur),))
                        if cur.fetchone()[0] is not None:
                            raise ValueError("Greenplum test destination already exists: " + name)
                cur.execute(entrypoint["sql"], prepare=False)
                for name in sorted(names):
                    if cancelled():
                        raise ValueError("Job cancelled")
                    output = outputs[name]
                    ref = comparisons[destinations[name]["logical"]]
                    declared = columns_for(output)
                    parts = output["name"]["parts"]
                    actual_schema = _catalog(cur, parts)
                    ref_schema = _catalog(cur, ref["reference"]["parts"])
                    expected = [(c["name"], physical_type(c["data_type"])) for c in declared]
                    if [(c["name"],c["type"]) for c in ref_schema] != expected:
                        raise ValueError("Reference schema differs from contract: " + name)
                    actual_columns = [(c["name"],c["type"]) for c in actual_schema]
                    schema_passed = actual_columns == expected and all(
                        c.get("nullable") is None or r["not_null"] == (c["nullable"] is False)
                        for r,c in zip(actual_schema,declared))
                    rows, _ = relation(cur, parts, output["columns"], profile.max_rows)
                    oracle, _ = relation(cur, ref["reference"]["parts"], output["columns"], profile.max_rows)
                    a, e = row_multiset(rows), row_multiset(oracle)
                    checks.append({"scenario":sid,"output":name,"reference":".".join(ref["reference"]["parts"]),
                        "provenance":ref["provenance"],"reference_run_id":ref["reference_run_id"],
                        "rows_actual":len(rows),"rows_expected":len(oracle),"extra_rows":sum((a-e).values()),
                        "missing_rows":sum((e-a).values()),"schema_compatible":schema_passed,
                        "actual_schema":actual_schema,"expected_schema":declared,
                        "actual_hash":digest(sorted(a.items())),"reference_hash":digest(sorted(e.items())),
                        "passed":schema_passed and a==e})
                scenarios[sid] = entrypoint["sha256"]
            except psycopg.Error as error:
                raise ValueError("Greenplum validation failed (SQLSTATE " + (error.sqlstate or "connection") + ")") from None
            finally:
                try:
                    conn.rollback()
                finally:
                    if holder is not None:
                        holder.pop("connection", None)
    return {"target":"greenplum","version":version,"entrypoints":scenarios,"comparison":checks,
            "input_snapshot_hashes":input_hashes,
            "passed":bool(checks) and all(c["passed"] for c in checks),"comparison_policy":"exact_typed_multiset_and_catalog",
            "mode":"exact_export_rollback","reference_scope":"independent_control"}
