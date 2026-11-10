from migration_v2.linker import link


def test_clickhouse_missing_is_preserved_across_left_join_and_month_projection():
    source = lambda name, columns: {"source_id": name, "target": {"parts": name.split(".")}, "columns": columns}
    col = lambda name, dtype, nullable: {"name": name, "data_type": dtype, "nullable": nullable}
    outputs = [
        {"name": {"parts": ["joined"]}, "storage": "temporary", "role": "intermediate",
         "columns": ["observed_at", "segment"], "column_metadata": []},
        {"name": {"parts": ["mart", "monthly"]}, "storage": "persistent", "role": "final",
         "columns": ["month", "segment"], "column_metadata": []},
    ]
    plan = {"outputs": outputs, "bindings": [
        source("src.sales", [col("id", "INTEGER", False), col("observed_at", "DATE", True)]),
        source("src.customers", [col("id", "INTEGER", False), col("segment", "VARCHAR", False)]),
    ]}
    first = {"name": "01.sql", "sql": "CREATE TEMPORARY TABLE joined ENGINE=Memory AS "
             "SELECT a.observed_at, b.segment FROM src.sales a LEFT JOIN src.customers b "
             "ON a.id=b.id SETTINGS join_use_nulls=1;"}
    second = {"name": "02.sql", "sql": "CREATE TABLE mart.monthly (month Date, segment String) "
              "ENGINE=MergeTree ORDER BY tuple() AS "
              "SELECT toStartOfMonth(DATE(observed_at)) AS month, segment FROM joined GROUP BY month, segment;"}
    result = link(plan, [first, second], "clickhouse")
    assert [f["code"] for f in result["findings"]].count("physical_nullable_output_required") == 2
    assert result["statements"][0]["nullable_columns"] == ["observed_at", "segment"]
    from migration_v2.linker import preserve_clickhouse_nullable
    repaired = preserve_clickhouse_nullable([first, second], result)
    assert repaired[0] == first
    assert "month Nullable(Date), segment Nullable(String)" in repaired[1]["sql"]
    assert repaired[1]["sql"].split(" AS ", 1)[1] == second["sql"].split(" AS ", 1)[1]
    assert link(plan, repaired, "clickhouse")["static_passed"]




def test_boolean_null_proof_does_not_treat_an_or_branch_as_guaranteed():
    from sqlglot import parse_one
    from migration_v2.clickhouse_nulls import nonnull_filters
    def proof(condition):
        return nonnull_filters(parse_one("SELECT id FROM t WHERE " + condition).args["where"].this)
    assert proof("NOT (a IS NULL OR b IS NULL OR c < 0)") == {("", "a"), ("", "b")}
    assert proof("a IS NOT NULL OR b IS NOT NULL") == set()
    assert proof("a IS NOT NULL AND b IS NOT NULL") == {("", "a"), ("", "b")}
