"""Bag comparison over an agreed transport: no candidate-dependent normalization."""
from datetime import date,datetime,timedelta
from decimal import Decimal,InvalidOperation


def family(data_type):
    t=data_type.upper()
    if t.startswith(('INT','BIGINT','SMALLINT','NUMERIC','DECIMAL','DOUBLE','FLOAT','REAL','UINT')):return 'numeric'
    if t.startswith(('VARCHAR','CHAR','TEXT','STRING')):return 'text'
    if t.startswith(('TIMESTAMP','DATETIME')):return 'timestamp'
    if t.startswith('DATE'):return 'date'
    if t in {'BOOLEAN','BOOL'}:return 'boolean'
    return t


def value(v,kind):
    if kind=='text':return '' if v is None else str(v).rstrip(' ')
    if v is None or v=='':return None
    if kind=='numeric':
        if isinstance(v,str) and v.strip()=='.':return None
        return Decimal(str(int(v) if isinstance(v,bool) else v))
    if kind in {'date','timestamp'}:
        if isinstance(v,(date,datetime)):return v.isoformat(sep=' ') if isinstance(v,datetime) else v.isoformat()
        s=str(v).replace('T',' ')
        if kind=='date' and len(s)>=10 and s[4]=='-':return s[:10]
        if kind=='timestamp':
            try:return datetime.fromisoformat(s).isoformat(sep=' ')
            except ValueError:pass
        try:
            n=Decimal(s);dt=datetime(1960,1,1)+timedelta(days=float(n)) if kind=='date' else datetime(1960,1,1)+timedelta(seconds=float(n))
            return dt.date().isoformat() if kind=='date' else dt.isoformat(sep=' ')
        except (InvalidOperation,ValueError,OverflowError):return s
    return v


def compare(columns,expected,actual):
    kinds=[family(c['data_type']) for c in columns]
    approximate=[c['data_type'].upper().startswith(('DOUBLE','FLOAT','REAL')) for c in columns]
    if any(len(row)!=len(columns) for rows in (expected,actual) for row in rows):
        return {'passed':False,'error':'row_width_mismatch','expected_rows':len(expected),'actual_rows':len(actual)}
    try:
        wanted=[[value(v,k) for v,k in zip(row,kinds)] for row in expected]
        found=[[value(v,k) for v,k in zip(row,kinds)] for row in actual]
    except (InvalidOperation,ValueError,TypeError) as error:
        return {'passed':False,'error':'type/value conversion: '+type(error).__name__,'expected_rows':len(expected),'actual_rows':len(actual)}
    extras=list(found);missing=[]
    def same(a,b):
        if len(a)!=len(b):return False
        for x,y,tolerant in zip(a,b,approximate):
            if x==y:continue
            if tolerant and x is not None and y is not None and abs(x-y)<=Decimal('0.00000001'):continue
            return False
        return True
    for row in wanted:
        index=next((i for i,r in enumerate(extras) if same(row,r)),None)
        if index is None:missing.append(row)
        else:extras.pop(index)
    return {'passed':not missing and not extras,'expected_rows':len(wanted),'actual_rows':len(found),
        'missing_rows':len(missing),'extra_rows':len(extras),'counterexample':{'missing':missing[:2],'extra':extras[:2]},
        'numeric_absolute_tolerance':'0.00000001','text_transport':'unpadded ASCII trailing spaces; blank character missing canonicalized'}
