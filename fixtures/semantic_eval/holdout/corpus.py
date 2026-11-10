"""New code holdout, frozen after generic guidance but before any model sees it."""
import hashlib
import json
from pathlib import Path
import random

from fixtures.semantic_eval.corpus import case,cols,table,archive,I,K,N
from .oracles import expected

ROOT=Path(__file__).resolve().parent


def cases():
    a=case('h01','Explicit OUTPUT snapshots to two datasets; no implicit final row','''data work.before(keep=id amount stage) work.after(keep=id amount stage);
set source.events;
stage=1;
if amount<0 then output work.before;
amount=sum(amount,2);
stage=2;
if flag=1 then output work.after;
run;''',{'events':table(cols(id=I,amount=N,flag=K),[['id']])},
        {n:cols(id=I,amount=N,stage=I) for n in ('before','after')})
    b=case('h02','Nested macro calls and macro IF with scoped arguments','''%let chosen=2;
%macro inner(mult=1);
data work.selected;
set source.events;
value=amount*&mult;
run;
%mend;
%macro outer(mode=1);
%if &mode=2 %then %do;
%inner(mult=3);
%end;
%else %do;
%inner(mult=7);
%end;
%mend;
%outer(mode=&chosen);
proc sql;
create table work.result as select g,count(*) as n,sum(value) as total
from work.selected group by g;
quit;''',{'events':table(cols(id=I,g=K,amount=N),[['id']])},
        {'selected':cols(id=I,g=K,amount=N,value=N),'result':cols(g=K,n=I,total=N)})
    return [a,b]


def dataset(ident,seed,empty):
    rng=random.Random(seed)
    if ident=='h01':
        pairs=[(None,1),(None,0),(-3,1),(-2,0),(0,1),(5,1),(4,0),(1,None)]
        if seed is not None:pairs += [(rng.choice([None,-9,-1,0,2,7]),rng.choice([None,0,1])) for _ in range(80)]
        rows=[[i+1,a,f] for i,(a,f) in enumerate(pairs)]
    else:
        pairs=[(None,None),(None,3),(1,None),(1,None),(2,2),(2,0),(3,-2),(4,9)]
        if seed is not None:pairs += [(rng.choice([None,1,2,3,4]),rng.choice([None,-3,0,7])) for _ in range(80)]
        rows=[[i+1,g,a] for i,(g,a) in enumerate(pairs)]
    if seed is not None:rng.shuffle(rows)
    return {'events':[] if empty else rows}


def freeze():
    target=ROOT/'frozen'
    if target.exists():raise ValueError('Holdout already frozen; do not overwrite evidence')
    target.mkdir()
    frozen=[]
    for c in cases():
        variants=[]
        for name,seed,empty in [('edge',None,False),('seed_211',211,False),('seed_307',307,False),('empty',None,True)]:
            data=dataset(c['id'],seed,empty)
            variants.append({'name':name,'inputs':data,'expected':expected(c['id'],data)})
        raw=archive(c);(target/(c['id']+'.egp')).write_bytes(raw)
        (target/(c['id']+'.sas')).write_text(c['source'],encoding='utf-8')
        frozen.append({**c,'archive_sha256':hashlib.sha256(raw).hexdigest(),'variants':variants})
    payload=json.dumps(frozen,ensure_ascii=False,indent=2).encode()
    (target/'corpus.json').write_bytes(payload)
    manifest={'synthetic':True,'sas_runtime':'not_available','cases':2,'positive_cases':2,'variants_per_case':4,
        'corpus_sha256':hashlib.sha256(payload).hexdigest(),
        'oracle_sha256':hashlib.sha256((ROOT/'oracles.py').read_bytes()).hexdigest()}
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest))


if __name__=='__main__':freeze()
