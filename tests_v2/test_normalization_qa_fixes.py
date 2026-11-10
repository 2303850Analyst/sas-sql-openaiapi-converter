"""Targeted synthetic regressions for confirmed normalization QA findings."""
import json

import pytest

from migration_v2.ai_pipeline import append_force_facts, recover_claim_references, run
from migration_v2.context import task_context
from migration_v2.contracts import digest
from migration_v2.linker import link
from migration_v2.task_planner import declared_writes
from tests_v2.test_ai_pipeline import prepared, transport
from tests_v2.test_destinations import base


def test_clickhouse_decimal_average_allows_bounded_exact_division(tmp_path):
    p=base(tmp_path)
    expression="round(divideDecimal(sumOrNull(value),toDecimal128(greatest(count(value),1),0),24),9)"
    query="CREATE TEMPORARY TABLE result ENGINE=Memory AS SELECT id,"+expression+" AS value FROM lab.events GROUP BY id"
    kwargs={"target":"clickhouse","expected_writes":{"result"}}
    assert link(p,[{"name":"exact.sql","sql":query}],**kwargs)["static_passed"]
    assert not link(p,[{"name":"dynamic.sql","sql":query.replace(",24)",",id)")}],**kwargs)["static_passed"]


def test_product_decimal_lowering_keeps_float_and_unknown_contracts_untouched(tmp_path):
    from migration_v2.contracts import Candidate
    from migration_v2.numeric_rules import lower_decimal_averages
    p=base(tmp_path)
    p["bindings"][0].update(schema_complete=True,columns=[{"name":"value","data_type":"NUMERIC(24,12)"}])
    p["outputs"]=[dict(p["outputs"][-1],columns=["mean_value"],
        column_metadata=[{"name":"mean_value","data_type":"DECIMAL(30,9)","nullable":True}])]
    source="proc sql; create table work.result as select avg(value) as mean_value from raw.events; quit;"
    sql="CREATE TEMPORARY TABLE result ENGINE=Memory AS SELECT avgOrNull(value) AS mean_value FROM lab.events"
    candidate=Candidate.model_validate_json(json.dumps({"schema_version":1,"task_hash":"0"*64,
        "artifacts":[{"name":"mean.sql","sql":sql}],"source_claims":["synthetic"],"assumptions":[]}))
    for mode in ("source_binary_float","unconfirmed"):
        p["source_semantics"]={"numeric_arithmetic":mode}
        assert lower_decimal_averages(candidate,source,p) is None
    p["source_semantics"]={"numeric_arithmetic":"exact_decimal_transport"}
    repaired,proof=lower_decimal_averages(candidate,source,p)
    assert repaired.task_hash==candidate.task_hash and repaired.source_claims==candidate.source_claims
    assert proof["expressions"]==[{"output":"mean_value","input":"value","division_scale":33,"storage_scale":9}]
    assert link(p,[repaired.artifacts[0].model_dump()],"clickhouse",source=source)["static_passed"]


def test_append_force_facts_distinguish_extra_source_and_absent_base_fields():
    source="proc append base=work.result data=src.events force; run;"
    output={"name":{"parts":["result"]},"columns":["id","x"],
            "column_metadata":[{"name":"id","data_type":"INTEGER"},{"name":"x","data_type":"NUMERIC(18,2)"}]}
    binding={"original":"src.events","target":{"parts":["lab","events"]},"schema_complete":True,
             "metadata_status":"user_supplied","columns":[{"name":"id"},{"name":"y"}]}
    target={"original":"work.result"}
    facts=append_force_facts(source,[output],[target],[binding])
    assert facts["retain_existing_rows"] and facts["insert_data_rows_only"]
    assert [(c["base"],c["data"]) for c in facts["columns"]]==[("id","id"),("x",None)]
    assert append_force_facts(source,[output],[target],[dict(binding,schema_complete=False)]) is None
    assert append_force_facts(source.replace("src.events","src.events(keep=id)"),[output],[target],[binding]) is None


@pytest.mark.parametrize("proc", ["summary", "means"])
def test_procedure_header_discovers_only_top_level_data(proc):
    code = f"proc {proc} data=raw.events(where=(data=decoy)) nway; output out=result sum=total; run;"

    class Vault:
        def text(self, source):
            return code

    snapshot = {"sources": [{"id": "p", "status": "available", "sha256": digest(code.encode())}]}
    result = task_context(snapshot, {"source_ids": ["p"]}, Vault())
    assert [entry["original"] for entry in result["inputs"]] == ["raw.events"]


@pytest.mark.parametrize("proc",["summary","means"])
@pytest.mark.parametrize("out",["work.result(drop=_type_)", "result", "work.&out."])
def test_proc_body_output_is_own_write(proc,out):
    unit={"definitions":[],"parameter_assignments":[],
          "source":f"proc {proc} data=raw.events; output out={out} sum=total; run;"}
    assert declared_writes(unit,{"out":"result"})==({"result"},False)


def test_scope_excludes_data_output_and_unresolved_macro_is_unknown():
    unit={"definitions":[],"parameter_assignments":[],"source":"data work.result; output; run; output out=other;"}
    assert declared_writes(unit,{})==({"result"},False)
    unit["source"]="proc summary; output out=%sysfunc(get()); run;"
    assert declared_writes(unit,{})==(set(),True)


def test_protocol_recovery_is_bounded():
    ids=["unit-aaaaaaaaaaaa111111111111:s:181","unit-aaaaaaaaaaaa222222222222:s:181"]
    assert recover_claim_references(["unit-aaaaaaaaaaaa:s:181",ids[1]],ids) is None
    assert recover_claim_references([ids[0].replace("111111111111","")],ids) is None
    assert recover_claim_references([ids[0],ids[0]],ids) is None
    assert recover_claim_references(["unit-bbbbbbbbbbbb:s:181",ids[1]],ids) is None


def test_corrected_metadata_keeps_sql_and_receives_independent_review(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path)
    calls=[]
    mock=transport(calls)
    original=[]
    def answer(wb,profile,body,cancelled):
        content,provenance=mock(wb,profile,body,cancelled)
        if calls[-1]["role"]=="generate" and not original:
            candidate=json.loads(content)
            original.append(candidate["artifacts"])
            claim=candidate["source_claims"][0]
            prefix,sep,suffix=claim.partition(":")
            candidate["source_claims"][0]=prefix[:21]+sep+suffix
            return json.dumps(candidate),provenance
        return content,provenance
    monkeypatch.setattr("migration_v2.ai_pipeline.request_json",answer)
    result=run(wb,p,runtime)
    attempt=next(a for u in result["units"].values() for a in u["attempts"] if a.get("claim_reference_correction"))
    assert attempt["candidate"]["artifacts"]==original[0]
    assert attempt["review"]["review"]["verdict"]=="pass" and result["release_allowed"]


@pytest.mark.parametrize("target,expression,passed",[
    ("clickhouse","lagInFrame(value,1,NULL) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",True),
    ("clickhouse","lagInFrame(value,1,NULL) OVER (ORDER BY id DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",False),
    ("clickhouse","lagInFrame(value,1,0) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",False),
    ("clickhouse","lagInFrame(value,1,NULL) OVER (ORDER BY id ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)",False),
    ("clickhouse","lagInFrame(value,1,NULL) OVER (ORDER BY id)",False),
    ("postgresql","LAST_VALUE(value) IGNORE NULLS OVER (ORDER BY id)",False),
])
def test_local_window_boundaries(tmp_path,target,expression,passed):
    p=base(tmp_path)
    sql="CREATE TEMP TABLE result AS SELECT id,"+expression+" AS value FROM lab.events"
    result=link(p,[{"name":"one.sql","sql":sql}],target,expected_writes={"result"},
                lag_order={"keys":[{"column":"id","descending":False}]} if target=="clickhouse" else None)
    assert result["static_passed"]==passed,result["findings"]
