"""New AI pipeline, only frozen constructed projects, shared USD5 ledger.

One candidate/profile is executed unchanged on all data variants. Reviewer failure
never becomes acceptance merely because an independent dataset happened to match.
"""
import argparse
from collections import Counter
from decimal import Decimal
from dataclasses import replace
from datetime import date,datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import struct
from tempfile import TemporaryFile
import time

import psycopg
from psycopg import sql
from migration_v2.runtime import Runtime
from migration_v2.workflow import Workbench
from migration_v2.contracts import digest
from migration_v2.ai_pipeline import run,scenario_plan
from migration_v2.generation import Budget
from fixtures.testifier_lab.compare import compare,family
from fixtures.testifier_lab.compare_v2 import compare_v2,POLICY_HASH

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'fixtures/testifier_lab/frozen'
OUT=ROOT/'reports/testifier-bi-20261108'
PRIVATE=ROOT/'private_inputs/testifier-model-lab'
CH='sas-testifier-lab-clickhouse-1'


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);pending=path.with_suffix('.pending')
    pending.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8');pending.replace(path)


def prepare(wb,c):
    p=wb.import_egp('Synthetic_'+c['id']+'.egp',(FROZEN/(c['id']+'.egp')).read_bytes())
    ids=list((c.get('sources') or {'P1':c['source']}))
    p=wb.command(p['id'],p['revision'],'set_project_scope',{'orders':[{'id':'main','label':'Synthetic complete '+c['id'],'source_ids':ids}]})
    external={'source.'+n for n in c['inputs']}
    for name in external-{i['original'] for i in p['context']['inputs']}:
        p=wb.command(p['id'],p['revision'],'declare_input',{'origin_source_id':ids[0],'name':{'parts':name.split('.')},'rationale':'Explicit frozen synthetic input, independent complete schema contract'})
    internal=[];bindings=[]
    for entry in p['context']['inputs']:
        if entry['original'] in external:
            name=entry['original'].split('.')[1]
            bindings.append({'source_id':entry['id'],'decision':'replace','target':{'parts':['src_'+c['id'],name]},'columns':c['inputs'][name]['columns'],
                'unique_keys':c['inputs'][name]['unique_keys'],'schema_complete':True,'metadata_status':'user_supplied'})
        elif entry['original'].casefold().startswith('work.') or entry['original'] in c['output_sources'].values():internal.append(entry['id'])
        else:raise ValueError('Unclassified input inventory: '+entry['original'])
    outputs=[];origins={};effects={}
    for name,columns in c['outputs'].items():
        original=c['output_sources'][name];parts=original.split('.')
        if parts[0]=='work':parts=parts[1:]
        key='.'.join(parts);origins[key]=original
        outputs.append({'name':{'parts':parts},'storage':'temporary' if len(parts)==1 else 'persistent','columns':[col['name'] for col in columns],'role':'final' if name in c['finals'] else 'intermediate'})
        effects[key]=['create','insert'] if c['id']=='e20' else ['create']
    plan={'scenario_id':p['scenario']['id'],'target':'greenplum','target_version':'7.0','acceptance':'code_review',
        'targets':['greenplum','postgresql','clickhouse'],'target_versions':{'greenplum':'7.0','postgresql':'17.10','clickhouse':'25.8'},
        'bindings':bindings,'intermediate_inputs':internal,'outputs':outputs,'output_sources':origins,'allowed_effects':effects,'test_mode':True,
        'max_model_calls':64,'max_run_seconds':2400,'max_repair_attempts':2,
        'validation_scope':'Full frozen constructed source; complete declared schema; testifier; row bags and declared type families checked separately. No native SAS claim.',
        'source_semantics':{'numeric_missing':'special_tags_lost' if c['id']=='n01' else 'ordinary_as_null','character_missing':'null_empty_ascii_spaces','character_storage':'unpadded_varchar','input_observation_order':'explicit_sort_only'},
        'resolutions':[{'issue_id':i['id'],'rationale':'Reviewed the complete constructed source and frozen synthetic contracts'} for i in p['context']['issues'] if not i['hard']]}
    p=wb.command(p['id'],p['revision'],'save_plan',plan)
    if p['blockers'] or p['task_plan']['requirements']:
        return p,{'blockers':p['blockers'],'requirements':p['task_plan']['requirements']}
    return wb.command(p['id'],p['revision'],'approve_plan',{}),None


def owner():return psycopg.connect(host='127.0.0.1',port=55441,dbname='synthetic_lab',user='lab_owner',password='synthetic_lab_only',autocommit=True)


def initialize():
    with owner() as db:
        assert db.execute('select current_database()').fetchone()[0]=='synthetic_lab'
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='sql_runner'").fetchone():db.execute("CREATE ROLE sql_runner LOGIN PASSWORD 'synthetic_runner_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
        db.execute('CREATE SCHEMA IF NOT EXISTS mart');db.execute('GRANT USAGE,CREATE ON SCHEMA mart TO sql_runner')


def seed_pg(c,v):
    schema='src_'+c['id']
    with owner() as db:
        loaded={}
        db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
        for name,definition in c['inputs'].items():
            columns=definition['columns']
            db.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} ({})').format(sql.Identifier(schema,name),sql.SQL(',').join(sql.SQL('{} {}').format(sql.Identifier(x['name']),sql.SQL(x['data_type'])) for x in columns)))
            db.execute(sql.SQL('TRUNCATE {}').format(sql.Identifier(schema,name)))
            if v['inputs'][name]:
                with db.cursor() as cursor:cursor.executemany(sql.SQL('INSERT INTO {} VALUES ({})').format(sql.Identifier(schema,name),sql.SQL(',').join(sql.Placeholder() for _ in columns)),v['inputs'][name])
            with db.cursor() as cur:
                cur.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema,name)))
                if [x.name for x in cur.description]!=[x["name"] for x in columns]:
                    raise ValueError("PostgreSQL fixture columns differ after load")
                actual=list(cur)
            types=db.execute("""SELECT format_type(atttypid,atttypmod) FROM pg_attribute
                WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum""",
                (sql.Identifier(schema,name).as_string(db),)).fetchall()
            if [type_spelling(t) for (t,) in types]!=[type_spelling(c["data_type"]) for c in columns]:
                raise ValueError("PostgreSQL fixture types differ after load")
            assert_loaded(columns,v["inputs"][name],actual)
            loaded[name]={"rows":len(actual),"types":[t for (t,) in types]}
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO sql_runner').format(sql.Identifier(schema)))
        db.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO sql_runner').format(sql.Identifier(schema)))
        # Existing production-name sentinels are owned by another role and never writable.
        for name in c['finals']:
            parts=c['output_sources'][name].split('.')
            db.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} (__protected INTEGER)').format(sql.Identifier(*parts)))
            if not db.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(*parts))).fetchone()[0]:db.execute(sql.SQL('INSERT INTO {} VALUES (918273)').format(sql.Identifier(*parts)))
    return loaded


def native_pg(c,v,artifacts,plan,*,normalized=False):
    loaded=seed_pg(c,v);result={'outputs':{},'rows':{},'verified_input':loaded}
    type_families={16:'boolean',20:'numeric',21:'numeric',23:'numeric',700:'numeric',701:'numeric',1700:'numeric',25:'text',1042:'text',1043:'text',1082:'date',1114:'timestamp',1184:'timestamp'}
    with psycopg.connect(host='127.0.0.1',port=55441,dbname='synthetic_lab',user='sql_runner',password='synthetic_runner_only') as db:
        try:
            db.execute("SET LOCAL statement_timeout='8s'; SET LOCAL lock_timeout='1s'; SET LOCAL work_mem='16MB'")
            for a in artifacts:db.execute(a['sql'],prepare=False)
            for name,columns in c['outputs'].items():
                original=c['output_sources'][name]
                target=next(o['name']['parts'] for o in plan['outputs'] if plan['output_sources']['.'.join(o['name']['parts'])]==original)
                with db.cursor(name="synthetic_output_"+str(len(result["rows"]))) as cursor:
                    cursor.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(*target)))
                    actual=list(cursor)
                    description=cursor.description
                result['rows'][name]=actual
                if normalized:
                    types=db.execute("""SELECT attname,format_type(atttypid,atttypmod)
                        FROM pg_catalog.pg_attribute WHERE attrelid=%s::regclass
                        AND attnum>0 AND NOT attisdropped ORDER BY attnum""",
                        (sql.Identifier(*target).as_string(db),)).fetchall()
                    schema=[{"name":key,"data_type":dtype} for key,dtype in types]
                    checked=compare_v2(columns,v["expected"][name],actual,actual_columns=schema,
                        expected_transport="archive",comparison_zone="UTC")
                    checked["actual_schema"]=schema
                    result["outputs"][name]=checked
                    continue
                checked=compare(columns,v['expected'][name],actual)
                actual_types=[type_families.get(col.type_code,str(col.type_code)) for col in description]
                checked['types_passed']=actual_types==[family(x['data_type']) for x in columns];checked['actual_families']=actual_types
                checked['schema_passed']=[col.name for col in description]==[col['name'] for col in columns]
                checked['passed'] &= checked['types_passed'] and checked['schema_passed'];result['outputs'][name]=checked
        except Exception as error:result['error']=str(error)[:1200]
        finally:db.rollback()
    with owner() as db:
        result['production_unchanged']=all(db.execute(sql.SQL('SELECT array_agg(__protected) FROM {}').format(sql.Identifier(*c['output_sources'][n].split('.')))).fetchone()[0]==[918273] for n in c['finals'])
    result['passed']=not result.get('error') and len(result['outputs'])==len(c['outputs']) and all(x['passed'] for x in result['outputs'].values()) and result['production_unchanged']
    return result


def ch_type(col):
    t=col['data_type'].strip().upper();kind=family(t)
    decimal=re.fullmatch(r"(?:NUMERIC|DECIMAL)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)",t)
    if decimal:
        precision,scale=map(int,decimal.groups())
        if not 1<=precision<=76 or not 0<=scale<=precision:
            raise ValueError("Unsupported ClickHouse fixture precision/scale")
        value=f"Decimal({precision}, {scale})"
    elif t in {"SMALLINT","INTEGER","INT","BIGINT","REAL","FLOAT4","DOUBLE PRECISION","FLOAT8","FLOAT","BOOLEAN"}:
        value={"SMALLINT":"Int16","INTEGER":"Int32","INT":"Int32","BIGINT":"Int64",
               "REAL":"Float32","FLOAT4":"Float32","BOOLEAN":"Bool"}.get(t,"Float64")
    elif kind in {"text","date","timestamp"}:
        value={"text":"String","date":"Date32","timestamp":"DateTime64(6,'UTC')"}[kind]
    else:
        raise ValueError("No explicit ClickHouse fixture type for "+t)
    return 'Nullable('+value+')' if col.get('nullable') is not False else value


def type_spelling(value):
    value=re.sub(r"\s+","",value.lower())
    return (value.replace("character varying".replace(" ",""),"varchar")
            .replace("timestampwithouttimezone","timestamp").replace("decimal","numeric"))


def assert_loaded(columns,expected,actual):
    def cell(c,v):
        if v is None:return None
        t=type_spelling(c["data_type"])
        if t in {"real","float4"}:return struct.pack("!f",float(v))
        if t in {"doubleprecision","float8","float"}:return struct.pack("!d",float(v))
        if family(c["data_type"])=="numeric":return Decimal(str(v))
        if family(c["data_type"])=="timestamp":
            return datetime.fromisoformat(str(v)).isoformat()
        return str(v)
    def bag(rows):
        return Counter(tuple(cell(c,v) for c,v in zip(columns,row,strict=True)) for row in rows)
    if bag(expected)!=bag(actual):
        raise ValueError("Fixture values changed during native load")


def ch_query(query):
    return subprocess.run(['docker','exec','-i',CH,'clickhouse-client','--multiquery','--max_execution_time','8'],input=query,text=True,encoding='utf-8',errors='replace',capture_output=True,timeout=30)


def verify_ch_load(schema, inputs, rows):
    loaded={}
    for name,spec in inputs.items():
        table=f"`{schema}`.`{name}`"
        detail=ch_query(f"DESCRIBE TABLE {table} FORMAT JSONEachRow")
        values=ch_query(f"SELECT * FROM {table} FORMAT JSONEachRow")
        if detail.returncode or values.returncode:
            raise ValueError("ClickHouse fixture readback failed")
        actual_schema=[(v["name"],v["type"]) for line in detail.stdout.splitlines() if (v:=json.loads(line))]
        expected_schema=[(c["name"],ch_type(c)) for c in spec["columns"]]
        if [(n,type_spelling(t)) for n,t in actual_schema]!=[(n,type_spelling(t)) for n,t in expected_schema]:
            raise ValueError("ClickHouse fixture physical types differ after load")
        actual=[[v[c["name"]] for c in spec["columns"]] for line in values.stdout.splitlines()
                if (v:=json.loads(line,parse_float=Decimal))]
        assert_loaded(spec["columns"],rows[name],actual)
        loaded[name]={"rows":len(actual),"types":actual_schema}
    return loaded


def native_ch(c,v,artifacts,plan,*,normalized=False):
    schema='src_'+c['id'];parts=[f'CREATE DATABASE IF NOT EXISTS {schema}; CREATE DATABASE IF NOT EXISTS mart;']
    for name,definition in c['inputs'].items():
        columns=definition['columns'];parts.append(f'CREATE OR REPLACE TABLE {schema}.{name} ('+','.join('`'+x['name']+'` '+ch_type(x) for x in columns)+') ENGINE=Memory;')
    seeded=ch_query('\n'.join(parts))
    if seeded.returncode:return {'passed':False,'error':'fixture_seed: '+seeded.stderr[:800]}
    # FORMAT data consumes stdin to EOF; do not concatenate another SQL statement
    # after JSONEachRow. Each external input has a separate bounded insert call.
    for name,definition in c['inputs'].items():
        if not v['inputs'][name]:continue
        data='\n'.join(json.dumps(dict(zip([x['name'] for x in definition['columns']],row)),ensure_ascii=False) for row in v['inputs'][name])+'\n'
        seeded=subprocess.run(['docker','exec','-i',CH,'clickhouse-client','--query',f'INSERT INTO {schema}.{name} FORMAT JSONEachRow'],input=data,text=True,encoding='utf-8',capture_output=True,timeout=30)
        if seeded.returncode:return {'passed':False,'error':'fixture_seed: '+seeded.stderr[:800]}
    # CLI batches require statement separators; DB-API accepts an unterminated
    # final statement. Add a separator after a newline to preserve trailing comments.
    queries=[a['sql']+'\n;' for a in artifacts];targets={}
    for name,columns in c['outputs'].items():
        target=next(o['name']['parts'] for o in plan['outputs'] if plan['output_sources']['.'.join(o['name']['parts'])]==c['output_sources'][name]);targets[name]=target
        physical='.'.join('`'+p+'`' for p in target)
        queries += [f"SELECT '{name}' AS __output, 'begin' AS __kind FORMAT JSONEachRow;",f'DESCRIBE TABLE {physical} FORMAT JSONEachRow;',
            f"SELECT '{name}' AS __output, 'data' AS __kind,* FROM {physical} FORMAT JSONEachRow;"]
    result={'outputs':{},'rows':{}};types={};current=None
    try:
        result["verified_input"]=verify_ch_load(schema,c["inputs"],v["inputs"])
        with TemporaryFile(mode="w+t",encoding="utf-8") as stream:
            proc=subprocess.run(['docker','exec','-i',CH,'clickhouse-client','--multiquery','--max_execution_time','8'],
                input='\n'.join(queries),text=True,encoding='utf-8',errors='replace',stdout=stream,stderr=subprocess.PIPE,timeout=30)
            if proc.returncode:
                raise ValueError(proc.stderr[-1400:])
            stream.seek(0)
            for line in stream:
                row=json.loads(line,parse_float=Decimal) if normalized else json.loads(line)
                if row.get('__kind')=='begin':current=row['__output'];result['rows'][current]=[];types[current]={}
                elif row.get('__kind')=='data':result['rows'][row['__output']].append([row[x['name']] for x in c['outputs'][row['__output']]])
                elif current and 'name' in row and 'type' in row:types[current][row['name']]=row['type']
            for name,columns in c['outputs'].items():
                if name not in result['rows']:result['outputs'][name]={'passed':False,'error':'missing_output'};continue
                if normalized:
                    schema=[{"name":key,"data_type":dtype} for key,dtype in types[name].items()]
                    checked=compare_v2(columns,v["expected"][name],result["rows"][name],actual_columns=schema,
                        expected_transport="archive",actual_transport="archive",comparison_zone="UTC")
                    checked["actual_schema"]=schema
                    result["outputs"][name]=checked
                    continue
                checked=compare(columns,v['expected'][name],result['rows'][name]);checked['types_passed']=True
                for column in columns:
                    typ=types[name].get(column['name'],'missing').replace('Nullable(','').rstrip(')')
                    if family(typ)!=family(column['data_type']):checked['types_passed']=False
                checked['schema_passed']=list(types[name])==[col['name'] for col in columns]
                checked['passed'] &= checked['types_passed'] and checked['schema_passed'];checked['actual_types']=types[name];result['outputs'][name]=checked
    except Exception as error:result['error']=str(error)[:1000]
    finally:
        cleanup=['DROP TABLE IF EXISTS '+'.'.join('`'+p+'`' for p in target)+';' for target in targets.values() if len(target)==2 and target[-1].endswith('_test')]
        if cleanup:ch_query('\n'.join(cleanup))
    result['passed']=not result.get('error') and len(result['outputs'])==len(c['outputs']) and all(x['passed'] for x in result['outputs'].values())
    return result


def diagnostic_artifacts(result,target,task_plan):
    artifacts=[]
    for unit in task_plan['tasks']:
        record=result['units'].get(target+':'+unit['id'],{})
        candidate=record.get('candidate')
        if not candidate:
            attempt=next((a for a in reversed(record.get('attempts',[])) if a.get('static',{}).get('static_passed') and not (a.get('candidate') or {}).get('assumptions')),None)
            candidate=attempt.get('candidate') if attempt else None
        if not candidate:return None
        artifacts += candidate['artifacts']
    return artifacts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--phase',default='initial');parser.add_argument('--cases',default='');parser.add_argument('--resume',action='store_true');parser.add_argument('--thinking',action='store_true');parser.add_argument('--strong-model',action='store_true');parser.add_argument('--compact',action='store_true');args=parser.parse_args()
    manifest=json.loads((FROZEN/'manifest.json').read_text());raw=(FROZEN/'corpus.json').read_bytes();assert hashlib.sha256(raw).hexdigest()==manifest['corpus_sha256']
    cases=json.loads(raw);selected=set(args.cases.split(',')) if args.cases else {c['id'] for c in cases}
    base=Runtime.load(args.config).model
    assert base and base.exposure=='synthetic_only'
    model=replace(base,authorized_budget_usd=5,budget_usd=5,max_request_bytes=180000,max_output_tokens=8000,synthetic_archives=tuple(c['archive_sha256'] for c in cases))
    if args.thinking:model=replace(model,extra={'thinking':{'type':'enabled'}},max_output_tokens=16000,request_timeout_seconds=600)
    if args.strong_model:
        # Official USD rates: 2 input / 10 output / 2 or 4 cache write.
        # Bound all input by 6, covering even a 1h write plus uncached read.
        # Same approved provider/endpoint, same synthetic-only archive allowlist.
        model=replace(model,model='kimi-k3.1-pro',input_usd_per_million=6,output_usd_per_million=10,
            extra={'reasoning_effort':'low'},max_output_tokens=12000,request_timeout_seconds=300)
    if args.compact:model=replace(model,max_output_tokens=2000,input_usd_per_million=4 if args.strong_model else model.input_usd_per_million)
    wb=Workbench(PRIVATE);runtime=Runtime(model=model);initialize();OUT.mkdir(parents=True,exist_ok=True)
    for c in cases:
        if c['id'] not in selected:continue
        path=OUT/args.phase/(c['id']+'.json')
        if path.exists() and not args.resume:continue
        started=time.monotonic();record={'id':c['id'],'title':c['title'],'negative':c['negative'],'corpus_sha256':manifest['corpus_sha256'],'profiles':{}}
        try:
            if args.resume and path.exists():
                old=json.loads(path.read_text(encoding='utf-8'));p=wb.get(old['project_id']);problem=None
            else:p,problem=prepare(wb,c)
            record['project_id']=p['id'];record['preparation']=problem
            if problem:save(path,record);print(json.dumps({'id':c['id'],'preparation_blocked':True}),flush=True);continue
            pipeline=run(wb,p,runtime);p=wb.record_pipeline(p['id'],p['revision'],pipeline)
            record['model_configuration']={'model':model.model,'extra':model.extra,'max_output_tokens':model.max_output_tokens,
                'input_accounting_bound_per_million':model.input_usd_per_million,'output_rate_per_million':model.output_usd_per_million}
            record['requirements']=pipeline['requirements']
            save(OUT/args.phase/(c['id']+'-pipeline.json'),pipeline)
            for target in ('greenplum','postgresql','clickhouse'):
                profile=pipeline['profiles'].get(target,{})
                tested={'code_passed':profile.get('passed',False),'execution':False,'variants':[],'expected_refusal':c['negative'] and not profile.get('passed',False)}
                artifacts=profile.get('scenarios',{}).get('main',{}).get('artifacts') if profile.get('passed') else diagnostic_artifacts(pipeline,target,p['task_plan'])
                tested['diagnostic_candidate']=bool(artifacts) and not tested['code_passed']
                if not c['negative'] and artifacts and target!='greenplum':
                    plan=scenario_plan(p,'main',target)
                    for variant in c['variants']:
                        execution=(native_pg if target=='postgresql' else native_ch)(c,variant,artifacts,plan,normalized=True)
                        tested['variants'].append({'name':variant['name'],**execution})
                    tested['execution']=True;tested['all_variants_passed']=all(v['passed'] for v in tested['variants'])
                record['profiles'][target]=tested
                if tested['execution'] and tested['code_passed'] and p.get('candidate'):
                    from migration_v2.contracts import digest
                    p=wb.record_test_evidence(p['id'],p['revision'],{'task_hash':p['task']['hash'],'candidate_hash':digest(p['candidate']),
                        'dataset_hash':digest(c['variants']),'oracle_hash':manifest['oracle_code_sha256'],'target':target,
                        'policy_hash':POLICY_HASH,'matched':tested['all_variants_passed'],'summary':'Frozen synthetic variants: '+str(sum(v['passed'] for v in tested['variants']))+'/4 matched'})
            if pipeline['release_allowed'] and p['evidence']['release_allowed']:p=wb.command(p['id'],p['revision'],'approve_release',{})
            record['accepted_code']=bool(p.get('gate2'))
            record['budget']=Budget(wb).status()
        except Exception as error:record['error']=type(error).__name__+': '+str(error)[:1400]
        record['seconds']=round(time.monotonic()-started,2);save(path,record)
        print(json.dumps({'id':c['id'],'accepted_code':record.get('accepted_code'),
            'profiles':{t:{k:r.get(k) for k in ('code_passed','all_variants_passed','expected_refusal')} for t,r in record['profiles'].items()},'budget':Budget(wb).status()},ensure_ascii=True),flush=True)
        if any('budget exhausted' in str(x).lower() for x in (record.get('error',''),record.get('requirements',[]))):break
    save(OUT/'budget.json',{'authorized_usd':5,'synthetic_only':True,**Budget(wb).status()})


if __name__=='__main__':main()
