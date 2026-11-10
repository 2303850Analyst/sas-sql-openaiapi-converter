"""Synthetic full EGP protocol tests. Mock outputs do not validate a real model."""
import io
import json
import zipfile
import pytest
from migration_v2.workflow import Workbench
from migration_v2.runtime import Runtime,ModelProfile
from migration_v2.contracts import digest
from migration_v2.ai_pipeline import run
from migration_v2.linker import link,semantic_checks


def egp():
    codes={"P1":"%let cutoff=0; %macro copy; data work.stage; set raw.events; if value>&cutoff.; run; %mend; %copy;",
           "P2":"proc sql; create table work.result as select t1.id,t1.value from work.stage t1; quit;"}
    xml='<ProjectCollection><Elements>'+''.join('<Element Type="SAS.EG.ProjectElements.CodeTask"><Element><ID>'+i+'</ID><Label>'+i+'</Label></Element></Element>' for i in codes)+'</Elements></ProjectCollection>'
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('project.xml',xml)
        for i,text in codes.items():z.writestr(i+'/code.sas',text)
    return b.getvalue()


def prepared(tmp_path):
    wb=Workbench(tmp_path)
    p=wb.import_egp('Full_Synthetic.egp',egp())
    p=wb.command(p['id'],p['revision'],'set_project_scope',{'orders':[{'id':'main','label':'Full synthetic scenario','source_ids':['P1','P2']}]})
    plan={'scenario_id':p['scenario']['id'],'acceptance':'code_review','targets':['greenplum','postgresql','clickhouse'],
        'target':'greenplum','target_version':'7.0','target_versions':{'greenplum':'7.0','postgresql':'17.10','clickhouse':'25.8'},
        'bindings':[{'source_id':i['id'],'decision':'replace','target':{'parts':['lab','events']},
            'metadata_status':'user_supplied','schema_complete':True,
            'columns':[{'name':'id','data_type':'INTEGER'},{'name':'value','data_type':'INTEGER'}]} for i in p['context']['inputs']],
        'outputs':[{'name':{'parts':[name]},'storage':'temporary','columns':['id','value'],'role':'final' if name=='result' else 'intermediate'} for name in ('stage','result')],
        'output_sources':{'stage':'work.stage','result':'work.result'},
        'validation_scope':'Full synthetic source, all fields and output effects; code review only.',
        'source_semantics':{'numeric_missing':'ordinary_as_null','character_missing':'null_empty_ascii_spaces',
            'character_storage':'unpadded_varchar','input_observation_order':'explicit_sort_only'},
        'resolutions':[{'issue_id':i['id'],'rationale':'Reviewed the complete synthetic source and declared output contracts'} for i in p['context']['issues'] if not i['hard']]}
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    p=wb.command(p['id'],p['revision'],'approve_plan',{})
    model=ModelProfile(endpoint='https://synthetic.invalid/v1/chat/completions',model='mock',key_file='unused',key_name='unused',
        input_usd_per_million=1,output_usd_per_million=1,budget_usd=1,synthetic_archives=(p['snapshot']['archive_sha256'],))
    return wb,p,Runtime(model=model)


def transport(calls,*,bad_alias_once=False,review_fail_once=False):
    def answer(wb,profile,body,cancelled):
        data=json.loads(body['messages'][1]['content']);generator=body['messages'][0]['content'].startswith('You translate')
        if body['messages'][0]['content'].startswith('Analyze this ORIGINAL'):
            name='stage' if data['unit']['source_id']=='P1' else 'result'
            result={'scope_hash':data['hash'],'outputs':[{'output':{'name':{'parts':[name]},'storage':'temporary','columns':['id','value'],'role':'intermediate' if name=='stage' else 'final'},
                'source_name':'work.'+name,'effects':['create'],'evidence':[data['unit']['obligations'][0]]}],'requirements':[]}
            calls.append({'role':'plan','data':data})
            return json.dumps(result),{'generator':'synthetic_mock_only','request_hash':digest(body)}
        calls.append({'role':'generate' if generator else 'review','data':data})
        if generator:
            unit=data['unit'];target=data['target']
            if unit['source_id']=='P1':
                query='SELECT t1.id,t1.value FROM lab.events t1 WHERE t1.value>0'
                if bad_alias_once and sum(c['role']=='generate' for c in calls)==1:query='SELECT t1.id,t1.value FROM lab.events WHERE value>0'
                name='stage'
            else:query='SELECT t1.id,t1.value FROM stage t1';name='result'
            sql='CREATE TEMPORARY TABLE '+name+(' ENGINE=Memory' if target=='clickhouse' else '')+' AS '+query+';'
            result={'schema_version':1,'task_hash':data['hash'],'artifacts':[{'name':'001.sql','sql':sql}],
                'source_claims':unit['obligations'],'assumptions':[]}
        else:
            reject=review_fail_once and sum(c['role']=='review' for c in calls)==1
            result={'scope_hash':data['scope_hash'],'candidate_hash':data['candidate_hash'],
                'verdict':'fail' if reject else 'pass','reviewed_obligations':data['obligations'],
                'findings':[{'code':'synthetic_mutant','message':'Synthetic reviewer requests a correction','severity':'blocking',
                    'obligation':data['obligations'][0],'sas_location':'1','sql_location':'001.sql:1','counterexample':'synthetic only','required_information':''}] if reject else []}
        return json.dumps(result),{'generator':'synthetic_mock_only','request_hash':digest(body)}
    return answer


def test_full_egp_three_dialects_and_code_gate_without_reference(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    result=run(wb,p,runtime)
    assert result['release_allowed'],result['requirements']
    assert len(result['units'])==6 and len(calls)==11
    assert len([c for c in calls if c['role']=='generate'])==2
    assert len([c for c in calls if c['role']=='review'])==9
    assert all(c['data']['unit']['source']!='' for c in calls if c['role']=='generate')
    assert all(c['data']['context']['unit']['definitions'] for c in calls if c['role']=='review' and 'unit' in c['data']['context'] and c['data']['context']['unit']['source_id']=='P1')
    p=wb.record_pipeline(p['id'],p['revision'],result)
    p=wb.command(p['id'],p['revision'],'approve_release',{})
    assert p['state']=='CODE_ACCEPTED' and p['gate2']['basis']=='code_review_not_data_equivalence'
    assert p['evidence']['checks']['equivalence']=='not_run'


def test_failed_model_response_does_not_discard_other_target_profiles(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[];mock=transport(calls)
    def answer(wb,profile,body,cancelled):
        data=json.loads(body['messages'][1]['content'])
        if 'candidate' in data and data['context']['target']=='greenplum':raise ValueError('Synthetic invalid response')
        return mock(wb,profile,body,cancelled)
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',answer)
    result=run(wb,p,runtime)
    assert set(result['profiles'])=={'greenplum','postgresql','clickhouse'}
    assert not result['profiles']['greenplum']['passed']
    assert result['profiles']['postgresql']['passed'] and result['profiles']['clickhouse']['passed']
    assert not result['release_allowed']
    assert any(f['code']=='pipeline_stopped' and f['target']=='greenplum' for f in result['requirements'])


def test_corrected_static_checker_rechecks_existing_sql_before_review(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path)
    calls=[];monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    from migration_v2.linker import finding
    def broken(*args,**kwargs):
        result=link(*args,**kwargs)
        if args[2]=='greenplum' and 'CREATE TEMPORARY TABLE stage' in str(args[1]):result.update(static_passed=False,findings=[finding('synthetic_checker_gap','Synthetic known checker gap')])
        return result
    monkeypatch.setattr('migration_v2.ai_pipeline.link',broken)
    first=run(wb,p,runtime);assert not first['release_allowed']
    generations=sum(c['role']=='generate' for c in calls)
    p=wb.record_pipeline(p['id'],p['revision'],first)
    monkeypatch.setattr('migration_v2.ai_pipeline.link',link)
    result=run(wb,p,runtime);assert result['release_allowed'],result['requirements']
    assert sum(c['role']=='generate' for c in calls)==generations+1 # Only the not-yet-generated second unit.


def test_identical_assembly_reuses_exact_review_without_new_paid_calls(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    first=run(wb,p,runtime);p=wb.record_pipeline(p['id'],p['revision'],first)
    count=len(calls);second=run(wb,p,runtime)
    assert second['release_allowed'] and len(calls)==count
    assert all(s['assembly_review']['reuse_from_revision']==p['revision'] for profile in second['profiles'].values() for s in profile['scenarios'].values())


def test_changed_review_policy_rechecks_units_and_invalidates_dependents(tmp_path,monkeypatch):
    import migration_v2.ai_pipeline as pipeline
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    first=run(wb,p,runtime);p=wb.record_pipeline(p['id'],p['revision'],first)
    generators=sum(c['role']=='generate' for c in calls);count=len(calls)
    monkeypatch.setattr(pipeline,'REVIEWER',pipeline.REVIEWER+'\nSynthetic policy revision.')
    second=run(wb,p,runtime)
    assert second['release_allowed'] and len(calls)>count
    assert sum(c['role']=='generate' and c['data']['unit']['source_id']=='P1' for c in calls)==1
    assert sum(c['role']=='generate' for c in calls)==generators
    assert sum(c['role']=='review' and c['data']['context'].get('unit',{}).get('source_id')=='P2' for c in calls)==6


def test_newer_negative_assembly_review_prevents_reusing_older_pass(tmp_path,monkeypatch):
    import copy
    from migration_v2.ai_pipeline import REVIEWER
    wb,p,runtime=prepared(tmp_path);monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport([]))
    result=run(wb,p,runtime);p=wb.record_pipeline(p['id'],p['revision'],result)
    negative=copy.deepcopy(result);checked=negative['profiles']['greenplum']['scenarios']['main']['assembly_review']
    checked['review']['verdict']='fail';checked['review']['findings']=[{'severity':'blocking','message':'Synthetic new defect'}]
    p=wb.record_pipeline(p['id'],p['revision'],negative)
    assert wb.cached_assembly_review(p['id'],p['revision'],checked['review']['scope_hash'],checked['review']['candidate_hash'],digest(REVIEWER)) is None


def test_missing_alias_is_repaired_before_review(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls,bad_alias_once=True))
    result=run(wb,p,runtime)
    assert result['release_allowed'],result['requirements']
    repaired=next(v for v in result['units'].values() if len(v['attempts'])==2)
    assert any(f['code']=='alias_unresolved' for f in repaired['attempts'][0]['findings'])
    assert repaired['attempts'][1]['candidate']!=repaired['attempts'][0]['candidate']


def test_reviewer_disagreement_cannot_be_overridden(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls,review_fail_once=True))
    result=run(wb,p,runtime)
    assert not result['release_allowed'] # Identical regenerated candidate is not progress.
    assert any(not u['passed'] for u in result['units'].values())
    p=wb.record_pipeline(p['id'],p['revision'],result)
    with pytest.raises(ValueError,match='current linked'):
        wb.command(p['id'],p['revision'],'approve_release',{})
    key, failed = next((k,u) for k,u in result['units'].items() if not u['passed'])
    original = failed['attempts'][0]['candidate']
    with pytest.raises(ValueError,match='прошедший статическую'):
        wb.command(p['id'],p['revision'],'request_unit_rereview',{
            'unit_key':key,'reason':'Recheck this synthetic semantic counterexample','candidate_hash':'0'*64})
    p=wb.command(p['id'],p['revision'],'request_unit_rereview',{
        'unit_key':key,'reason':'Recheck this synthetic semantic counterexample','candidate_hash':digest(original)})
    calls.clear()
    checked=run(wb,p,runtime)
    assert checked['units'][key]['attempts'][-1]['kind']=='resume_review'
    assert checked['units'][key]['attempts'][-1]['candidate']==original
    assert not checked['release_allowed']  # A second negative review still blocks release.
    assert not any(c['role']=='generate' and c['data']['hash']==failed['hash'] for c in calls)
    review_call=next(c['data'] for c in calls if c['role']=='review' and c['data']['candidate']==original)
    reconsideration=review_call['reconsideration']
    assert reconsideration['reason']=='Recheck this synthetic semantic counterexample'
    assert reconsideration['prior_review']['verdict']=='fail'
    assert reconsideration['prior_review']['candidate_hash']==review_call['candidate_hash']


def test_verifier_upgrade_retains_sql_but_rechecks_before_acceptance(tmp_path,monkeypatch):
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    old=run(wb,p,runtime)
    old['version']='historical-verifier'
    p=wb.record_pipeline(p['id'],p['revision'],old)
    calls.clear()
    current=run(wb,p,runtime)
    assert current['release_allowed']
    assert not any(c['role']=='generate' for c in calls)
    assert any(c['role']=='review' and 'unit' in c['data']['context'] for c in calls)
    assert all(u['candidate']==old['units'][k]['candidate'] for k,u in current['units'].items())


def test_rule_content_change_with_same_version_invalidates_approval_and_preserves_sql(tmp_path,monkeypatch):
    import migration_v2.ai_pipeline as pipeline
    wb,p,runtime=prepared(tmp_path);calls=[]
    monkeypatch.setattr('migration_v2.ai_pipeline.request_json',transport(calls))
    monkeypatch.setattr(pipeline,'rules_fingerprint',lambda:'1'*64,raising=False)
    old=run(wb,p,runtime)
    p=wb.record_pipeline(p['id'],p['revision'],old)
    p=wb.command(p['id'],p['revision'],'approve_release',{})
    assert wb.code_review_current(p)
    policy='3'*64
    p=wb.record_test_evidence(p['id'],p['revision'],{'task_hash':p['task']['hash'],'candidate_hash':digest(p['candidate']),
        'target':'postgresql','dataset_hash':'4'*64,'oracle_hash':'5'*64,'policy_hash':policy,'matched':True,'summary':'Synthetic raw evidence'})
    assert wb.current_test_evidence(p,'postgresql',policy)
    assert not wb.current_test_evidence(p,'postgresql','6'*64)
    assert wb.code_review_current(p)
    monkeypatch.setattr(pipeline,'rules_fingerprint',lambda:'2'*64,raising=False)
    assert not wb.code_review_current(p)
    with zipfile.ZipFile(io.BytesIO(wb.export_draft(p))) as z:
        assert not json.loads(z.read('manifest.json'))['release_allowed']
    calls.clear()
    revised=run(wb,p,runtime)
    assert revised['release_allowed'] and revised['version']==old['version']
    assert revised['rules_fingerprint']!=old['rules_fingerprint']
    assert all(u['candidate']==old['units'][k]['candidate'] for k,u in revised['units'].items())
    assert not any(c['role']=='generate' for c in calls)


def test_rule_file_location_does_not_change_content_identity(tmp_path):
    from migration_v2.rule_identity import source_identity
    a=tmp_path/'old.py'; b=tmp_path/'new.py'
    a.write_bytes(b'a=1\r\n');b.write_bytes(b'a=1\n')
    assert source_identity([a])==source_identity([b])
    b.write_bytes(b'a=2\n')
    assert source_identity([a])!=source_identity([b])


@pytest.mark.parametrize("complete",[True,False])
def test_assembly_accepts_only_complete_statement_level_coverage(tmp_path,monkeypatch,complete):
    wb,p,runtime=prepared(tmp_path);calls=[];mock=transport(calls)
    def answer(wb,profile,body,cancelled):
        response,provenance=mock(wb,profile,body,cancelled)
        data=json.loads(body["messages"][1]["content"])
        ctx=data.get("context",{})
        if ctx.get("mode")=="assembly_review":
            result=json.loads(response)
            statements=[obligation for entry in ctx["unit_reviews"] for obligation in entry["obligations"]]
            result["reviewed_obligations"]=statements if complete else statements[:-1]
            response=json.dumps(result)
        return response,provenance
    monkeypatch.setattr("migration_v2.ai_pipeline.request_json",answer)
    pipeline=run(wb,p,runtime)
    assert pipeline["release_allowed"] is complete
    if complete:
        assembly=pipeline["profiles"]["greenplum"]["scenarios"]["main"]["assembly_review"]
        assert assembly["coverage_normalization"]["granularity"]=="complete_statements_to_units"
    else:
        assert any(f["code"]=="review_protocol_error" for f in pipeline["requirements"])


def test_missing_string_and_numeric_are_distinct(tmp_path):
    wb,p,_=prepared(tmp_path);plan=p['plan']
    plan['bindings'][0]['columns'][1]={'name':'value','data_type':'VARCHAR(20)'}
    sql=[{'name':'001.sql','sql':'CREATE TEMP TABLE result AS SELECT id,value FROM lab.events WHERE value IS NULL;'}]
    assert any(f['code']=='character_missing_mismatch' for f in semantic_checks(plan,'if missing(value);',sql,'postgres'))
    plan['bindings'][0]['columns'][1]['data_type']='INTEGER'
    assert not semantic_checks(plan,'if missing(value);',sql,'postgres')
