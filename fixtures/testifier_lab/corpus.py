"""Constructed SAS/data and independent references. No model SQL is imported here."""
from collections import defaultdict
from datetime import date,datetime,timedelta
from decimal import Decimal
import calendar
import copy
import hashlib
import io
import json
from pathlib import Path
import random
import zipfile
from fixtures.semantic_eval.corpus import cases as old_cases,dataset as old_data,cols,table,N,I,K,C
from fixtures.semantic_eval.oracles import expected as old_expected,total,char

ROOT=Path(__file__).resolve().parent


def extra_cases():
    e=cols(id=I,x=N,y=N,z=N,g=K)
    def c(i,title,source,outputs,inputs=None,negative=False):
        return {'id':i,'title':title,'source':source,'inputs':inputs or {'events':table(e,[['id']])},'outputs':outputs,'negative':negative}
    return [
      c('e13','Multiple explicit OUTPUT routes, missing and DELETE',
        'data work.positive work.other; set source.events; if x>0 then output work.positive; else output work.other; run;',{'positive':e,'other':e}),
      c('e14','ARRAY + SUM + N + NMISS with all missing',
        'data work.result(keep=id total present absent); set source.events; array a[3] x y z; total=sum(of a[*]); present=n(of a[*]); absent=nmiss(of a[*]); run;',{'result':cols(id=I,total=N,present=I,absent=I)}),
      c('e15','PROC FORMAT and PUT, including ordinary numeric missing',
        "proc format; value bands .='missing' low-<0='negative' 0='zero' 0<-high='positive'; run; data work.result(keep=id bucket); length bucket $8; set source.events; bucket=put(x,bands.); run;",{'result':cols(id=I,bucket=C)}),
      c('e16','PROC SQL GROUP BY, HAVING, COUNT DISTINCT, AVG and missing',
        'proc sql; create table work.result as select g,count(*) as n,count(x) as present,count(distinct x) as distinct_x,sum(x) as total,avg(x) as average from source.events group by g having count(*)>=1; quit;',{'result':cols(g=K,n=I,present=I,distinct_x=I,total=N,average=N)}),
      c('e17','PROC SUMMARY NWAY MISSING and multiple statistics',
        'proc summary data=source.events nway missing; class g; var x; output out=work.result(drop=_type_ _freq_) sum=total mean=average n=present; run;',{'result':cols(g=K,total=N,average=N,present=I)}),
      c('e18','INPUT COMMA informat and suppressed invalid conversion',
        'data work.result(keep=id amount); set source.events; amount=input(code,?? comma12.2); run;',{'result':cols(id=I,amount=N)}, {'events':table(cols(id=I,code=C),[['id']])}),
      c('e19','Macro DO unrolling and concatenation of every intermediate table',
        '%macro build; %do i=1 %to 3; data work.t&i.; set source.events; value=x*&i.; keep id value; run; %end; data work.result; set work.t1 work.t2 work.t3; run; %mend; %build;', {n:cols(id=I,value=N) for n in ('t1','t2','t3','result')}),
      c('e20','DATA then APPEND FORCE with a missing source column',
        'data work.result; set source.firsts; run; proc append base=work.result data=source.seconds force; run;',{'result':cols(id=I,x=N)},
        {'firsts':table(cols(id=I,x=N)),'seconds':table(cols(id=I,y=N))}),
      c('e21','Date arithmetic across leap days and month ends',
        "data work.result(keep=id month_end next_end months); set source.events; month_end=intnx('month',d,0,'e'); next_end=intnx('month',d,1,'e'); months=intck('month','01JAN2024'd,d); format month_end next_end yymmdd10.; run;",{'result':cols(id=I,month_end=('DATE',True),next_end=('DATE',True),months=K)},
        {'events':table(cols(id=I,d=('DATE',True)),[['id']])}),
      c('e22','CATX skips blanks and preserves nonblank tabs/NBSP',
        "data work.result(keep=id joined upper_code); length joined $90 upper_code $40; set source.events; joined=catx('|',a,b); upper_code=upcase(a); run;",{'result':cols(id=I,joined=('VARCHAR(90)',True),upper_code=C)},
        {'events':table(cols(id=I,a=C,b=C),[['id']])}),
      c('e23','SELECT INTO computed parameter and macro IF with empty alternate',
        '%macro select_positive; proc sql noprint; select count(*) into :n from source.events where x>0; quit; %if &n>0 %then %do; data work.result; set source.events; if x>0; run; %end; %else %do; data work.result; set source.events(obs=0); run; %end; %mend; %select_positive;',{'result':e}),
      c('n03','Unbounded TRANSPOSE ID domain requires an output-domain contract',
        'proc transpose data=source.events out=work.result; by g; id category; var x; run;',{'result':cols(g=K,q1=N,q2=N)},
        {'events':table(cols(g=K,category=C,x=N))},True),
      c('n04','Unknown environment value may not be guessed',
        '%let cutoff=%sysget(MISSING_SYNTHETIC_INPUT); data work.result; set source.events; if x>&cutoff.; run;',{'result':e},negative=True),
    ]


def flow():
    sales=cols(id=I,customer_id=K,g=K,observed_at=('TIMESTAMP',True),amount=N)
    customers=cols(customer_id=I,segment=C)
    enriched=sales+cols(segment=C)
    sources={
      'A': '%let floor=0; %macro clean; data work.cleaned work.rejected; set source.sales; if missing(customer_id) or missing(amount) or amount<&floor then output work.rejected; else output work.cleaned; run; %mend; %clean;',
      'B': 'proc sql; create table work.enriched as select a.id,a.customer_id,a.g,a.observed_at,a.amount,b.segment from work.cleaned a left join source.customers b on a.customer_id=b.customer_id; quit;',
      'C': "%macro aggregate; proc sql; create table mart.flow_monthly as select intnx('month',datepart(observed_at),0,'b') as month format=yymmdd10.,segment,count(*) as n,sum(amount) as total from work.enriched group by calculated month,segment; quit; %mend; %aggregate;",
      'D': "data mart.flow_flags; set work.enriched; if amount>=100 then band='high'; else band='low'; keep id customer_id segment band; run;"}
    return {'id':'flow','title':'Whole multi-program sales ETL: macro, DQ routes, JOIN, date aggregation and flags',
        'sources':sources,'source':'\n'.join(sources.values()),'inputs':{'sales':table(sales,[['id']]),'customers':table(customers,[['customer_id']])},
        'outputs':{'cleaned':sales,'rejected':sales,'enriched':enriched,'monthly':cols(month=('DATE',True),segment=C,n=I,total=N),'flags':cols(id=I,customer_id=I,segment=C,band=C)},'negative':False}


def data_for(c,seed=None,empty=False):
    ident=c['id'];rng=random.Random(seed)
    if ident.startswith(('t','n0')) and ident not in {'n03','n04'}:return old_data(ident,seed,empty)
    if ident=='flow':
        result={'customers':[[1,'alpha'],[2,'beta'],[3,'alpha']],
            'sales':[[1,1,1,'2024-02-29 12:00:00',100],[2,2,None,'2024-03-01 00:00:00',5],
                [3,None,1,'2024-03-31 23:59:59',9],[4,1,1,None,0],[5,3,2,'2024-03-31 23:59:59',None],
                [6,99,2,'2024-04-01 00:00:00',20],[7,3,2,'2024-03-31 23:59:59',-1]]}
        if seed is not None:
            result['sales'] += [[8+i,rng.choice([None,1,2,3,99]),rng.choice([None,1,2]),rng.choice([None,'2024-02-29 12:00:00','2024-03-31 23:59:59','2024-04-01 00:00:00']),rng.choice([None,-5,0,5,100,125])] for i in range(70)]
    elif ident=='e18':
        values=[None,'',' ','1,234.56','-4.50','BAD','0','999.99']
        if seed is not None:values += [rng.choice(values) for _ in range(70)]
        result={'events':[[i+1,v] for i,v in enumerate(values)]}
    elif ident=='e20':result={'firsts':[[1,None],[2,5],[2,5]],'seconds':[[3,99],[4,None]]}
    elif ident=='e21':
        values=[None,'2023-12-31','2024-01-31','2024-02-29','2024-03-01','2024-04-30']
        if seed is not None:values += [(date(2024,1,1)+timedelta(days=rng.randrange(365))).isoformat() for _ in range(70)]
        result={'events':[[i+1,v] for i,v in enumerate(values)]}
    elif ident=='e22':
        values=[None,'','  ',' a ','ABC   ',' leading','\t','\u00a0','run; quit;']
        result={'events':[[i+1,a,b] for i,(a,b) in enumerate([(a,values[-i-1]) for i,a in enumerate(values)])]}
        if seed is not None:result['events'] += [[10+i,rng.choice(values),rng.choice(values)] for i in range(70)]
    elif ident=='n03':result={'events':[[1,'q1',3],[1,'q9',5]]}
    else:
        result={'events':[[1,None,None,None,None],[2,-1,5,None,1],[3,0,0,0,1],[4,1,2,3,2],[5,2,None,4,2]]}
        if seed is not None:result['events'] += [[6+i,*[rng.choice([None,-3,0,2,8]) for _ in range(3)],rng.choice([None,1,2,3])] for i in range(70)]
    if empty:return {k:[] for k in result}
    if seed is not None:
        for rows in result.values():rng.shuffle(rows)
    return result


def expected(c,data):
    ident=c['id']
    if ident.startswith('t'):return old_expected(ident,data)
    rows=data.get('events',[])
    if ident=='e13':return {'positive':[r for r in rows if r[1] is not None and r[1]>0],'other':[r for r in rows if r[1] is None or r[1]<=0]}
    if ident=='e14':return {'result':[[r[0],total(r[1:4]),sum(x is not None for x in r[1:4]),sum(x is None for x in r[1:4])] for r in rows]}
    if ident=='e15':return {'result':[[r[0],'missing' if r[1] is None else ('negative' if r[1]<0 else ('zero' if r[1]==0 else 'positive'))] for r in rows]}
    if ident in {'e16','e17'}:
        groups=defaultdict(list)
        for r in rows:groups[r[4]].append(r[1])
        out=[]
        for g,values in groups.items():
            present=[Decimal(str(v)) for v in values if v is not None];summed=sum(present) if present else None
            average=summed/len(present) if present else None
            out.append([g,len(values),len(present),len(set(present)),summed,average] if ident=='e16' else [g,summed,average,len(present)])
        return {'result':out}
    if ident=='e18':
        out=[]
        for i,v in rows:
            try:amount=Decimal(v.replace(',','').strip()) if v and v.strip() else None
            except Exception:amount=None
            out.append([i,amount])
        return {'result':out}
    if ident=='e19':
        out={f't{i}':[[r[0],None if r[1] is None else r[1]*i] for r in rows] for i in (1,2,3)}
        out['result']=out['t1']+out['t2']+out['t3'];return out
    if ident=='e20':return {'result':data['firsts']+[[i,None] for i,_ in data['seconds']]}
    if ident=='e21':
        out=[]
        for i,d in rows:
            if d is None:out.append([i,None,None,None]);continue
            day=date.fromisoformat(d);following=date(day.year+(day.month==12),day.month%12+1,1)
            out.append([i,day.replace(day=calendar.monthrange(day.year,day.month)[1]).isoformat(),following.replace(day=calendar.monthrange(following.year,following.month)[1]).isoformat(),(day.year-2024)*12+day.month-1])
        return {'result':out}
    if ident=='e22':return {'result':[[i,'|'.join(v.strip(' ') for v in (a or '',b or '') if v.strip(' ')),(a or '').upper()] for i,a,b in rows]}
    if ident=='e23':return {'result':[r for r in rows if r[1] is not None and r[1]>0]}
    if ident=='flow':
        cleaned=[];rejected=[];enriched=[];lookup={i:s for i,s in data['customers']}
        for row in data['sales']:
            if row[1] is None or row[4] is None or row[4]<0:rejected.append(row)
            else:cleaned.append(row);enriched.append(row+[lookup.get(row[1])])
        groups=defaultdict(list)
        for row in enriched:groups[(row[3][:7]+'-01' if row[3] else None,row[5])].append(row[4])
        return {'cleaned':cleaned,'rejected':rejected,'enriched':enriched,
            'monthly':[[m,s,len(values),sum(values)] for (m,s),values in groups.items()],
            'flags':[[r[0],r[1],r[5],'high' if r[4]>=100 else 'low'] for r in enriched]}
    raise ValueError(ident)


def archive(c):
    sources=c.get('sources') or {'P1':c['source']}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('project.xml','<ProjectCollection><Elements>'+''.join(f'<Element Type="SAS.EG.ProjectElements.CodeTask"><Element><ID>{i}</ID><Label>{i}</Label></Element></Element>' for i in sources)+'</Elements></ProjectCollection>')
        for i,source in sources.items():z.writestr(i+'/code.sas',source)
    return out.getvalue()


def freeze():
    out=ROOT/'frozen';out.mkdir(exist_ok=True)
    all_cases=old_cases()+extra_cases()+[flow()]
    for c in all_cases:
        intermediate={'l','r','hi','lo','ordered','t1','t2','t3','cleaned','rejected','enriched'}
        c['finals']=[n for n in c['outputs'] if n not in intermediate]
        c['output_sources']={n:f'mart.{c["id"]}_{n}' if n in c['finals'] else 'work.'+n for n in c['outputs']}
        import re
        for n in c['finals']:
            for field in ('source',):c[field]=re.sub(r'\bwork\.'+re.escape(n)+r'\b',c['output_sources'][n],c[field])
            if c.get('sources'):c['sources']={k:re.sub(r'\bmart\.flow_'+re.escape(n)+r'\b|\bwork\.'+re.escape(n)+r'\b',c['output_sources'][n],v) for k,v in c['sources'].items()}
        c['variants']=[]
        for name,seed,empty in [('edge',None,False),('seed_41',41,False),('seed_97',97,False),('empty',None,True)]:
            data=data_for(c,seed,empty);reference=None if c['negative'] else expected(c,data)
            c['variants'].append({'name':name,'inputs':data,'expected':reference})
        raw=archive(c);(out/(c['id']+'.egp')).write_bytes(raw);c['archive_sha256']=hashlib.sha256(raw).hexdigest()
        (out/(c['id']+'.sas')).write_text(c['source'],encoding='utf-8')
    payload=json.dumps(all_cases,ensure_ascii=False,indent=2,default=str).encode();(out/'corpus.json').write_bytes(payload)
    manifest={'synthetic_only':True,'cases':len(all_cases),'positive':sum(not c['negative'] for c in all_cases),'variants':4,
        'corpus_sha256':hashlib.sha256(payload).hexdigest(),'oracle_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8');print(json.dumps(manifest))


if __name__=='__main__':freeze()
