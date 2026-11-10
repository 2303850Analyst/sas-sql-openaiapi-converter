"""Synthetic SAS programs and input distributions; no generated SQL or reference rows in prompts."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import random
import zipfile

ROOT = Path(__file__).resolve().parent
N = ('NUMERIC(18,2)', True)
I = ('INTEGER', False)
K = ('INTEGER', True)
C = ('VARCHAR(40)', True)


def cols(**items):
    return [{'name':n,'data_type':v[0],'nullable':v[1]} for n,v in items.items()]


def table(columns, keys=()):
    return {'columns':columns, 'unique_keys':list(keys)}


def case(ident, title, source, inputs, outputs, negative=False):
    return {'id':ident, 'title':title, 'source':'/* Synthetic semantic evaluation; no corporate source. */\n'+source,
            'inputs':inputs, 'outputs':outputs, 'negative':negative}


def cases():
    result=[]
    e=cols(id=I,x=N,y=N)
    result.append(case('t01','Numeric missing, comparisons, SUM versus addition', '''data work.result(keep=id miss low truth row_sum ordinary);
set source.events;
miss=missing(x);
if x<0 then low=1; else low=0;
if x then truth=1; else truth=0;
row_sum=sum(x,y);
ordinary=x+y;
run;''', {'events':table(e,[['id']])}, {'result':cols(id=I,miss=I,low=I,truth=I,row_sum=N,ordinary=N)}))
    e=cols(id=I,code=C)
    result.append(case('t02','Character missing and trailing versus leading blanks', '''data work.result(keep=id miss equal_code marker);
length marker $30;
set source.events;
miss=missing(code);
if code='ABC' then equal_code=1; else equal_code=0;
marker='run; quit; proc sql;';
run;''', {'events':table(e,[['id']])}, {'result':cols(id=I,miss=I,equal_code=I,marker=C)}))
    e=cols(id=I,k=K,score=N,seq=I)
    result.append(case('t03','Nullable ASC/DESC, ties and FIRST', '''proc sort data=source.events out=work.hi;
by k descending score seq;
run;
data work.top;
set work.hi;
by k descending score seq;
if first.k;
run;
proc sort data=source.events out=work.lo;
by k score seq;
run;
data work.bottom;
set work.lo;
by k score seq;
if first.k;
run;''', {'events':table(e,[['id'],['seq']])}, {n:e for n in ('hi','top','lo','bottom')}))
    left=cols(id=I,k=K,sensor=C,amount=N)
    right=cols(k=K,sensor=C,bias=N)
    result.append(case('t04','Many-to-one MERGE with missing composite BY keys', '''proc sort data=source.lefts out=work.l;
by k sensor id;
run;
proc sort data=source.rights out=work.r;
by k sensor;
run;
data work.result(keep=id amount bias adjusted);
merge work.l(in=a) work.r(in=b);
by k sensor;
if a;
adjusted=sum(amount,bias);
run;''', {'lefts':table(left,[['id']]),'rights':table(right,[['k','sensor']])},
                       {'l':left,'r':right,'result':cols(id=I,amount=N,bias=N,adjusted=N)}))
    left=cols(k=K,lseq=I,lvalue=N);right=cols(k=K,rseq=I,rvalue=N)
    result.append(case('t05','Many-to-many MERGE retains exhausted side; IN= is group state', '''proc sort data=source.lefts out=work.l;
by k lseq;
run;
proc sort data=source.rights out=work.r;
by k rseq;
run;
data work.result;
merge work.l(in=a) work.r(in=b);
by k;
if a;
keep k lseq lvalue rseq rvalue;
run;''', {'lefts':table(left,[['lseq']]),'rights':table(right,[['rseq']])},
                       {'l':left,'r':right,'result':cols(k=K,lseq=I,lvalue=N,rseq=K,rvalue=N)}))
    left=cols(k=K,id=I,age=N);right=cols(k=K,rseq=I,age=N,rvalue=N)
    result.append(case('t06','MERGE common variable overwritten by later observations', '''proc sort data=source.lefts out=work.l;
by k id;
run;
proc sort data=source.rights out=work.r;
by k rseq;
run;
data work.result;
merge work.l(in=a) work.r(in=b);
by k;
if a;
keep k id age rvalue;
run;''', {'lefts':table(left,[['id']]),'rights':table(right,[['rseq']])},
                       {'l':left,'r':right,'result':cols(k=K,id=I,age=N,rvalue=N)}))
    e=cols(id=I,g=K,seq=I,amount=N)
    result.append(case('t07','SUM statement reset at FIRST and explicit row order', '''proc sort data=source.events out=work.ordered;
by g seq;
run;
data work.result(keep=id g seq total);
set work.ordered;
by g seq;
if first.g then total=0;
total+amount;
run;''', {'events':table(e,[['id'],['seq']])}, {'ordered':e,'result':cols(id=I,g=K,seq=I,total=N)}))
    e=cols(id=I,seq=I,active=K,amount=N)
    result.append(case('t08','Conditional LAG queue, including a missing queued value', '''proc sort data=source.events out=work.ordered;
by seq;
run;
data work.result(keep=id prev);
set work.ordered;
if active=1 then prev=lag(amount);
else prev=.;
run;''', {'events':table(e,[['id'],['seq']])}, {'ordered':e,'result':cols(id=I,prev=N)}))
    e=cols(id=I,x=N,y=N)
    result.append(case('t09','WHERE before assignments; sequential IFs see updated values', '''data work.result(keep=id x y);
set source.events;
where x>=0;
x=x+1;
if x>=1;
if x<3 then y=10;
if y=10 then y=y+x;
run;''', {'events':table(e,[['id']])}, {'result':e}))
    e=cols(id=I,g=K,active=K,observed_at=('TIMESTAMP',True),amount=N)
    result.append(case('t10','Macro argument, leap day, INTNX DATEPART and grouped missing values', '''%let start='28FEB2024:00:00:00'dt;
%let finish='01APR2024:00:00:00'dt;
%macro summarize(active_flag=1);
proc sql;
create table work.result as
select g,intnx('month',datepart(observed_at),0,'b') as month format=yymmdd10.,
count(*) as n,sum(amount) as total
from source.events
where active=&active_flag and observed_at>=&start and observed_at<&finish
group by g,calculated month;
quit;
%mend;
%summarize(active_flag=1);''', {'events':table(e,[['id']])}, {'result':cols(g=K,month=('DATE',False),n=I,total=N)}))
    e=cols(id=I)
    result.append(case('t11','PUT Z5 and DATA step LENGTH truncation', '''data work.result;
length code $5 short $3;
set source.events;
if id<0 or id>99999 then delete;
code=put(id,z5.);
short='ABCDEF';
run;''', {'events':table(e,[['id']])}, {'result':cols(id=I,code=('VARCHAR(5)',False),short=('VARCHAR(3)',False))}))
    e=cols(id=I,x=N)
    result.append(case('t12','SET concatenation keeps duplicates; RENAME and DROP', '''data work.result(drop=old);
set source.firsts(rename=(x=old)) source.seconds(rename=(x=old));
value=sum(old,1);
run;''', {'firsts':table(e),'seconds':table(e)}, {'result':cols(id=I,value=N)}))
    e=cols(id=I,x=N)
    result.append(case('n01','Cannot recover special .A after its tag is lost', '''data work.result(keep=id flag);
set source.events;
if x=.A then flag=1; else flag=0;
run;''', {'events':table(e,[['id']])}, {'result':cols(id=I,flag=I)},True))
    e=cols(k=K,value=N)
    result.append(case('n02','NODUPKEY EQUALS without recoverable input observation order', '''proc sort data=source.events out=work.result nodupkey equals;
by k;
run;''', {'events':table(e)}, {'result':e},True))
    return result


def dataset(ident, seed=None, empty=False):
    rng=random.Random(seed)
    if ident in ('t01','t09'):
        pairs=[(None,None),(None,5),(-3,None),(0,None),(0,0),(1,10),(2,10),(3,10),(4,None),(-1,9)]
        if seed is not None:pairs += [(rng.choice([None,-5,-1,0,1,2,3,7]),rng.choice([None,0,3,10])) for _ in range(80)]
        result={'events':[[i+1,a,b] for i,(a,b) in enumerate(pairs)]}
    elif ident=='t02':
        values=[None,'',' ','    ','ABC','ABC   ',' ABC','\t','\u00a0','abc','0','A B','run; quit;']
        if seed is not None:values += [rng.choice(values) for _ in range(80)]
        result={'events':[[i+1,v] for i,v in enumerate(values)]}
    elif ident=='t03':
        pairs=[(None,None),(None,4),(None,4),(1,None),(1,7),(1,7),(2,None),(2,None),(3,-5),(3,0)]
        if seed is not None:pairs += [(rng.choice([None,1,2,3,4]),rng.choice([None,-8,0,2,9])) for _ in range(80)]
        result={'events':[[i+1,k,v,i+1] for i,(k,v) in enumerate(pairs)]}
    elif ident=='t04':
        keys=[(None,None),(1,''),(1,'A'),(1,' A'),(2,'B'),(3,'C')]
        result={'lefts':[[1,None,'',5],[2,None,' ',None],[3,1,None,2],[4,1,'  ',3],[5,1,'A   ',10],
                         [6,1,' A',5],[7,2,'B',None],[8,9,'Z',4]],
                'rights':[[k,s,v] for (k,s),v in zip(keys,[10,20,30,40,None,70])]}
        if seed is not None:
            for i in range(80):
                k,s=rng.choice(keys);s=rng.choice([None,'',' ','   ']) if not s else s+(' '*rng.randrange(3))
                result['lefts'].append([9+i,k,s,rng.choice([None,0,5,-2])])
    elif ident=='t05':
        result={'lefts':[[None,1,10],[1,2,11],[1,3,None],[2,4,20],[2,5,21],[2,6,22],[4,7,40]],
                'rights':[[None,1,100],[None,2,101],[1,3,110],[1,4,111],[1,5,112],[2,6,120],[3,7,130]]}
        if seed is not None:
            for side,base in [('lefts',80),('rights',150)]:
                for i in range(40):result[side].append([rng.choice([None,1,2,3,4,5]),base+i,rng.choice([None,0,2,8])])
    elif ident=='t06':
        result={'lefts':[[1,1,11],[1,2,None],[1,3,14],[2,4,19],[None,5,7]],
                'rights':[[1,1,8,100],[2,2,None,200],[2,3,9,201],[None,4,2,300]]}
        if seed is not None:
            for i in range(40):
                result['lefts'].append([rng.choice([None,1,2,3]),80+i,rng.choice([None,8,11])])
                result['rights'].append([rng.choice([None,1,2,4]),150+i,rng.choice([None,9,13]),rng.choice([None,100,200])])
    elif ident=='t07':
        pairs=[(None,None),(None,5),(1,None),(1,3),(1,None),(1,-1),(2,None),(2,None)]
        if seed is not None:pairs += [(rng.choice([None,1,2,3]),rng.choice([None,0,7,-2])) for _ in range(80)]
        result={'events':[[i+1,g,i+1,a] for i,(g,a) in enumerate(pairs)]}
    elif ident=='t08':
        pairs=[(0,90),(1,10),(0,99),(1,None),(0,42),(1,20),(1,0),(None,7),(1,3)]
        if seed is not None:pairs += [(rng.choice([None,0,1]),rng.choice([None,0,-4,8])) for _ in range(80)]
        result={'events':[[i+1,i+1,a,v] for i,(a,v) in enumerate(pairs)]}
    elif ident=='t10':
        dates=[None,'2024-02-27 23:59:59','2024-02-28 00:00:00','2024-02-29 12:30:00',
               '2024-03-01 00:00:00','2024-03-31 23:59:59','2024-04-01 00:00:00']
        result={'events':[[1,None,1,dates[2],None],[2,None,1,dates[3],None],
                         [3,1,1,dates[3],5],[4,1,0,dates[4],99],[5,1,1,dates[4],0],
                         [6,1,1,dates[5],2],[7,1,1,dates[6],100],[8,1,1,None,2],
                         [9,2,1,dates[1],8],[10,2,None,dates[3],7]]}
        if seed is not None:
            result['events'] += [[11+i,rng.choice([None,1,2,3]),rng.choice([None,0,1]),rng.choice(dates),rng.choice([None,0,4,-1])] for i in range(80)]
    elif ident=='t11':
        ids=[-1,0,1,7,123,12345,99999,100000]
        if seed is not None:ids += rng.sample(range(8,99999),80)
        result={'events':[[i] for i in ids]}
    elif ident=='t12':
        result={'firsts':[[1,None],[1,None],[2,3],[3,0]],'seconds':[[1,None],[2,3],[4,-2]]}
        if seed is not None:
            for side in result:result[side] += [[rng.choice([1,2,3,4]),rng.choice([None,0,2,5])] for _ in range(80)]
    elif ident=='n01':result={'events':[[1,None],[2,None]]}
    elif ident=='n02':result={'events':[[1,10],[1,20],[2,30]]}
    else:raise ValueError(ident)
    if empty:return {name:[] for name in result}
    # Physical SQL input order must not supply accidental SAS ordering guarantees.
    if seed is not None:
        for rows in result.values():rng.shuffle(rows)
    return result


def archive(item):
    xml='<ProjectCollection EGVersion="synthetic"><Elements><Element Type="SAS.EG.ProjectElements.CodeTask"><Element><ID>CodeTask-main</ID><Label>'+item['id']+'</Label></Element><CodeTask><Embedded>True</Embedded></CodeTask></Element></Elements></ProjectCollection>'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,raw in [('project.xml',xml.encode('utf-16')),('CodeTask-main/code.sas',item['source'].encode())]:
            info=zipfile.ZipInfo(name,(2026,11,6,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,raw)
    return out.getvalue()


def freeze():
    from .oracles import expected
    target=ROOT/'frozen';target.mkdir(exist_ok=True)
    frozen=[]
    for c in cases():
        variants=[]
        for name,seed,empty in [('edge',None,False),('seed_41',41,False),('seed_97',97,False),('empty',None,True)]:
            data=dataset(c['id'],seed,empty)
            variants.append({'name':name,'inputs':data,'expected':None if c['negative'] else expected(c['id'],data)})
        raw=archive(c);(target/(c['id']+'.egp')).write_bytes(raw)
        (target/(c['id']+'.sas')).write_text(c['source'],encoding='utf-8')
        frozen.append({**c,'archive_sha256':hashlib.sha256(raw).hexdigest(),'variants':variants})
    payload=json.dumps(frozen,ensure_ascii=False,indent=2).encode()
    (target/'corpus.json').write_bytes(payload)
    manifest={'synthetic':True,'sas_runtime':'not_available','cases':len(frozen),
              'positive_cases':sum(not c['negative'] for c in frozen),'variants_per_case':4,
              'corpus_sha256':hashlib.sha256(payload).hexdigest(),
              'oracle_sha256':hashlib.sha256((ROOT/'oracles.py').read_bytes()).hexdigest()}
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest))


if __name__=='__main__':freeze()
