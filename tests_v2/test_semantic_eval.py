"""Boundaries discovered by the frozen synthetic evaluation; never calls a model."""
from dataclasses import replace
import json
import os

import pytest
import sqlglot

from fixtures.semantic_eval.oracles import validate_sentinels
from migration_v2.execution import declared_family, execute
from migration_v2.verification import allowed_node
from migration_v2.runtime import DatabaseProfile
from migration_v2.generation import decode_candidate
from tests_v2.test_generation_execution import prepared, candidate, profile


def test_independent_reference_sentinels():
    validate_sentinels()


@pytest.mark.parametrize('query,allowed',[
    ("SELECT sum(x) OVER (ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t",True),
    ("SELECT lpad(cast(id AS TEXT),5,'0') FROM t",True),
    ("SELECT lpad(cast(id AS TEXT),1000000000,'0') FROM t",False),
    ("SELECT lpad(cast(id AS TEXT),n,'0') FROM t",False),
    ("SELECT pg_read_file('/secret') FROM t",False),
])
def test_reviewed_expression_boundaries(query,allowed):
    assert all(allowed_node(n) for n in sqlglot.parse_one(query,read='postgres').walk()) is allowed


def test_types_and_explicit_budget_cap(tmp_path):
    assert declared_family('INTEGER') == declared_family('NUMERIC(18,2)') == 'numeric'
    assert declared_family('VARCHAR(40)') == 'text'
    assert declared_family('DATE') != declared_family('TIMESTAMP')
    with pytest.raises(ValueError):declared_family('INT[]')
    wb,p=prepared(tmp_path)
    cfg=profile(p)
    assert cfg.budget_usd==.09
    assert replace(cfg,budget_usd=.85).budget_usd==.85
    with pytest.raises(ValueError):replace(cfg,budget_usd=1.01)


def test_json_fence_transport_never_repairs_or_discards_other_content(tmp_path):
    wb,p=prepared(tmp_path)
    raw=json.dumps(candidate(p))
    plain,kind=decode_candidate(raw)
    fenced,framing=decode_candidate('```json\n'+raw+'\n```')
    assert plain==fenced and kind=='json' and framing=='single_json_fence'
    for bad in ('Explanation\n'+raw,raw+'\n'+raw,'```json\n'+raw+'\n```\nExplanation'):
        with pytest.raises(ValueError):decode_candidate(bad)


@pytest.mark.skipif(os.environ.get('SAS_V2_TEST_POSTGRES')!='1',reason='Explicit synthetic PostgreSQL fixture required')
def test_actual_numeric_input_cannot_use_a_character_contract(tmp_path):
    wb,p=prepared(tmp_path)
    plan=p['plan']
    for binding in plan['bindings']:
        for column in binding['columns']:
            if column['name']=='station_id':column['data_type']='VARCHAR(20)'
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    p=wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate(p))})
    db=DatabaseProfile(host='127.0.0.1',port=int(os.environ.get('SAS_V2_PG_PORT','55437')),
        dbname='observatory_lab',user='migration_validator',sslmode='disable',version_prefix='17.10',reference_schemas=('sky_expected',))
    with pytest.raises(ValueError,match='Input type differs'):
        execute(wb,p,db)
