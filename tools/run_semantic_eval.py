"""Frozen synthetic corpus -> real model transport -> real verifier/PG executor.

One explicit call per case and phase, persistent reservations, no paid retries.
Every SQL candidate runs unchanged on four datasets which the model never sees.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import time

import psycopg
from psycopg import sql

from fixtures.semantic_eval.corpus import ROOT
from migration_v2 import generation
from migration_v2.contracts import digest
from migration_v2.execution import execute
from migration_v2.runtime import Runtime, DatabaseProfile
from migration_v2.workflow import Workbench


def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.pending')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


def schemas(c):return 'eval_'+c['id']+'_src','eval_'+c['id']+'_ref'


def prepare(wb,c,phase,root=ROOT):
    p=wb.import_egp(c['id']+'_'+phase+'.egp',(root/'frozen'/(c['id']+'.egp')).read_bytes())
    assert p['snapshot']['archive_sha256']==c['archive_sha256']
    p=wb.command(p['id'],p['revision'],'select_scenario',{'source_ids':['CodeTask-main']})
    declared={'source.'+name for name in c['inputs']}
    for name in sorted(declared-{i['original'] for i in p['context']['inputs']}):
        p=wb.command(p['id'],p['revision'],'declare_input',{'origin_source_id':'CodeTask-main',
                     'name':{'parts':name.split('.')},'rationale':'Reviewed full synthetic program and its explicit input contract'})
    assert {i['original'] for i in p['context']['inputs']}==declared,(c['id'],p['context']['inputs'])
    src,ref=schemas(c)
    plan={'scenario_id':p['scenario']['id'],'target':'postgresql','target_version':'17.10',
          'bindings':[{'source_id':i['id'],'decision':'replace','target':{'parts':[src,i['original'].split('.')[1]]},
                       'metadata_status':'user_supplied',**c['inputs'][i['original'].split('.')[1]]} for i in p['context']['inputs']],
          'outputs':[{'name':{'parts':[name]},'storage':'temporary','columns':[col['name'] for col in columns],
                      'role':'intermediate' if name in ('l','r','hi','lo','ordered') else 'final'} for name,columns in c['outputs'].items()],
          'comparisons':[{'output':{'parts':[name]},'reference':{'parts':[ref,name]},'provenance':'synthetic_handwritten',
                          'reference_run_id':'frozen_python_spec_v1'} for name in c['outputs']],
          'validation_scope':'Frozen independent Python reference; all rows, multiplicities, types and every declared output; not native SAS.',
          'source_semantics':{'numeric_missing':'special_tags_lost' if c['id']=='n01' else 'ordinary_as_null',
                              'character_missing':'null_empty_ascii_spaces','character_storage':'unpadded_varchar',
                              'input_observation_order':'explicit_sort_only'},
          'resolutions':[{'issue_id':i['id'],'rationale':'Reviewed complete synthetic source and explicit external inputs and outputs'} for i in p['context']['issues'] if not i['hard']]}
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    return wb.command(p['id'],p['revision'],'approve_plan',{})


def seed(c,variant):
    # This fixture owner is only ever used against the dedicated synthetic stand.
    with psycopg.connect(host='127.0.0.1',port=55439,dbname='semantic_eval',user='fixture_owner',password='synthetic_owner_only') as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0]=='semantic_eval'
        src,ref=schemas(c)
        for schema,definitions,rows in [(src,{n:t['columns'] for n,t in c['inputs'].items()},variant['inputs']),
                                        (ref,c['outputs'],variant['expected'] or {n:[] for n in c['outputs']})]:
            assert schema.startswith('eval_')
            conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
            for name,columns in definitions.items():
                definition=sql.SQL(',').join(sql.SQL('{} {}').format(sql.Identifier(col['name']),sql.SQL(col['data_type'])) for col in columns)
                conn.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} ({})').format(sql.Identifier(schema,name),definition))
                conn.execute(sql.SQL('TRUNCATE TABLE {}').format(sql.Identifier(schema,name)))
                if rows[name]:
                    with conn.cursor() as cur:
                        cur.executemany(sql.SQL('INSERT INTO {} VALUES ({})').format(sql.Identifier(schema,name),sql.SQL(',').join(sql.Placeholder() for _ in columns)),rows[name])
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO migration_validator').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO migration_validator').format(sql.Identifier(schema)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--model-config',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--phase',required=True)
    parser.add_argument('--corpus-root',type=Path,default=ROOT)
    parser.add_argument('--prompt',type=Path)
    parser.add_argument('--cases',default='')
    parser.add_argument('--thinking', choices=('enabled','disabled'))
    parser.add_argument('--max-output',type=int,default=6000)
    parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    root=args.corpus_root
    frozen=(root/'frozen/corpus.json').read_bytes()
    manifest=json.loads((root/'frozen/manifest.json').read_text())
    assert hashlib.sha256(frozen).hexdigest()==manifest['corpus_sha256']
    assert hashlib.sha256((root/'oracles.py').read_bytes()).hexdigest()==manifest['oracle_sha256']
    cases=json.loads(frozen)
    profile=replace(Runtime.load(args.model_config).model,budget_usd=.85,max_output_tokens=args.max_output,
                    synthetic_archives=tuple(c['archive_sha256'] for c in cases))
    if args.thinking:
        profile=replace(profile,extra={**profile.extra,'thinking':{'type':args.thinking}})
    if args.prompt:generation.SYSTEM=args.prompt.read_text(encoding='utf-8')
    wb=Workbench(args.data)
    for c in cases:
        if args.cases and c['id'] not in args.cases.split(','):continue
        path=args.out/args.phase/(c['id']+'.json')
        if path.exists():
            print(json.dumps({'case':c['id'],'state':'already_recorded_no_retry'}),flush=True);continue
        record={'case':c['id'],'title':c['title'],'phase':args.phase,'negative':c['negative'],
                'corpus_sha256':manifest['corpus_sha256'],'prompt_sha256':hashlib.sha256(generation.SYSTEM.encode()).hexdigest(),
                'model':profile.model,'model_parameters':profile.extra,'max_output_tokens':profile.max_output_tokens,
                'state':'started','variants':[]}
        dump(path,record)
        start=time.monotonic()
        try:
            p=prepare(wb,c,args.phase,root);record.update(project_id=p['id'],revision=p['revision'])
            payload=generation.model_payload(wb,p,profile)
            assert schemas(c)[1] not in json.dumps(payload)
            assert 'reference_run_id' not in json.dumps(payload)
            record.update(request_sha256=digest(payload),reference_not_in_prompt=True)
            if args.prepare_only:
                record['state']='prepared';dump(path,record);print(c['id'],'prepared',flush=True);continue
            result=generation.propose(wb,p,profile)
            record['provenance']=result['provenance'];record['candidate']=result['candidate']
            p=wb.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(result['candidate'])})
            record['static']=p['evidence']
            if c['negative']:
                record['state']='safe_refusal' if result['candidate']['assumptions'] else ('unsafe_accepted' if p['evidence']['static_passed'] else 'blocked_by_static')
            elif not p['evidence']['static_passed']:
                record['state']='static_failed'
            else:
                dbprofile=DatabaseProfile(host='127.0.0.1',port=55439,dbname='semantic_eval',user='migration_validator',
                                          sslmode='disable',version_prefix='17.10',reference_schemas=(schemas(c)[1],))
                for variant in c['variants']:
                    seed(c,variant)
                    one={'name':variant['name'],'input_rows':sum(len(r) for r in variant['inputs'].values())}
                    try:
                        evidence=execute(wb,p,dbprofile)
                        one.update(passed=evidence['checks']['comparison']=='passed',evidence=evidence)
                        assert not evidence['release_allowed']
                    except Exception as error:
                        one.update(passed=False,error=str(error) if type(error) is ValueError else type(error).__name__)
                    record['variants'].append(one)
                    dump(path,record)
                record['state']='all_variants_passed' if all(v['passed'] for v in record['variants']) else 'comparison_failed'
        except Exception as error:
            record.update(state='failed',error=str(error) if type(error) in (ValueError,AssertionError) else type(error).__name__)
        record['elapsed_seconds']=round(time.monotonic()-start,3)
        record['budget']=generation.Budget(wb).status()
        if record.get('candidate'):
            path.with_suffix('.sql').write_text('\n\n'.join(a['sql'] for a in record['candidate']['artifacts']),encoding='utf-8')
        dump(path,record)
        print(json.dumps({k:record.get(k) for k in ('case','state','error','elapsed_seconds','budget')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
