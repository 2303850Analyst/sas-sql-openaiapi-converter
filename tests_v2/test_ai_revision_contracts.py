"""Synthetic protocol/negative tests; model mocks are not translation evidence."""
import copy
import io
import json
import zipfile

import pytest

from migration_v2.ai_pipeline import run
from migration_v2.contracts import digest
from migration_v2.dialect_adapter import adapt
from migration_v2.linker import link, semantic_checks
from migration_v2.plan_advisor import propose
from migration_v2.task_planner import plan_tasks
from migration_v2.workflow import Workbench, Conflict
from tests_v2.test_ai_pipeline import prepared, transport


def import_codes(wb, codes):
    buffer=io.BytesIO()
    xml='<ProjectCollection><Elements>'+''.join('<Element Type="SAS.EG.ProjectElements.CodeTask"><Element><ID>'+i+'</ID><Label>'+i+'</Label></Element></Element>' for i in codes)+'</Elements></ProjectCollection>'
    with zipfile.ZipFile(buffer,'w') as z:
        z.writestr('project.xml',xml)
        for i,code in codes.items():z.writestr(i+'/code.sas',code)
    p=wb.import_egp('Synthetic_Revision.egp',buffer.getvalue())
    return wb.command(p['id'],p['revision'],'set_project_scope',{'orders':[{'id':'main','label':'Synthetic','source_ids':list(codes)}]})


def test_large_program_keeps_all_units_macro_instances_and_computed_values(tmp_path):
    wb=Workbench(tmp_path)
    source='%macro unit(n); data work.t&n.; set raw.events; run; %mend;\n'
    source+='\n'.join('%unit('+str(i)+');' for i in range(60))
    source+='\nproc sql; select count(*) into :count from work.t59; quit;\n%if &count>0 %then %do; data work.result; set work.t59; run; %end;'
    p=import_codes(wb,{'P1':source});p['plan']={'parameters':{}}
    planned=plan_tasks(wb,p)
    assert len(planned['tasks'])==62
    assert len({u['id'] for u in planned['tasks']})==62
    assert all(u['definitions'] for u in planned['tasks'][:60])
    assert not planned['requirements'],planned['requirements']
    assert planned['classifications'][-1]['end']==len(source)
    assert 'into :count' in planned['tasks'][-2]['source']
    assert '&count>0' in planned['tasks'][-1]['source']


def test_include_is_bound_to_reference_and_missing_nested_macro_is_actionable(tmp_path):
    wb=Workbench(tmp_path)
    p=import_codes(wb,{'P1':'%include "library.sas"; %outer;'});p['plan']={'parameters':{}}
    assert any(r['code']=='include_source_required' for r in plan_tasks(wb,p)['requirements'])
    p=wb.command(p['id'],p['revision'],'attach_include',{'source_id':'P1','offset':0,'filename':'library.sas','text':'%macro outer; %missing_impl; %mend;'})
    p['plan']={'parameters':{}}
    planned=plan_tasks(wb,p)
    assert len(planned['tasks'])==1 and planned['tasks'][0]['definitions']
    assert any(r['code']=='macro_definition_required' and r['name']=='missing_impl' for r in planned['requirements'])
    assert any(c['role']=='included_macro_definition' for c in planned['classifications'])


def test_budget_resume_reuses_unreviewed_candidate_and_preserves_ledger(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);plan=copy.deepcopy(p['plan']);plan['max_model_calls']=1
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    calls=[];monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    first=run(wb,p,runtime)
    assert not first['release_allowed'] and len(calls)==1
    assert next(iter(first['units'].values()))['attempts'][0]['candidate']
    p=wb.record_pipeline(p['id'],p['revision'],first)
    p=wb.command(p['id'],p['revision'],'grant_model_calls',{'additional_calls':30,'rationale':'Synthetic explicit resume after budget stop'})
    second=run(wb,p,runtime)
    assert second['release_allowed'],second['requirements']
    assert sum(c['role']=='generate' for c in calls)==2 # No repeated first generation.
    with wb.connect() as db:
        assert db.execute('SELECT count(*) FROM pipeline_calls').fetchone()[0]==len(calls)


def test_human_sql_fix_keeps_gate1_invalidates_consumers_and_gate2(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    result=run(wb,p,runtime);p=wb.record_pipeline(p['id'],p['revision'],result)
    p=wb.command(p['id'],p['revision'],'approve_release',{})
    old_hashes={k:u['hash'] for k,u in result['units'].items()}
    key=next(k for k,u in result['units'].items() if k.startswith('greenplum:') and 'lab.events' in u['candidate']['artifacts'][0]['sql'])
    p=wb.command(p['id'],p['revision'],'replace_task_sql',{'unit_key':key,'sql':'CREATE TEMP TABLE stage AS SELECT id,value FROM lab.events WHERE value>0 AND id IS NOT NULL;'})
    assert p['gate1'] and not p['gate2']
    assert not p['pipeline']['profiles']['greenplum']['passed']
    revised=run(wb,p,runtime)
    assert revised['release_allowed'] # Mock reviewer only: this test concerns invalidation, not logic correctness.
    downstream=[k for k in revised['units'] if k.startswith('greenplum:') and k!=key]
    assert all(revised['units'][k]['hash']!=old_hashes[k] for k in downstream)
    assert any(a.get('kind')=='manual' for a in revised['units'][key]['attempts'])


@pytest.mark.parametrize('sql,code',[
    ('CREATE TEMP TABLE result AS SELECT id FROM lab.events;','output_columns_mismatch'),
    ('CREATE TEMP TABLE stage AS SELECT id,value FROM lab.events;','missing_output'),
    ('CREATE TEMP TABLE result AS SELECT id,value FROM not_created;','missing_producer'),
    ('CREATE TEMP TABLE result AS SELECT t1.id,t1.value FROM lab.events;','alias_unresolved'),
    ('CREATE TEMP TABLE result AS SELECT id,value FROM lab.events; DELETE FROM lab.events;','undeclared_effect'),
])
def test_linker_rejects_lost_fields_outputs_producers_aliases_and_effects(tmp_path,sql,code):
    _,p,_=prepared(tmp_path)
    checked=link(p['plan'],[{'name':'test.sql','sql':sql}],'postgresql',[], 'data result; set raw.events; run;')
    assert not checked['static_passed']
    assert any(f['code']==code for f in checked['findings']),checked['findings']


def test_numeric_missing_sort_and_filter_mutants(tmp_path):
    _,p,_=prepared(tmp_path)
    for source,sql,code in [
        ('proc sort; by descending value; run;','SELECT id,value FROM lab.events ORDER BY value DESC;','numeric_missing_sort_mismatch'),
        ('if value>0;','SELECT id,value FROM lab.events WHERE value>0 OR value IS NULL;','numeric_missing_filter_mismatch')]:
        findings=semantic_checks(p['plan'],source,[{'name':'test.sql','sql':sql}],'postgres')
        assert any(f['code']==code for f in findings)
    assert not semantic_checks(p['plan'],'if value>0;',[{'name':'test.sql','sql':'SELECT id,value FROM lab.events WHERE value>0;'}],'postgres')


def test_alternative_scenarios_are_exported_separately(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path)
    p=wb.command(p['id'],p['revision'],'set_project_scope',{'orders':[{'id':i,'label':i,'source_ids':['P1','P2']} for i in ('A','B')]})
    plan=copy.deepcopy(wb.get(p['id'],3)['plan'])
    plan['scenario_id']=p['scenario']['id'];plan['scenario_outputs']={'A':['stage','result'],'B':['stage','result']}
    p=wb.command(p['id'],p['revision'],'save_plan',plan);p=wb.command(p['id'],p['revision'],'approve_plan',{})
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport([]))
    result=run(wb,p,runtime);assert result['release_allowed'],result['requirements']
    p=wb.record_pipeline(p['id'],p['revision'],result);p=wb.command(p['id'],p['revision'],'approve_release',{})
    with zipfile.ZipFile(io.BytesIO(wb.export_draft(p))) as z:
        manifest=json.loads(z.read('manifest.json'))
        assert len(manifest['entrypoints'])==6 and len({e['path'] for e in manifest['entrypoints']})==6
        assert {e['scenario_id'] for e in manifest['entrypoints']}=={'A','B'}
        assert manifest['status']=='accepted_code_not_executed'


def test_contract_advisor_is_reviewed_and_cannot_approve_gate(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);approved=p['plan']
    p=wb.command(p['id'],p['revision'],'set_external_parameter',{'name':'threshold','type':'integer','value':'3'})
    intake={k:approved[k] for k in ('bindings','parameters','source_semantics','target_versions','intermediate_inputs')}
    intake['parameters']=p['parameters']
    p=wb.command(p['id'],p['revision'],'save_intake',intake)
    def answer(wb,profile,body,cancelled):
        data=json.loads(body['messages'][1]['content'])
        if body['messages'][0]['content'].startswith('Analyze this ORIGINAL'):
            name='stage' if data['unit']['source_id']=='P1' else 'result'
            output=next(o for o in approved['outputs'] if o['name']['parts']==[name])
            result={'scope_hash':data['hash'],'outputs':[{'output':output,'source_name':'work.'+name,'effects':['create'],'evidence':[data['unit']['obligations'][0]]}],'requirements':[],'advisory_notes':['Physical output types require human confirmation.']}
        else:
            result={'scope_hash':data['scope_hash'],'candidate_hash':data['candidate_hash'],'verdict':'pass','reviewed_obligations':data['obligations'],'findings':[]}
        return json.dumps(result),{'generator':'synthetic_mock_only'}
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',answer)
    advice=propose(wb,p,runtime);assert advice['applicable']
    assert len(advice['advisory_notes'])==2 and advice['requirements']==[]
    p=wb.record_plan_proposal(p['id'],p['revision'],advice)
    p=wb.command(p['id'],p['revision'],'apply_plan_proposal',{})
    assert p['plan']['output_sources']=={'stage':'work.stage','result':'work.result'}
    assert p['plan']['parameter_types']=={'threshold':'integer'}
    assert p['state']=='PLAN_READY' and not p['gate1']
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    assert p['gate1']

    # Continue from the actual reviewed proposal through the same final export path.
    from tests_v2.test_ai_pipeline import transport
    import hashlib
    calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    result=run(wb,p,runtime)
    assert result['release_allowed'],result['requirements']
    p=wb.record_pipeline(p['id'],p['revision'],result)
    p=wb.command(p['id'],p['revision'],'approve_release',{})
    with zipfile.ZipFile(io.BytesIO(wb.export_draft(p))) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert len(manifest['entrypoints'])==3
        for entry in manifest['entrypoints']:
            exported=archive.read(entry['path'])
            assert hashlib.sha256(exported).hexdigest()==entry['sha256']
            review=next(c for c in calls if c['role']=='review'
                and c['data']['context'].get('mode')=='assembly_review'
                and c['data']['context']['target']==entry['target'])
            assert review['data']['candidate']['artifacts'][0]['sql'].encode()==exported


def test_dialect_adapter_is_bounded_and_does_not_accept_functions():
    candidate={'artifacts':[{'name':'001.sql','sql':'CREATE TEMP TABLE stage AS SELECT id,value FROM lab.events WHERE value>0;'}]}
    result=adapt(candidate,'postgresql','clickhouse','a'*64,['original'])
    assert 'TEMPORARY' in result['artifacts'][0]['sql'] and 'Memory' in result['artifacts'][0]['sql']
    candidate['artifacts'][0]['sql']='CREATE TEMP TABLE stage AS SELECT id,random() AS value FROM lab.events;'
    assert adapt(candidate,'postgresql','clickhouse','a'*64,['original']) is None


def test_contract_cannot_omit_known_sas_output(tmp_path):
    wb,p,_=prepared(tmp_path);plan=copy.deepcopy(p['plan'])
    plan['outputs']=[o for o in plan['outputs'] if o['name']['parts']==['result']]
    plan['output_sources']={'result':'work.result'}
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    assert any(r['code']=='output_contract_required' and r['name']=='stage' for r in p['task_plan']['requirements'])
    with pytest.raises(ValueError,match='unresolved requirements'):wb.command(p['id'],p['revision'],'approve_plan',{})


def test_future_assignment_and_unused_macro_do_not_hide_required_external_parameter(tmp_path):
    wb=Workbench(tmp_path)
    p=import_codes(wb,{'P1':'%macro unused; %put &never_called; %mend; data a; set raw.events; if value>&cutoff.; run;',
        'P2':'%let cutoff=0; data b; set a; run;'});p['plan']={'parameters':{}}
    needed=[r for r in plan_tasks(wb,p)['requirements'] if r['code']=='external_parameter_required']
    assert [r['name'] for r in needed]==['cutoff'] and needed[0]['source_id']=='P1'


def test_cancelled_pipeline_preserves_completed_units_for_resume(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    result=run(wb,p,runtime,lambda:len(calls)>=2)
    assert not result['release_allowed'] and any(u['passed'] for u in result['units'].values())
    p=wb.record_pipeline(p['id'],p['revision'],result)
    resumed=run(wb,p,runtime)
    assert resumed['release_allowed']
    assert sum(c['role']=='generate' for c in calls)==2


def test_invalid_review_verdict_gets_bounded_protocol_correction(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[];base=transport(calls);broken=[]
    def answer(wb,profile,body,cancelled):
        content,provenance=base(wb,profile,body,cancelled)
        if calls[-1]['role']=='review' and not broken:
            response=json.loads(content);response['verdict']='fail';broken.append(True)
            return json.dumps(response),provenance
        return content,provenance
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',answer)
    result=run(wb,p,runtime)
    assert result['release_allowed']
    assert any('protocol_correction' in c['data'] for c in calls)
    assert len(calls)==12 # One extra review, no verdict edited by the application.


def test_migration_of_existing_store_preserves_history_and_backup(tmp_path):
    wb,p,_=prepared(tmp_path)
    before=wb.get(p['id'],1)
    with wb.connect() as db:db.execute('PRAGMA user_version=1')
    resumed=Workbench(tmp_path)
    assert resumed.get(p['id'],1)==before
    assert (tmp_path/'backups/pre-ai-code-review.sqlite3').is_file()
