"""Synthetic destination/testifier regressions; no API calls."""
import copy
import io
import json
import zipfile
import pytest
from migration_v2.contracts import Plan,parse_json
from migration_v2.destinations import resolve,unit_plan,seed_artifacts
from migration_v2.linker import link
from tests_v2.test_ai_pipeline import prepared,transport
from migration_v2.ai_pipeline import run


def base(tmp_path):return copy.deepcopy(prepared(tmp_path)[1]['plan'])


def test_work_default_and_explicit_address_are_not_string_replacements(tmp_path):
    p=base(tmp_path);p['test_mode']=True
    p['output_destinations']={'result':{'parts':['qa','report']}}
    r=resolve(p)
    assert r['plan']['outputs'][0]['name']['parts']==['stage']
    assert r['plan']['outputs'][0]['storage']=='temporary'
    assert r['plan']['outputs'][1]['name']['parts']==['qa','report_test']
    assert r['plan']['output_sources']['qa.report_test']=='work.result'
    assert r['plan']['bindings']==p['bindings']
    assert p['outputs'][1]['name']['parts']==['result'] # Immutable approved logical contract.


def test_suffix_is_idempotent_and_effects_and_scenarios_follow_names(tmp_path):
    p=base(tmp_path);p.update(test_mode=True,output_destinations={'result':{'parts':['qa','report_test']}},
        allowed_effects={'result':['create','insert']},scenario_outputs={'main':['stage','result']})
    r=resolve(p)['plan']
    assert r['scenario_outputs']['main']==['stage','qa.report_test']
    assert r['allowed_effects']['qa.report_test']==['create','insert']
    assert resolve(r)['plan']==r


@pytest.mark.parametrize('overrides',[
    {'unknown':{'parts':['qa','x']}},
    {'result':{'parts':['stage']}},
    {'result':{'parts':['lab','events']}},
    {'result':{'parts':['qa','x'*60]}},
])
def test_collisions_and_identifier_overflow_block_before_generation(tmp_path,overrides):
    p=base(tmp_path);p.update(test_mode=True,output_destinations=overrides)
    p['bindings'][0]['target']={'parts':['lab','events_test']}
    with pytest.raises(ValueError):parse_json(Plan,json.dumps(p))


def test_inplace_mutation_has_a_test_seed_and_rebound_unit_input(tmp_path):
    p=base(tmp_path);p['outputs']=[{'name':{'parts':['mart','existing']},'storage':'persistent','columns':['id','value'],'role':'final'}]
    p['bindings'][0]['target']={'parts':['mart','existing']}
    p['output_sources']={'mart.existing':'mart.existing'}
    p.update(test_mode=True,allowed_effects={'mart.existing':['update']})
    resolved=resolve(p);plan=resolved['plan'];seeds=seed_artifacts(resolved['seeds'],'postgresql')
    assert len(seeds)==1 and '"mart"."existing_test"' in seeds[0]['sql']
    assert unit_plan(plan,resolved['seeds'])['bindings'][0]['target']['parts']==['mart','existing_test']
    artifacts=seeds+[{'name':'update.sql','sql':'UPDATE mart.existing_test SET value=value+1;'}]
    assert link(plan,artifacts,'postgresql')['static_passed']
    unsafe=link(plan,seeds+[{'name':'bad.sql','sql':'UPDATE mart.existing SET value=value+1;'}],'postgresql')
    assert any(f['code']=='undeclared_effect' for f in unsafe['findings'])
    # A replacing DATA/CTAS reads the original and creates the test destination
    # itself. Pre-seeding it would introduce a duplicate CREATE.
    p['allowed_effects']={'mart.existing':['create']}
    resolved=resolve(p)
    assert resolved['seeds']==[]
    assert unit_plan(resolved['plan'],[])['bindings'][0]['target']['parts']==['mart','existing']
    assert link(resolved['plan'],[{'name':'replace.sql','sql':'CREATE TABLE mart.existing_test AS SELECT id,value+1 AS value FROM mart.existing;'}],'postgresql')['static_passed']


def test_gate_invalidation_and_export_work_registry(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);old=p['task']['hash']
    plan=copy.deepcopy(p['plan']);plan['test_mode']=True
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    assert not p['gate1'] and not p['gate2']
    assert p['destination_preview']==resolve(p['plan'])['registry']
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    assert p['task']['hash']!=old
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport([]))
    result=run(wb,p,runtime);assert result['release_allowed']
    p=wb.record_pipeline(p['id'],p['revision'],result)
    p=wb.command(p['id'],p['revision'],'approve_release',{})
    with zipfile.ZipFile(io.BytesIO(wb.export_draft(p))) as z:
        assert len(json.loads(z.read('temporary/registry.json')))==2
        assert json.loads(z.read('manifest.json'))['testifier']['enabled']


def test_physical_output_is_in_both_generator_and_reviewer_contract(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);plan=copy.deepcopy(p['plan'])
    plan.update(test_mode=True,output_destinations={'result':{'parts':['qa','report']}})
    p=wb.command(p['id'],p['revision'],'save_plan',plan);p=wb.command(p['id'],p['revision'],'approve_plan',{})
    calls=[];mock=transport(calls)
    def answer(wb,profile,body,cancelled):
        content,provenance=mock(wb,profile,body,cancelled)
        if calls[-1]['role']=='generate':
            data=calls[-1]['data'];response=json.loads(content)
            if data['unit']['source_id']=='P2':
                response['artifacts'][0]['sql']='CREATE TABLE qa.report_test'+(' ENGINE=MergeTree ORDER BY tuple()' if data['target']=='clickhouse' else '')+' AS SELECT id,value FROM stage;'
                content=json.dumps(response)
        return content,provenance
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',answer)
    result=run(wb,p,runtime)
    assert result['release_allowed'],result['requirements']
    for target,profile in result['profiles'].items():
        assert 'qa.report_test' in profile['scenarios']['main']['artifacts'][-1]['sql']
    assert all(any(o['name']['parts']==['qa','report_test'] for o in c['data']['context']['contract']['outputs']) for c in calls if c['role']=='review' and c['data']['context'].get('unit',{}).get('source_id')=='P2')


def test_known_counterexample_revokes_gate_and_cannot_be_overwritten_by_review(tmp_path,monkeypatch):
    from migration_v2.contracts import digest
    from migration_v2.workflow import Conflict
    wb,p,runtime=prepared(tmp_path)
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport([]))
    result=run(wb,p,runtime)
    p=wb.record_pipeline(p['id'],p['revision'],result)
    p=wb.command(p['id'],p['revision'],'approve_release',{})
    observation={'task_hash':p['task']['hash'],'candidate_hash':digest(p['candidate']),
        'dataset_hash':digest('synthetic rows'),'oracle_hash':digest('independent calculation'),
        'target':'postgresql','matched':False,'summary':'Wrong sum on synthetic missing values'}
    with pytest.raises(Conflict):wb.record_test_evidence(p['id'],p['revision']-1,observation)
    p=wb.record_test_evidence(p['id'],p['revision'],observation)
    assert not p['gate2'] and not p['evidence']['release_allowed'] and p['state']=='VALIDATION_FAILED'
    with pytest.raises(ValueError):wb.command(p['id'],p['revision'],'approve_release',{})
    p=wb.record_pipeline(p['id'],p['revision'],result)
    assert not p['evidence']['release_allowed']
    assert {key:p['quality_evidence'][0][key] for key in observation}==observation
    assert p['events'][-2]['actor']=='verification_worker'
    with pytest.raises(ValueError):wb.command(p['id'],p['revision'],'approve_release',{})
    renamed=copy.deepcopy(result)
    renamed['candidate']['artifacts'][0]['name']='renamed.sql'
    p=wb.record_pipeline(p['id'],p['revision'],renamed)
    assert not p['evidence']['release_allowed']
    p=wb.command(p['id'],p['revision'],'retire_test_evidence',{
        'id':digest(p['quality_evidence'][0]),'reason':'Synthetic oracle has been corrected'})
    assert p['quality_evidence'] and p['retired_test_evidence'] and not p['gate2']
    p=wb.record_pipeline(p['id'],p['revision'],result)
    assert wb.command(p['id'],p['revision'],'approve_release',{})['gate2']


def test_worker_counterexample_queues_one_unit_and_preserves_predecessor(tmp_path,monkeypatch):
    from migration_v2.contracts import digest
    from migration_v2.workflow import Conflict
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    result=run(wb,p,runtime);p=wb.record_pipeline(p['id'],p['revision'],result)
    key=next(k for k,u in result['units'].items() if k.startswith('greenplum:') and 'result AS' in u['candidate']['artifacts'][0]['sql'])
    hash_=digest(result['units'][key]['candidate']);observation={'dataset_hash':digest('synthetic'),'message':'Synthetic target rejected this exact unit'}
    with pytest.raises(Conflict):wb.record_unit_test_failure(p['id'],p['revision'],key,'0'*64,observation)
    p=wb.record_unit_test_failure(p['id'],p['revision'],key,hash_,observation)
    assert p['gate1'] and not p['gate2'] and not p['pipeline']['units'][key]['passed']
    assert p['pipeline']['units'][key]['attempts'][-1]['findings'][-1]['origin']=='local_test_worker'
    assert all(u['passed'] for k,u in p['pipeline']['units'].items() if k!=key)
    p=wb.record_pipeline(p['id'],p['revision'],result)
    assert not p['evidence']['release_allowed'] and p['unit_quality_failures'][0]['candidate_hash']==hash_
    with pytest.raises(ValueError):wb.command(p['id'],p['revision'],'approve_release',{})
