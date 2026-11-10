"""Regression tests for Greenplum's explicit schema and release evidence."""
import copy

import pytest

from migration_v2.greenplum import lower_tables, physical_type
from migration_v2.linker import link
from migration_v2.runtime import DatabaseProfile


def plan():
    fields = [{"name":"id","data_type":"INTEGER","nullable":False},
              {"name":"band","data_type":"VARCHAR(40)","nullable":True}]
    return {"bindings":[{"source_id":"synthetic","target":{"parts":["source","data"]},"columns":fields}],
            "outputs":[{"name":{"parts":["mart","test_output"]},"storage":"persistent",
                        "role":"final","columns":["id","band"],"column_metadata":fields}],
            "allowed_effects":{"mart.test_output":["create"]}}


def test_greenplum_typed_create_is_one_effect_and_keeps_the_projection():
    original = [{"name":"data.sql","sql":"CREATE TABLE mart.test_output AS SELECT id, band FROM source.data;"}]
    contract = plan()
    assert any(x["code"]=="greenplum_physical_schema" for x in link(contract,original,"greenplum")["findings"])
    assert link(contract,original,"postgresql")["static_passed"]
    typed = lower_tables(original,contract)
    assert 'VARCHAR(40)' in typed[0]["sql"] and '"id" INT NOT NULL' in typed[0]["sql"]
    result = link(contract,typed,"greenplum")
    assert result["static_passed"] and [s["action"] for s in result["statements"]]==["create"]
    assert lower_tables(typed,contract)==typed


@pytest.mark.parametrize("before,after",[
    ('"id" INT NOT NULL','"id" INT'),
    ('VARCHAR(40)','VARCHAR(41)'),
    ('"band" VARCHAR(40)','"band" VARCHAR(40) DEFAULT \'x\''),
    ('INTO mart.test_output ("id", "band")','INTO mart.test_output ("band", "id")'),
])
def test_greenplum_rejects_wrong_or_unmatched_typed_creation(before,after):
    contract=plan()
    typed=lower_tables([{"name":"data.sql","sql":"CREATE TABLE mart.test_output AS SELECT id, band FROM source.data;"}],contract)
    changed=[{**typed[0],"sql":typed[0]["sql"].replace(before,after)}]
    assert changed!=typed
    assert not link(contract,changed,"greenplum")["static_passed"]


def test_greenplum_append_remains_a_separate_authorized_effect():
    candidate=[{"name":"data.sql","sql":"""CREATE TABLE mart.test_output AS SELECT id, band FROM source.data;
        INSERT INTO mart.test_output (id,band) SELECT id, band FROM source.data;"""}]
    contract=plan()
    typed=lower_tables(candidate,contract)
    assert not link(contract,typed,"greenplum")["static_passed"]
    contract=copy.deepcopy(contract)
    contract["allowed_effects"]["mart.test_output"].append("insert")
    result=link(contract,typed,"greenplum")
    assert result["static_passed"]
    assert [s["action"] for s in result["statements"]]==["create","insert"]


def test_greenplum_local_profile_requires_separate_schemas_and_concrete_version():
    settings={"host":"127.0.0.1","port":55463,"dbname":"sandbox","user":"validator",
              "target":"greenplum","version_prefix":"6.27.1","sslmode":"disable",
              "reference_schemas":("oracle",),"write_schemas":("qa",)}
    assert DatabaseProfile(**settings).target=="greenplum"
    for override in ({"reference_schemas":("qa",)},{"write_schemas":("public",)},
                     {"reference_schemas":("gp_reference",)},{"version_prefix":"unknown"}):
        with pytest.raises(ValueError):
            DatabaseProfile(**(settings | override))
    assert physical_type("character varying(40)")=="VARCHAR(40)"
    assert physical_type("DECIMAL(18, 2)")=="NUMERIC(18,2)"
    assert physical_type("decimal(8)")=="NUMERIC(8,0)"

def test_greenplum_character_transport_requires_explicit_policy_and_keeps_nonblank_text():
    contract=plan()
    original=[{"name":"data.sql","sql":"CREATE TABLE mart.test_output AS SELECT id, band FROM source.data;"}]
    assert "TRIM" not in lower_tables(original,contract)[0]["sql"]
    contract["source_semantics"]={"character_missing":"null_empty_ascii_spaces",
                                  "character_storage":"unpadded_varchar"}
    lowered=lower_tables(original,contract)
    assert """CASE WHEN TRIM(TRAILING ' ' FROM "_sas_char_transport"."band") = '' THEN NULL ELSE "_sas_char_transport"."band" END""" in lowered[0]["sql"]
    assert link(contract,lowered,"greenplum")["static_passed"]
