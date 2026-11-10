"""Synthetic PostgreSQL adversarial tests. Dedicated semantic fixture only."""
import hashlib
import json
import os
from pathlib import Path
import psycopg
import pytest
from fixtures.semantic_eval.corpus import archive
from migration_v2.workflow import Workbench
from migration_v2.execution import execute
from migration_v2.runtime import DatabaseProfile
from tools.run_semantic_eval import prepare, seed, schemas

pytestmark = pytest.mark.skipif(os.environ.get('SAS_V2_TEST_SEMANTIC')!='1',reason='Dedicated synthetic fixture required')


@pytest.mark.parametrize('case',['precision','omitted_column','qualified'])
def test_previously_confirmed_executor_failures(tmp_path,case):
    col={'name':'id','data_type':'NUMERIC(38,0)' if case=='precision' else 'INTEGER','nullable':False}
    names=['first','second'] if case=='qualified' else ['result']
    c={'id':case,'source':'data work.result; set source.events; run;' if case!='qualified' else
       'data work.first; set source.events; run; data work.second; set work.first; run;',
       'inputs':{'events':{'columns':[col],'unique_keys':[['id']]}},'outputs':{name:[col] for name in names},'negative':False}
    raw=archive(c); c['archive_sha256']=hashlib.sha256(raw).hexdigest()
    (tmp_path/'frozen').mkdir(); (tmp_path/'frozen'/(case+'.egp')).write_bytes(raw)
    wb=Workbench(tmp_path/'state');p=prepare(wb,c,'redesign',tmp_path)
    a=[['123456789012345678901234567890']] if case=='precision' else [[1],[2]]
    b=[['123456789012345678901234567891']] if case=='precision' else a
    # Each schema belongs exclusively to the dedicated synthetic stand.
    if case=='omitted_column':
        with psycopg.connect(host='127.0.0.1',port=55439,dbname='semantic_eval',user='fixture_owner',password='synthetic_owner_only') as conn:
            conn.execute('DROP SCHEMA IF EXISTS eval_omitted_column_ref CASCADE')
    seed(c,{'inputs':{'events':a},'expected':{name:b for name in names}})
    if case=='omitted_column':
        with psycopg.connect(host='127.0.0.1',port=55439,dbname='semantic_eval',user='fixture_owner',password='synthetic_owner_only') as conn:
            conn.execute('ALTER TABLE eval_omitted_column_ref.result ADD COLUMN amount NUMERIC(18,2)')
    if case=='qualified':
        plan=p['plan']
        for o in plan['outputs']:o.update(name={'parts':['eval_qualified_target',o['name']['parts'][0]]},storage='persistent')
        for comp in plan['comparisons']:comp['output']={'parts':['eval_qualified_target',comp['output']['parts'][0]]}
        p=wb.command(p['id'],p['revision'],'save_plan',plan)
        p=wb.command(p['id'],p['revision'],'approve_plan',{})
        query='CREATE TABLE eval_qualified_target.first AS SELECT id FROM eval_qualified_src.events; CREATE TABLE eval_qualified_target.second AS SELECT eval_qualified_target.first.id FROM eval_qualified_target.first;'
    else: query=f'CREATE TEMP TABLE result AS SELECT id FROM eval_{case}_src.events;'
    candidate={'schema_version':1,'task_hash':p['task']['hash'],'artifacts':[{'name':'001.sql','sql':query}],
        'source_claims':[r['id'] for r in p['context']['regions'] if r['kind']!='layout_or_comment'],'assumptions':[]}
    p=wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate)})
    cfg=DatabaseProfile(host='127.0.0.1',port=55439,dbname='semantic_eval',user='migration_validator',sslmode='disable',version_prefix='17.10',reference_schemas=(schemas(c)[1],))
    if case=='omitted_column':
        with pytest.raises(ValueError,match='Complete output/reference schema'):
            execute(wb,p,cfg)
    else:
        evidence=execute(wb,p,cfg)
        assert evidence['checks']['comparison']==('failed' if case=='precision' else 'passed')
        assert not evidence['release_allowed']
