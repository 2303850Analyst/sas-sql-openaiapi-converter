"""Independent specifications for new composed code; not native SAS output."""
from collections import defaultdict


def expected(ident,data):
    if ident=='h01':
        before,after=[],[]
        for i,amount,flag in data['events']:
            if amount is None or amount<0:before.append([i,amount,1])
            updated=(amount or 0)+2
            if flag==1:after.append([i,updated,2])
        return {'before':before,'after':after}
    if ident=='h02':
        selected=[];groups=defaultdict(list)
        for i,g,amount in data['events']:
            value=None if amount is None else amount*3
            selected.append([i,g,amount,value]);groups[g].append(value)
        summary=[]
        for g,values in groups.items():
            present=[v for v in values if v is not None]
            summary.append([g,len(values),sum(present) if present else None])
        return {'selected':selected,'result':summary}
    raise ValueError(ident)
