"""Run frozen synthetic source in the pinned independent OpenSAS container."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import time
from fixtures.testifier_lab.compare import compare

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'fixtures/testifier_lab/frozen'
OUT=ROOT/'reports/testifier-bi-20261108'
IMAGE='sas-testifier-opensas:4ef0651a'


def literal(v,typ):
    if v is None:return "''" if typ.startswith(('VARCHAR','CHAR')) else '.'
    if typ.startswith(('VARCHAR','CHAR')):return "'"+str(v).replace("'","''")+"'"
    if typ.startswith(('DATE','TIMESTAMP')):
        d=datetime.fromisoformat(v);months=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
        s=f'{d.day:02}{months[d.month-1]}{d.year}'
        return "'"+s+(d.strftime(':%H:%M:%S')+"'dt" if typ.startswith('TIMESTAMP') else "'d")
    return str(v)


def program(c,variant):
    parts=["/* SYNTHETIC ONLY. Independent runtime; not SAS Institute SAS. */", "libname source '/work'; libname mart '/work';"]
    for name,definition in c['inputs'].items():
        columns=definition['columns'];parts.append('data source.'+name+';')
        for col in columns:
            typ=col['data_type'];width='$'+str(int(typ.split('(')[1].split(')')[0])) if typ.startswith(('VARCHAR','CHAR')) else '8'
            parts.append('length '+col['name']+' '+width+';')
        for row in variant['inputs'][name]:
            parts.append(' '.join(col['name']+'='+literal(v,col['data_type'])+';' for col,v in zip(columns,row))+' output;')
        parts.append('stop; run;')
    parts.append(c['source'])
    for name in c['outputs']:
        parts.append(f'proc export data={c["output_sources"][name]} outfile="/out/{name}.csv" dbms=csv replace; run;')
    return '\n'.join(parts)


def run_one(job):
    c,variant=job;folder=ROOT/'private_inputs/testifier-opensas'/c['id']/variant['name'];folder.mkdir(parents=True,exist_ok=True)
    script=folder/'program.sas';script.write_text(program(c,variant),encoding='utf-8');output=folder/'out';output.mkdir(exist_ok=True)
    started=time.monotonic();name='sas-lab-'+c['id']+'-'+variant['name'].replace('_','-')
    command=['docker','run','--rm','--name',name,'--network','none','--read-only','--tmpfs','/work:rw,mode=1777,size=64m','--tmpfs','/tmp:rw,mode=1777,size=64m',
        '--memory','384m','--cpus','1','--pids-limit','64','--mount',f'type=bind,source={script},target=/program.sas,readonly',
        '--mount',f'type=bind,source={output},target=/out',IMAGE,'/program.sas']
    try:process=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker','rm','-f',name],capture_output=True)
        return {'id':c['id'],'variant':variant['name'],'passed':False,'error':'runtime_timeout'}
    result={'id':c['id'],'variant':variant['name'],'exit_code':process.returncode,'seconds':round(time.monotonic()-started,3),'outputs':{},'rows':{},'log_tail':(process.stdout+process.stderr)[-1600:]}
    for name,columns in c['outputs'].items():
        path=output/(name+'.csv')
        if not path.exists():result['outputs'][name]={'passed':False,'error':'missing_output'};continue
        try:
            with path.open(encoding='utf-8-sig',newline='') as f:
                reader=csv.DictReader(f);headers={n.casefold():n for n in reader.fieldnames or []}
                if set(headers)!={col['name'].casefold() for col in columns}:raise ValueError('columns_mismatch')
                rows=[[row[headers[col['name'].casefold()]] for col in columns] for row in reader]
            result['rows'][name]=rows
            result['outputs'][name]=compare(columns,variant['expected'][name],rows) if not c['negative'] else {'passed':None,'reason':'negative/information-loss case; runtime execution is not proof of recoverability'}
        except Exception as error:result['outputs'][name]={'passed':False,'error':str(error)[:120]}
    result['passed']=None if c['negative'] else process.returncode==0 and all(r['passed'] for r in result['outputs'].values())
    return result


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    corpus=json.loads((FROZEN/'corpus.json').read_text(encoding='utf-8'));manifest=json.loads((FROZEN/'manifest.json').read_text())
    assert hashlib.sha256((FROZEN/'corpus.json').read_bytes()).hexdigest()==manifest['corpus_sha256']
    jobs=[(c,v) for c in corpus for v in c['variants']]
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run_one,jobs))
    report={'engine':'OpenSAS independent interpreter, not SAS Institute runtime','git_commit':'4ef0651a4bed1dc0a956fc4a8eff35b8cf6abead','image':IMAGE,
        'corpus_sha256':manifest['corpus_sha256'],'api_calls':0,'results':results}
    (OUT/'opensas.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    summary={'positive_variants':sum(r['passed'] is not None for r in results),'passed':sum(r['passed'] is True for r in results),'failed':sum(r['passed'] is False for r in results)}
    print(json.dumps(summary))


if __name__=='__main__':main()
