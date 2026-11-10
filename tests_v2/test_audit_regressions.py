"""Synthetic checks for the specific failures reproduced in the evidence audit."""
from fastapi.testclient import TestClient

from fixtures.testifier_lab.compare import compare
from migration_v2.linker import link
from migration_v2.runtime import Runtime
from migration_v2.server import create_app
from migration_v2.task_planner import plan_tasks
from migration_v2.workflow import Workbench
from tests_v2.test_ai_revision_contracts import import_codes
from tests_v2.test_destinations import base


def test_include_recomputes_context_and_reopens_http_gate(tmp_path):
    app = create_app(tmp_path, "local-test", Runtime())
    project = import_codes(app.state.workbench, {"P1": '%include "rules.sas"; %make;'})
    ident = project["id"]
    with TestClient(app, headers={"X-Local-Session":"local-test"}) as client:
        def command(action, payload, status=200):
            nonlocal project
            response = client.post(f"/api/projects/{ident}/commands", json={
                "revision":project["revision"],"action":action,"payload":payload})
            assert response.status_code == status, response.text
            if status == 200:
                project = response.json()
            return response

        plan = {"scenario_id":project["scenario"]["id"],"acceptance":"code_review",
            "target_version":"7.0","target_versions":{"greenplum":"7.0","postgresql":"17.10","clickhouse":"25.8"},
            "targets":["greenplum","postgresql","clickhouse"],"bindings":[],
            "outputs":[{"name":{"parts":["result"]},"storage":"temporary","columns":["id"]}],
            "output_sources":{"result":"work.result"},"validation_scope":"Synthetic literal include recovery"}
        command("save_plan", plan)
        assert "macro_unknown" in command("approve_plan", {}, 400).text
        attachment = {"source_id":"P1","offset":0,"filename":"rules.sas"}
        command("attach_include", {**attachment,"text":"%macro other; %mend;"})
        command("save_plan", plan)
        assert "macro_unknown" in command("approve_plan", {}, 400).text
        command("attach_include", {**attachment,"text":"%macro make; data work.result; id=1; output; run; %mend;"})
        command("save_plan", plan)
        assert not project["task_plan"]["requirements"]
        command("approve_plan", {})
        assert project["gate1"]


def test_comparator_rejects_exact_difference_extra_cells_and_lost_duplicates():
    column = {"name":"x","data_type":"NUMERIC(20,10)"}
    assert not compare([column], [["1.0000000000"]], [["1.0000000001"]])["passed"]
    assert not compare([column], [[1]], [[1, 999]])["passed"]
    assert not compare([column], [[1], [1]], [[1]])["passed"]
    assert compare([{"name":"x","data_type":"DOUBLE PRECISION"}], [[1]], [[1.0000000001]])["passed"]


def test_date_and_catx_reject_saved_error_patterns(tmp_path):
    plan = base(tmp_path)
    result = plan["outputs"][-1]
    result.update(columns=["d","joined"], column_metadata=[{"name":"d","data_type":"DATE"}])
    plan["outputs"] = [result]
    source = "data work.result; set raw.events; joined=catx('|',a,b); run;"
    bad_date = "CASE WHEN id=1 THEN CAST('2024-01-02' AS DATE)-INTERVAL '1 day' END"
    def checked(date, trim):
        sql = f"CREATE TEMP TABLE result AS SELECT {date} AS d, {trim} AS joined FROM lab.events"
        return link(plan, [{"name":"one.sql","sql":sql}], "postgresql", source=source)
    bad = checked(bad_date, "TRIM(TRAILING ' ' FROM ' a ')")
    assert {f["code"] for f in bad["findings"]} >= {"output_date_requires_final_cast","catx_leading_blanks"}
    assert checked(f"CAST({bad_date} AS DATE)", "TRIM(BOTH ' ' FROM ' a ')")["static_passed"]


def test_lag_rejects_order_with_prepended_id(tmp_path):
    plan = base(tmp_path)
    order = {"keys":[{"column":"value","descending":False}],"source_unit":"synthetic-sort"}
    def checked(keys):
        sql = f"CREATE TEMP TABLE result AS SELECT id, LAG(value) OVER (ORDER BY {keys}) AS value FROM stage"
        return link(plan, [{"name":"one.sql","sql":sql}], "postgresql", predecessors={"stage"},
                    expected_writes={"result"}, lag_order=order)
    assert any(f["code"]=="lag_order_mismatch" for f in checked("id,value")["findings"])
    assert checked("value")["static_passed"]


def test_sysget_is_context_and_requests_explicit_value(tmp_path):
    wb = Workbench(tmp_path)
    p = import_codes(wb, {"P1":"%let cutoff=%sysget(SYNTHETIC_LIMIT); data work.result; id=&cutoff; output; run;"})
    p["plan"] = {"parameters":{}}
    plan = plan_tasks(wb, p)
    assert len(plan["tasks"]) == 1
    assert any(r.get("name")=="cutoff" and r.get("environment_variable")=="SYNTHETIC_LIMIT"
               for r in plan["requirements"])
    p["plan"]["parameters"] = {"cutoff":"2"}
    plan = plan_tasks(wb, p)
    assert not plan["requirements"]
    assert plan["tasks"][0]["parameter_assignments"][0]["role"] == "confirmed_external_assignment"


def test_append_force_rejects_positional_rename(tmp_path):
    plan = base(tmp_path)
    plan["source_patterns"] = {plan["bindings"][0]["source_id"]:"raw.events"}
    plan["output_sources"] = {"result":"work.result"}
    plan["outputs"] = [dict(plan["outputs"][-1], columns=["id","x"])]
    plan["allowed_effects"] = {"result":["create","insert"]}
    source = "proc append base=work.result data=raw.events force; run;"
    def checked(value):
        sql = f"CREATE TEMP TABLE result AS SELECT id, CAST(NULL AS INTEGER) AS x FROM lab.events WHERE FALSE; INSERT INTO result(id,x) SELECT id,{value} FROM lab.events;"
        return link(plan,[{"name":"one.sql","sql":sql}],"postgresql",source=source)
    assert any(f["code"]=="append_force_column_alignment" for f in checked("value AS x")["findings"])
    assert checked("CAST(NULL AS INTEGER)").get("static_passed")
    reinsert="INSERT INTO result(id,x) SELECT id,x FROM result UNION ALL SELECT id,CAST(NULL AS INTEGER) FROM lab.events"
    result=link(plan,[{"name":"one.sql","sql":reinsert}],"postgresql",source=source,predecessors={"result"})
    assert any(f["code"]=="append_force_base_reinsert" for f in result["findings"])


def test_upcase_preserves_significant_leading_blanks(tmp_path):
    plan=base(tmp_path)
    plan["bindings"][0]["columns"]=[{"name":"a","data_type":"VARCHAR(20)"}]
    plan["outputs"]=[dict(plan["outputs"][-1],columns=["upper_code"])]
    source="data work.result; set raw.events; upper_code=upcase(a); run;"
    def checked(expr):
        return link(plan,[{"name":"one.sql","sql":f"CREATE TEMP TABLE result AS SELECT {expr} AS upper_code FROM lab.events"}],
                    "postgresql",source=source)
    assert checked("UPPER(a)")["static_passed"]
    assert any(f["code"]=="upcase_significant_leading_blanks" for f in checked("UPPER(TRIM(a))")["findings"])
