"""Independent Python reference specifications for the declared subset, not a SAS runtime.

Created before model requests. Do not import candidate SQL or model output here.
Character transport is unpadded VARCHAR; comparison ignores trailing ASCII spaces.
Numeric missing is ordinary missing only, represented by None.
"""
from collections import defaultdict
from datetime import datetime


def total(values):
    present=[v for v in values if v is not None]
    return sum(present) if present else None


def low(value):return (value is not None, value or 0)
def char(value):return (value or '').rstrip(' ')
def groups(rows,index=0):
    result=defaultdict(list)
    for row in rows:result[row[index]].append(row)
    return result


def expected(ident,data):
    if ident=='t01':
        return {'result':[[i,int(x is None),int(x is None or x<0),int(x is not None and x!=0),
                           total([x,y]),None if x is None or y is None else x+y] for i,x,y in data['events']]}
    if ident=='t02':
        return {'result':[[i,int(char(c)==''),int(char(c)=='ABC'),'run; quit; proc sql;'] for i,c in data['events']]}
    if ident=='t03':
        hi=sorted(data['events'],key=lambda r:(low(r[1]),r[2] is None,-(r[2] or 0),r[3]))
        lo=sorted(data['events'],key=lambda r:(low(r[1]),low(r[2]),r[3]))
        def first(rows):
            seen=set();out=[]
            for row in rows:
                if row[1] not in seen:out.append(row);seen.add(row[1])
            return out
        return {'hi':hi,'top':first(hi),'lo':lo,'bottom':first(lo)}
    if ident=='t04':
        lookup={(k,char(s)):bias for k,s,bias in data['rights']}
        assert len(lookup)==len(data['rights'])
        out=[]
        for i,k,s,amount in data['lefts']:
            bias=lookup.get((k,char(s)))
            out.append([i,amount,bias,total([amount,bias])])
        return {'l':data['lefts'],'r':data['rights'],'result':out}
    if ident in ('t05','t06'):
        left=groups(sorted(data['lefts'],key=lambda r:(low(r[0]),r[1])))
        right=groups(sorted(data['rights'],key=lambda r:(low(r[0]),r[1])))
        out=[]
        for key,ls in left.items():
            rs=right.get(key,[])
            if ident=='t05':
                for i in range(max(len(ls),len(rs))):
                    l=ls[min(i,len(ls)-1)];r=rs[min(i,len(rs)-1)] if rs else [key,None,None]
                    out.append([key,l[1],l[2],r[1],r[2]])
            else:
                pdv={'id':None,'age':None,'rvalue':None}
                for i in range(max(len(ls),len(rs))):
                    if i<len(ls):pdv.update(id=ls[i][1],age=ls[i][2])
                    if i<len(rs):pdv.update(age=rs[i][2],rvalue=rs[i][3])
                    out.append([key,pdv['id'],pdv['age'],pdv['rvalue']])
        return {'l':data['lefts'],'r':data['rights'],'result':out}
    if ident=='t07':
        rows=sorted(data['events'],key=lambda r:(low(r[1]),r[2]));sums=defaultdict(int);out=[]
        for i,g,seq,a in rows:
            sums[g]+=a or 0;out.append([i,g,seq,sums[g]])
        return {'ordered':rows,'result':out}
    if ident=='t08':
        rows=sorted(data['events'],key=lambda r:r[1]);queue=None;out=[]
        for i,seq,active,amount in rows:
            prev=None
            if active==1:prev,queue=queue,amount
            out.append([i,prev])
        return {'ordered':rows,'result':out}
    if ident=='t09':
        out=[]
        for i,x,y in data['events']:
            if x is None or x<0:continue
            x+=1
            if x<1:continue
            if x<3:y=10
            if y==10:y+=x
            out.append([i,x,y])
        return {'result':out}
    if ident=='t10':
        by=defaultdict(list)
        for i,g,active,ts,amount in data['events']:
            if active!=1 or ts is None or not '2024-02-28 00:00:00'<=ts<'2024-04-01 00:00:00':continue
            month=datetime.fromisoformat(ts).date().replace(day=1).isoformat();by[(g,month)].append(amount)
        return {'result':[[g,m,len(values),total(values)] for (g,m),values in by.items()]}
    if ident=='t11':return {'result':[[i,f'{i:05d}','ABC'] for (i,) in data['events'] if 0<=i<=99999]}
    if ident=='t12':return {'result':[[i,total([x,1])] for i,x in data['firsts']+data['seconds']]}
    raise ValueError('No executable reference for '+ident)


def validate_sentinels():
    assert total([None,None]) is None and total([None,0])==0
    assert char(' ABC  ')==' ABC' and char('\t')!=''
    assert expected('t01',{'events':[[1,None,None]]})['result']==[[1,1,1,0,None,None]]
    assert expected('t08',{'events':[[1,1,1,5],[2,2,0,99],[3,3,1,None],[4,4,1,7]]})['result']==[[1,None],[2,None],[3,5],[4,None]]
    merge={'lefts':[[1,1,11],[1,2,None]],'rights':[[1,1,8,100]]}
    assert expected('t06',merge)['result']==[[1,1,8,100],[1,2,None,100]]
    merge={'lefts':[[1,1,10]],'rights':[[1,1,100],[1,2,200]]}
    assert expected('t05',merge)['result']==[[1,1,10,1,100],[1,1,10,2,200]]
