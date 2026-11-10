"""Bounded scalar SAS rules. No execution, external lookups or guessed formats."""
import re
from sqlglot import exp

NAMES={'put','input','cats','catx','strip','trim','left','tranwrd','intnx',
       'coalesce','missing','mod','floor','index','prxmatch','length','lengthn','lengthc'}

def in_lists(tokens):
    result=list(tokens);changes=[]
    literal=lambda t:t.startswith(("'",'"')) or bool(re.fullmatch(r'\d+(?:\.\d+)?',t))
    for i,t in enumerate(tokens[:-1]):
        if t.lower()!='in' or tokens[i+1]!='(':continue
        end=i+2
        while end<len(tokens) and tokens[end]!=')':end+=1
        body=tokens[i+2:end]
        if not body or not all(literal(t) or t==',' for t in body):continue
        fixed=[]
        for token in body:
            if fixed and literal(token) and literal(fixed[-1]):fixed.append(',')
            fixed.append(token)
        changes.append((i+2,end,fixed))
    for start,end,fixed in reversed(changes):result[start:end]=fixed
    return result

def projection_attributes(tokens):
    """Extract display-only SELECT attributes, preserving their projection index."""
    result=[];attributes=[];depth=0;active=False;index=0;i=0
    if sum(t.lower()=='select' for t in tokens)!=1:return tokens,attributes
    while i<len(tokens):
        t=tokens[i];low=t.lower()
        if depth==0 and low=='select':active=True
        elif depth==0 and low=='from':active=False
        elif active and depth==0 and t==',':index+=1
        if active and depth==0 and low in {'format','label'} and i+1<len(tokens) and tokens[i+1]=='=':
            if low=='label':
                if i+2>=len(tokens) or not tokens[i+2].startswith(("'",'"')):raise ValueError('LABEL: нужна литеральная строка')
                v=tokens[i+2];value=v[1:-1].replace(v[0]*2,v[0]);end=i+3
                if v.startswith('"') and re.search(r'[%&]',value):raise ValueError('LABEL: не раскрыт макропараметр')
            else:
                end=i+2;parts=[]
                while end<len(tokens) and tokens[end] not in {'.',',',';'} and len(parts)<2:
                    parts.append(tokens[end]);end+=1
                if end>=len(tokens) or tokens[end]!='.':raise ValueError('FORMAT: нужен литеральный формат с точкой')
                parts.append('.');end+=1;value=''.join(parts)
                if not re.fullmatch(r'\$?[A-Za-z_]\w*\.|\d+\.',value):raise ValueError('FORMAT: этот вариант требует отдельного разбора')
            attributes.append({'projection':index,'attribute':low,'value':value});i=end;continue
        result.append(t)
        if t=='(':depth+=1
        elif t==')':depth-=1
        i+=1
    return result,attributes

def format_tokens(tokens):
    """Quote only PUT/INPUT's format argument; do not rewrite arbitrary dots."""
    result=list(tokens)
    replacements=[]
    for i,t in enumerate(tokens[:-1]):
        if t.lower() not in {'put','input'} or tokens[i+1]!='(':continue
        depth=1;commas=[];end=None
        for j in range(i+2,len(tokens)):
            if tokens[j]=='(':depth+=1
            elif tokens[j]==')':
                depth-=1
                if depth==0:end=j;break
            elif tokens[j]==',' and depth==1:commas.append(j)
        if end is None or len(commas)!=1:raise ValueError('PUT/INPUT: нужны значение и один литеральный формат')
        start=commas[0]+1
        fmt=''.join(tokens[start:end])
        if not re.fullmatch(r'(?:\$?[A-Za-z_]\w*|\d+)\.\d*',fmt):
            raise ValueError('PUT/INPUT: динамический формат или модификатор требует отдельного правила')
        replacements.append((start,end,"'"+fmt.lower()+"'"))
    for start,end,value in sorted(replacements,reverse=True):result[start:end]=[value]
    return result

def type_of(node,types):
    if node.meta.get('sas_type'):return node.meta['sas_type']
    if isinstance(node,exp.Paren):return type_of(node.this,types)
    if isinstance(node,exp.Literal):return 'character' if node.is_string else 'numeric'
    if isinstance(node,exp.Column):return types.get((node.table+'.'+node.name).casefold()) if node.table else types.get(node.name.casefold())
    if isinstance(node,(exp.Trim,exp.Substring,exp.Lower,exp.Upper,exp.Replace,exp.Concat,exp.TimeToStr)):return 'character'
    if isinstance(node,exp.Coalesce):return type_of(node.this,types)
    if isinstance(node,exp.Cast):
        target=node.args['to'].sql().upper()
        if target in {'TEXT','VARCHAR','CHAR'} or target.startswith(('VARCHAR(','CHAR(')):return 'character'
        if target=='DATE':return 'date'
    return None

def query_types(tree,catalog):
    """Only explicit contracts, unambiguous column bindings; no inferred types."""
    result={};bare={};domains={};bare_domains={};lengths={};bare_lengths={}
    for table in tree.find_all(exp.Table):
        key='.'.join(v for v in [table.db,table.name] if v).casefold()
        entry=catalog.get(key,{})
        if not isinstance(entry,dict):continue
        for col,typ in entry.get('column_types',{}).items():
            result[table.alias_or_name.casefold()+'.'+col.casefold()]=typ
            bare.setdefault(col.casefold(),[]).append(typ)
        for col,domain in entry.get('numeric_domains',{}).items():
            domains[table.alias_or_name.casefold()+'.'+col.casefold()]=domain
            bare_domains.setdefault(col.casefold(),[]).append(domain)
        for col,length in entry.get('column_lengths',{}).items():
            lengths[table.alias_or_name.casefold()+'.'+col.casefold()]=length
            bare_lengths.setdefault(col.casefold(),[]).append(length)
    tables=list(tree.find_all(exp.Table))
    complete=all(isinstance(catalog.get('.'.join(v for v in [t.db,t.name] if v).casefold()),dict) and catalog['.'.join(v for v in [t.db,t.name] if v).casefold()].get('columns') for t in tables)
    occurrences=lambda col: sum(col in [v.casefold() for v in catalog['.'.join(v for v in [t.db,t.name] if v).casefold()]['columns']] for t in tables) if complete else 0
    for col,values in bare.items():
        if len(values)==1 and complete and occurrences(col)==1:result[col]=values[0]
    for col,values in bare_domains.items():
        if len(values)==1 and col in result:domains[col]=values[0]
    for col,values in bare_lengths.items():
        if len(values)==1 and col in result:lengths[col]=values[0]
    result['__domains__']=domains
    result['__lengths__']=lengths
    # Nested queries have distinct scopes; no cross-scope type assumptions.
    return result if len(list(tree.find_all(exp.Select)))==1 else {}

def lower(name,args,rules,date_storage,types,formats=None,context=None):
    from .compiler import parse_one
    def sql(s):return parse_one('SELECT '+s).expressions[0]
    def text_arg(a):
        if type_of(a,types)!='character':
            raise ValueError(name.upper()+': укажите character в column_types; неявное числовое форматирование SAS не подменяется CAST')
        return a.sql()
    def clean(a):return 'TRIM(COALESCE('+text_arg(a)+", ''))"
    def numeric_arg(a):
        if type_of(a,types)!='numeric':raise ValueError(name.upper()+': укажите numeric в column_types')
        return a.sql()
    if name in {'strip','trim','left'} and len(args)==1:
        a=text_arg(args[0]);rules.add('sas.string.'+name)
        if name=='strip':return sql("TRIM(COALESCE("+a+", ''))")
        if name=='trim':return sql("COALESCE(NULLIF(RTRIM("+a+"), ''), ' ')")
        # LEFT shifts leading blanks to the end, preserving character length.
        return sql("RPAD(LTRIM(COALESCE("+a+", '')), LENGTH(COALESCE("+a+", '')), ' ')")
    if name in {'cats','catx'}:
        if not 1<=len(args)<=32 or (name=='catx' and len(args)<2):raise ValueError('CATS/CATX: неподдержанное число аргументов')
        values=args[1:] if name=='catx' else args
        pieces=[clean(a) for a in values]
        if name=='catx':
            delimiter=text_arg(args[0])
            # Do not use CONCAT_WS: SQLGlot's ClickHouse lowering inserts empty
            # arguments instead of skipping them, changing SAS separators.
            chunks=[]
            for i,piece in enumerate(pieces):
                if i:
                    prior=' OR '.join(p+" <> ''" for p in pieces[:i])
                    chunks.append("CASE WHEN "+piece+" <> '' AND ("+prior+") THEN COALESCE("+delimiter+", '') ELSE '' END")
                chunks.append(piece)
            body='CONCAT('+', '.join(chunks)+')' if len(chunks)>1 else chunks[0]
        else:body='CONCAT('+', '.join(pieces)+')' if len(pieces)>1 else pieces[0]
        rules.add('sas.string.'+name+'.character');return sql(body)
    if name=='tranwrd' and len(args)==3:
        a,b,c=[text_arg(v) for v in args]
        if not isinstance(args[1],exp.Literal) or not args[1].this:raise ValueError('TRANWRD: нужен непустой литеральный образец')
        rules.add('sas.string.tranwrd')
        return sql("REPLACE(COALESCE("+a+", ''), "+b+", COALESCE(NULLIF("+c+", ''), ' '))")
    if name=='coalesce' and 2<=len(args)<=16:
        kinds=[type_of(arg,types) for arg in args]
        if any(kind is None for kind in kinds) or len(set(kinds))!=1:
            raise ValueError('COALESCE: нужны одинаковые явные типы аргументов')
        rules.add('sas.coalesce.typed')
        return exp.Coalesce(this=args[0].copy(),expressions=[arg.copy() for arg in args[1:]])
    if name=='missing' and len(args)==1:
        kind=type_of(args[0],types);value=args[0].sql()
        if kind=='character':predicate="("+value+" IS NULL OR LENGTH(RTRIM("+value+")) = 0)"
        elif kind in {'numeric','date','datetime'}:predicate=value+" IS NULL"
        else:raise ValueError('MISSING: нужен явный тип аргумента')
        ancestor=getattr(context,'parent',None)
        while isinstance(ancestor,(exp.Paren,exp.Not,exp.And,exp.Or)):
            ancestor=ancestor.parent
        predicate_context=isinstance(ancestor,(exp.Where,exp.Having,exp.Join))
        replacement=sql(predicate if predicate_context else "CASE WHEN "+predicate+" THEN 1 ELSE 0 END")
        rules.add('sas.missing.typed');return replacement
    if name in {'mod','floor'}:
        if name=='floor' and len(args)==1:
            numeric_arg(args[0]);replacement=exp.Floor(this=args[0].copy())
        elif name=='mod' and len(args)==2:
            numeric_arg(args[0]);numeric_arg(args[1]);replacement=exp.Mod(this=args[0].copy(),expression=args[1].copy())
        else:raise ValueError(name.upper()+': неподдержанное число аргументов')
        rules.add('sas.'+name+'.numeric');return replacement
    if name=='index' and len(args)==2:
        source,needle=[text_arg(arg) for arg in args]
        rules.add('sas.index.character')
        return exp.StrPosition(this=parse_one('SELECT '+source).expressions[0],substr=parse_one('SELECT '+needle).expressions[0])
    if name in {'length','lengthn','lengthc'} and len(args)==1:
        value=text_arg(args[0]);key=(args[0].table+'.'+args[0].name if args[0].table else args[0].name).casefold() if isinstance(args[0],exp.Column) else ''
        if name=='lengthc':
            width=types.get('__lengths__',{}).get(key)
            if width is None:raise ValueError('LENGTHC: нужна column_lengths для байтовой длины SAS')
            replacement=exp.Literal.number(width)
        else:
            measured='LENGTH(RTRIM(COALESCE('+value+", '')))"
            replacement=sql(measured if name=='lengthn' else 'CASE WHEN '+measured+' = 0 THEN 1 ELSE '+measured+' END')
        rules.add('sas.'+name+'.character');return replacement
    if name=='prxmatch' and len(args)==2:
        if not isinstance(args[0],exp.Literal) or not args[0].is_string:raise ValueError('PRXMATCH: нужен литеральный шаблон')
        match=re.fullmatch(r'/(.*)/([ims]*)',args[0].this,re.S)
        if not match:raise ValueError('PRXMATCH: поддержана литеральная запись /pattern/ims')
        pattern,flags=match.groups()
        if len(pattern)>500 or re.search(r'\(\?|\\[1-9]|\\[pP]\{|\[\[:',pattern):raise ValueError('PRXMATCH: конструкция несовместима с общим regex-подмножеством целей')
        parent=getattr(context,'parent',None)
        if not isinstance(parent,(exp.GT,exp.GTE,exp.NEQ,exp.EQ,exp.LT,exp.LTE)) or not any(isinstance(v,exp.Literal) and not v.is_string and v.this=='0' for v in (parent.this,parent.expression)):
            raise ValueError('PRXMATCH: реализовано только сравнение результата с 0')
        text_arg(args[1]);prefix='(?'+flags+')' if flags else ''
        predicate=exp.RegexpLike(this=args[1].copy(),expression=exp.Literal.string(prefix+pattern))
        rules.add('sas.prxmatch.condition')
        return exp.Case(ifs=[exp.If(this=predicate,true=exp.Literal.number(1))],default=exp.Literal.number(0))
    if name=='put' and len(args)==2:
        fmt=args[1].this
        if fmt in (formats or {}):
            from .format_catalog import render_format
            rules.add('sas.put.custom_exact')
            return render_format(args[0],formats[fmt],types)
        width_match=re.fullmatch(r'(\d+)\.',fmt)
        if width_match:
            width=int(width_match[1]);arg=args[0]
            key=(arg.table+'.'+arg.name if arg.table else arg.name).casefold() if isinstance(arg,exp.Column) else ''
            domain=types.get('__domains__',{}).get(key)
            if isinstance(arg,exp.Literal) and not arg.is_string and re.fullmatch(r'-?\d+',arg.this):domain={'integer':True,'min':int(arg.this),'max':int(arg.this)}
            if not domain or type_of(arg,types)!='numeric' or not 1<=width<=32 or max(len(str(domain['min'])),len(str(domain['max'])))>width:
                raise ValueError('PUT w.: нужен numeric_domains с целочисленным диапазоном, помещающимся в ширину; иначе нужны правила округления/экспоненты')
            rules.add('sas.put.integer_width')
            result=sql("LPAD(COALESCE(CAST(CAST("+arg.sql()+" AS DECIMAL(38, 0)) AS TEXT), '.'), "+str(width)+", ' ')")
            result.meta.update(sas_type='character',sas_length=width)
            return result
        if fmt not in {'date9.','yymmn6.'}:raise ValueError('PUT '+fmt+': требуется определение формата или отдельное правило ширины/округления')
        if date_storage!='native':raise ValueError('PUT даты: нужен контракт native DATE/TIMESTAMP')
        # DATE9 uses English month names independent of DB session locale.
        a=args[0].sql();rules.add('sas.put.'+fmt[:-1])
        if fmt=='yymmn6.':body="TO_CHAR("+a+", 'YYYYMM')";width=6
        else:
            months=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
            month='CASE EXTRACT(MONTH FROM '+a+') '+ ' '.join('WHEN '+str(i+1)+" THEN '"+m+"'" for i,m in enumerate(months))+' END'
            body="CONCAT(TO_CHAR("+a+", 'DD'), "+month+", TO_CHAR("+a+", 'YYYY'))";width=9
        result=sql('CASE WHEN '+a+" IS NULL THEN '"+' '*(width-1)+".' ELSE "+body+' END')
        result.meta.update(sas_type='character',sas_length=width)
        return result
    if name=='input' and len(args)==2:
        fmt=args[1].this
        match=re.fullmatch(r'(\d+)\.(\d*)',fmt)
        if not match:raise ValueError('INPUT '+fmt+': нет правила этого informat')
        width=int(match[1]);decimals=int(match[2] or 0)
        if not 1<=width<=32 or decimals!=0:raise ValueError('INPUT: поддержан числовой w. шириной 1–32 без подразумеваемых десятичных разрядов')
        if not isinstance(args[0],exp.Literal) or not args[0].is_string:
            raise ValueError('INPUT w.: для колонки требуется политика ошибок/переполнения; реализованы проверяемые строковые литералы')
        import math
        value=args[0].this[:width].strip()
        if value in {'','.'}:return exp.Null()
        if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',value):
            raise ValueError('INPUT: литерал требует другого informat или обработки ошибки')
        number=float(value)
        if not math.isfinite(number):raise ValueError('INPUT: переполнение числового литерала')
        rules.add('sas.input.numeric_literal')
        return exp.Cast(this=exp.Literal.number(repr(number)),to=exp.DataType.build('DOUBLE PRECISION'))
    if name=='intnx' and len(args) in {3,4}:
        if date_storage!='native':raise ValueError('INTNX: нужен native DATE контракт')
        if not isinstance(args[0],exp.Literal) or not args[0].is_string:raise ValueError('INTNX: нужен литеральный интервал')
        unit=args[0].this.lower();align=args[3].this.lower() if len(args)==4 and isinstance(args[3],exp.Literal) and args[3].is_string else ('b' if len(args)==3 else '')
        align={'beginning':'b','end':'e','same':'s'}.get(align,align)
        if unit not in {'day','month','qtr','year'} or align not in {'b','e','s'}:raise ValueError('INTNX: поддержаны DAY/MONTH/QTR/YEAR и B/E/S без множителя/shift-index')
        step=args[2].sql()
        if not re.fullmatch(r'-?\d+',step):raise ValueError('INTNX: нужен целый литеральный сдвиг')
        count=int(step)
        if abs(count)>10000:raise ValueError('INTNX: сдвиг вне допустимого диапазона')
        source=args[1].sql();unit_sql='month' if unit=='qtr' else unit
        delta=count*(3 if unit=='qtr' else 1)
        moved=f"(CAST({source} AS DATE) + INTERVAL '{delta} {unit_sql}')"
        trunc='quarter' if unit=='qtr' else unit
        if align=='b':body=f"DATE_TRUNC('{trunc}', {moved})"
        elif align=='e':body=f"DATE_TRUNC('{trunc}', {moved}) + INTERVAL '{3 if unit=='qtr' else 1} {unit_sql}' - INTERVAL '1 day'"
        else:body=moved
        rules.add('sas.intnx.calendar.'+align);return sql('CAST('+body+' AS DATE)')
    raise ValueError(name.upper()+': этот вариант вызова пока не поддержан')
