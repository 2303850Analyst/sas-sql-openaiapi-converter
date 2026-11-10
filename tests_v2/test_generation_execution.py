"""Quality and failure regressions. All inputs are synthetic; no live API calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fixtures.v2_pilot.build import archive
from migration_v2.contracts import digest
from migration_v2.examples import load_example
from migration_v2.execution import execute, row_multiset
from migration_v2.generation import Budget, model_payload, propose
from migration_v2.jobs import Jobs
from migration_v2.runtime import Runtime, ModelProfile, DatabaseProfile
from migration_v2.server import create_app
from migration_v2.workflow import Workbench


def prepared(tmp_path, example='ctas'):
    wb = Workbench(tmp_path)
    p = wb.import_egp('Quality_Pilot.egp', archive())
    p = load_example(wb, p, example)
    return wb, wb.command(p['id'], p['revision'], 'approve_plan', {})


def profile(p):
    return ModelProfile(endpoint='https://example.invalid/v1/chat/completions',model='synthetic-model',
                        key_file='not-read.env',key_name='TEST_KEY',input_usd_per_million=.95,
                        output_usd_per_million=4.,max_output_tokens=7000,
                        synthetic_archives=(p['snapshot']['archive_sha256'],))


def candidate(p, query='SELECT station_id FROM sky_raw.stations WHERE active=1'):
    return {'schema_version':1,'task_hash':p['task']['hash'],
            'artifacts':[{'name':'001.sql','sql':'CREATE TEMP TABLE active_ids AS '+query+';'}],
            'source_claims':list(dict.fromkeys(r['id'] for r in p['context']['regions'] if r['kind']!='layout_or_comment')),
            'assumptions':[]}


def test_macro_sort_inventory_and_whole_source_not_reference_in_prompt(tmp_path):
    wb,p=prepared(tmp_path,'pipeline')
    assert {i['original'] for i in p['context']['inputs']} == {'source.stations','source.readings','source.targets','source.calibration'}
    assert not p['blockers']
    body=model_payload(wb,p,profile(p))
    source=wb.source_text(p,'CodeTask-pipeline')
    data=json.loads(body['messages'][1]['content'])
    assert data['sources'][0]['text'] == source
    assert '%make_monthly(active_flag=1)' in source
    assert 'sky_expected' not in json.dumps(body)
    assert 'reference_run_id' not in json.dumps(body)
    p['snapshot']['archive_sha256']='0'*64
    with pytest.raises(ValueError,match='corporate EGP is blocked'):
        model_payload(wb,p,profile(wb.get(p['id'])))


def test_budget_is_atomic_and_survives_restart(tmp_path):
    wb,p=prepared(tmp_path)
    config=profile(p)
    ledger=Budget(wb)
    body={'messages':[{'content':'x'*2000}]}
    def reserve():
        try:return ledger.reserve(config,body)
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:reserve(),range(5)))
    assert sum(r is not None for r in results)==2
    cost=Budget(Workbench(tmp_path)).status()
    assert 0<cost['charged_or_reserved_usd']<=.09
    assert cost['unknown_requests']==2
    ledger.settle(next(r for r in results if r),config,{'prompt_tokens':100,'completion_tokens':200})
    assert ledger.status()['known_usage_cost_usd']==pytest.approx(.000895)


def test_late_model_reply_never_overwrites_new_contract(tmp_path,monkeypatch):
    wb,p=prepared(tmp_path)
    started,finish=threading.Event(),threading.Event()
    def fake(*args):
        started.set(); assert finish.wait(5)
        return {'candidate':candidate(p),'provenance':{'generator':'synthetic_stub'}}
    monkeypatch.setattr('migration_v2.generation.propose',fake)
    jobs=Jobs(wb,Runtime(model=profile(p)))
    j=jobs.submit(p['id'],p['revision'],'generate')
    assert started.wait(3)
    new=wb.command(p['id'],p['revision'],'save_plan',p['plan'])
    finish.set()
    for _ in range(100):
        j=jobs.get(j['id'])
        if j['state']=='stale':break
        time.sleep(.02)
    assert j['state']=='stale'
    assert wb.get(p['id'])['revision']==new['revision']
    assert wb.get(p['id'])['candidate'] is None
    jobs.close()


def test_cancelled_job_preserves_no_candidate(tmp_path,monkeypatch):
    wb,p=prepared(tmp_path); started,finish=threading.Event(),threading.Event()
    def fake(*args):
        started.set(); assert finish.wait(5)
        return {'candidate':candidate(p)}
    monkeypatch.setattr('migration_v2.generation.propose',fake)
    jobs=Jobs(wb,Runtime(model=profile(p)));j=jobs.submit(p['id'],p['revision'],'generate')
    assert started.wait(3);jobs.cancel(j['id']);finish.set()
    for _ in range(100):
        if jobs.get(j['id'])['state']=='cancelled':break
        time.sleep(.02)
    assert jobs.get(j['id'])['state']=='cancelled'
    assert wb.get(p['id'])['candidate'] is None
    jobs.close()


def test_comparison_preserves_duplicates_missing_strings_and_leading_zeros():
    assert row_multiset([(None,),('',),('001',),(0,),(0,)]) != row_multiset([(None,),('',),('001',),(0,)])
    assert row_multiset([(None,)]) != row_multiset([('',)])
    assert row_multiset([('001',)]) != row_multiset([(1,)])


def test_readonly_job_claims_cannot_be_forged_via_http(tmp_path):
    app=create_app(tmp_path,'test-session')
    with TestClient(app) as client:
        client.headers['X-Local-Session']='test-session'
        p=client.post('/api/projects',content=archive()).json()
        r=client.post('/api/projects/'+p['id']+'/commands',json={'revision':1,'action':'record_execution','payload':{'release_allowed':True}})
        assert r.status_code==400
        assert client.post('/api/projects/'+p['id']+'/jobs',json={'revision':1,'kind':'generate'}).status_code==400


@pytest.mark.parametrize('status,finish,error',[(302,'stop','HTTP 302'),(200,'length','incomplete')])
def test_redirect_and_truncated_model_output_fail_closed(tmp_path,monkeypatch,status,finish,error):
    wb,p=prepared(tmp_path);config=profile(p)
    monkeypatch.setattr(ModelProfile,'secret',lambda _: 'synthetic-only-key')
    class Response:
        status_code=status
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_bytes(self):
            yield json.dumps({'choices':[{'finish_reason':finish,'message':{'content':json.dumps(candidate(p))}}],
                              'usage':{'prompt_tokens':100,'completion_tokens':200}}).encode()
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
            assert kwargs['verify'] is not False
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def stream(self,*a,**kwargs):return Response()
    monkeypatch.setattr('migration_v2.generation.httpx.Client',Client)
    with pytest.raises(ValueError,match=error):propose(wb,p,config)
    assert Budget(wb).status()['charged_or_reserved_usd']>0
    assert wb.get(p['id'])['candidate'] is None


def test_restart_marks_interrupted_and_prevents_two_workers(tmp_path):
    wb,p=prepared(tmp_path);jobs=Jobs(wb,Runtime())
    with pytest.raises(ValueError,match='already open'):Jobs(wb,Runtime())
    with closing(wb.connect()) as db,db:
        db.execute("INSERT INTO jobs(id,project_id,revision,kind,state,task_hash,created) VALUES('synthetic-crash',?,?,'generate','running',?,'test')",(p['id'],p['revision'],p['task']['hash']))
    jobs.close(); restarted=Jobs(wb,Runtime())
    assert restarted.get('synthetic-crash')['state']=='interrupted'
    assert wb.get(p['id'])['candidate'] is None
    restarted.close()


def test_failed_recheck_clears_previous_success(tmp_path,monkeypatch):
    wb,p=prepared(tmp_path)
    p=wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate(p))})
    previous=p['evidence'];previous['checks'].update(execution='passed',comparison='passed');previous['comparison']=[{'passed':True}]
    p=wb.record_execution(p['id'],p['revision'],previous)
    def failed(*a,**k):raise ValueError('Synthetic stand unavailable')
    monkeypatch.setattr('migration_v2.execution.execute',failed)
    cfg=DatabaseProfile(host='127.0.0.1',port=55437,dbname='synthetic',user='synthetic',sslmode='disable')
    jobs=Jobs(wb,Runtime(database=cfg));j=jobs.submit(p['id'],p['revision'],'execute')
    jobs.close()
    current=wb.get(p['id'])
    assert current['evidence']['checks']['comparison']=='not_run'
    assert not current['evidence']['release_allowed']
    assert not current['gate2']


@pytest.mark.skipif(os.environ.get('SAS_V2_TEST_POSTGRES')!='1',reason='Explicit synthetic local PostgreSQL fixture required')
def test_pg_values_duplicates_and_rollback(tmp_path):
    wb,p=prepared(tmp_path)
    config=DatabaseProfile(host='127.0.0.1',port=int(os.environ.get('SAS_V2_PG_PORT','55437')),dbname='observatory_lab',user='migration_validator',
                           sslmode='disable',version_prefix='17.10',reference_schemas=('sky_expected',))
    for query,passed in [('SELECT station_id FROM sky_raw.stations WHERE active=1',True),
                          ('SELECT 999 AS station_id FROM sky_raw.stations WHERE active=1',False),
                          ('SELECT station_id FROM sky_raw.stations WHERE active=1 UNION ALL SELECT station_id FROM sky_raw.stations WHERE active=1',False)]:
        p=wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(candidate(p,query))})
        assert p['evidence']['static_passed']  # These semantic mutants deliberately pass syntax.
        result=execute(wb,p,config)
        assert result['checks']['comparison']==('passed' if passed else 'failed')
        assert not result['release_allowed']  # A synthetic reference is never a SAS oracle.
        p=wb.record_execution(p['id'],p['revision'],result)
        with pytest.raises(ValueError,match='independent SAS'):
            wb.command(p['id'],p['revision'],'approve_release',{})
    import psycopg
    with psycopg.connect(host=config.host,port=config.port,dbname=config.dbname,user=config.user,
                         password=os.environ[config.password_env],sslmode='disable') as connection:
        assert connection.execute("SELECT count(*) FROM pg_catalog.pg_class WHERE relname LIKE 'mv2_%'").fetchone()[0]==0
