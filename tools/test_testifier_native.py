"""Synthetic destination protection in the dedicated disposable PostgreSQL lab."""
from copy import deepcopy
import psycopg
from migration_v2.destinations import resolve,seed_artifacts
from migration_v2.linker import link
from tools.run_testifier_lab import owner,OUT,save


def main():
    with owner() as db:
        db.execute('CREATE SCHEMA IF NOT EXISTS qa_testifier')
        db.execute('GRANT USAGE,CREATE ON SCHEMA qa_testifier TO sql_runner')
        db.execute('CREATE TABLE IF NOT EXISTS qa_testifier.original(id INTEGER,value INTEGER)')
        db.execute('TRUNCATE qa_testifier.original')
        db.execute('INSERT INTO qa_testifier.original VALUES(1,10),(2,NULL),(3,10)')
        db.execute('GRANT SELECT ON qa_testifier.original TO sql_runner')
    base={'outputs':[{'name':{'parts':['qa_testifier','original']},'storage':'persistent','columns':['id','value'],'role':'final'}],
        'bindings':[{'source_id':'synthetic','target':{'parts':['qa_testifier','original']},'columns':[{'name':'id','data_type':'INTEGER'},{'name':'value','data_type':'INTEGER'}]}],
        'allowed_effects':{'qa_testifier.original':['update']},'test_mode':True}
    checks={}
    for mode in ('update','create','temporary_override'):
        plan=deepcopy(base)
        if mode=='create':plan['allowed_effects']['qa_testifier.original']=['create']
        if mode=='temporary_override':plan['output_destinations']={'qa_testifier.original':{'parts':['work','copied']}}
        resolved=resolve(plan);effective=resolved['plan'];target='.'.join(effective['outputs'][0]['name']['parts'])
        artifacts=seed_artifacts(resolved['seeds'],'postgresql')
        artifacts.append({'name':'operation.sql','sql':
            'CREATE TABLE '+target+' AS SELECT id,value+1 AS value FROM qa_testifier.original;' if mode=='create' else
            'UPDATE '+target+' SET value=value+1;'})
        checked=link(effective,artifacts,'postgresql');assert checked['static_passed'],checked['findings']
        with psycopg.connect(host='127.0.0.1',port=55441,dbname='synthetic_lab',user='sql_runner',password='synthetic_runner_only') as db:
            for artifact in artifacts:db.execute(artifact['sql'],prepare=False)
            rows=db.execute('SELECT id,value FROM '+target+' ORDER BY id').fetchall()
            original=db.execute('SELECT id,value FROM qa_testifier.original ORDER BY id').fetchall()
            assert rows==[(1,11),(2,None),(3,11)] and original==[(1,10),(2,None),(3,10)]
            db.rollback()
        checks[mode]={'passed':True,'original_unchanged':True,'destination':target,'rows':rows,'seeded':bool(resolved['seeds'])}
    with owner() as db:
        checks['rollback_removed_test_outputs']=not db.execute("SELECT to_regclass('qa_testifier.original_test')").fetchone()[0]
    save(OUT/'testifier-native.json',{'synthetic_only':True,'target':'PostgreSQL 17.10','api_calls':0,'checks':checks})
    print(checks)


if __name__=='__main__':main()
