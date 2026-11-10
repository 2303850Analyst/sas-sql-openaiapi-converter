"""SET concatenation and catalogue-bound prefixes, never multiple SET readers."""
import re
from sqlglot import exp
from .data_rules import dataset,TABLE,LEX

def sources(text,catalog):
    tail=re.sub(r'^\s*set\s+','',text,flags=re.I).rstrip().removesuffix(';')
    tokens=[m.group() for m in LEX.finditer(tail) if not m.group().isspace()]
    groups=[];i=0
    while i<len(tokens):
        start=i;i+=1
        if i<len(tokens) and tokens[i]=='.':i+=2
        name=''.join(tokens[start:i]);prefix=i<len(tokens) and tokens[i]==':'
        if prefix:i+=1
        option_start=i
        if i<len(tokens) and tokens[i]=='(':
            depth=1;i+=1
            while i<len(tokens) and depth:
                if tokens[i]=='(':depth+=1
                elif tokens[i]==')':depth-=1
                i+=1
            if depth:raise ValueError('SET: незакрытые опции набора')
        options=' '.join(tokens[option_start:i])
        if not re.fullmatch(TABLE,name):raise ValueError('SET: не раскрыто имя набора')
        members=sorted(k for k in catalog if k.startswith(name.casefold())) if prefix else [name]
        if not members:raise ValueError('SET prefix: нет совпадений в явном каталоге таблиц')
        if len(members)>200:raise ValueError('SET prefix: более 200 таблиц')
        for member in members:groups.append(dataset('set '+member+' '+options+';','set'))
    return groups

def compile_list(items,target,date_storage,catalog,contracts,formats):
    from .data_program import compile_program
    from tools.inspect_sources import mask_noncode
    sets=[i for i in items if re.match(r'^\s*set\b',i['masked'],re.I)]
    if len(sets)!=1:raise ValueError('Несколько SET statements: требуется синхронное чтение, не UNION ALL')
    members=sources(sets[0]['text'],catalog)
    if any(re.match(r'^\s*by\b',i['masked'],re.I) for i in items):raise ValueError('SET списка с BY: нужен merge-поток; UNION ALL недостаточен')
    fields=[];types={};lengths={};projections=[];domains={};unknown_domains=set()
    for source,options in members:
        key=source.casefold();entry=contracts.get(key)
        if not isinstance(entry,dict):raise ValueError('SET списка: нужен типизированный каталог каждого источника')
        cols=options.get('keep') or entry['columns']
        known={c.casefold():c for c in entry['columns']}
        if any(c.casefold() not in known for c in cols):raise ValueError('SET KEEP: неизвестное поле')
        excluded={c.casefold() for c in options.get('drop',[])}
        renames={k.casefold():v for k,v in options.get('rename',{}).items()}
        if any(k not in {c.casefold() for c in cols} or k in excluded for k in renames):raise ValueError('SET RENAME: исключённое или неизвестное поле')
        projection={}
        for col in cols:
            folded=col.casefold()
            if folded in excluded:continue
            new=renames.get(folded,folded);typ=entry.get('column_types',{}).get(folded)
            if typ is None:raise ValueError('SET списка: нужны column_types для всех сохраняемых полей')
            if new.casefold() in projection:raise ValueError('SET RENAME: конфликт имён')
            new=new.casefold()
            if new in types and types[new]!=typ:raise ValueError('SET списка: несовместимые типы одноимённых полей')
            if typ=='character':
                length=entry.get('column_lengths',{}).get(folded)
                if length is None:raise ValueError('SET списка: для строк нужны column_lengths в байтах SAS')
                if new in lengths and lengths[new]!=length:raise ValueError('SET списка: разные длины строк требуют правила усечения по первому набору')
                lengths[new]=length
            domain=entry.get("numeric_domains",{}).get(folded)
            if not domain:unknown_domains.add(new)
            elif new not in domains:domains[new]=dict(domain)
            else:
                domains[new]["min"]=min(domains[new]["min"],domain["min"])
                domains[new]["max"]=max(domains[new]["max"],domain["max"])
            types[new]=typ
            if new not in fields:fields.append(new)
            projection[new]=exp.column(known[folded],quoted=True)
        projections.append((source,projection))
    selects=[]
    for source,projection in projections:
        columns=[]
        for name in fields:
            if name in projection:value=projection[name]
            elif types[name]=='character':value=exp.Literal.string('')
            else:
                dtype={'numeric':'DOUBLE PRECISION','date':'DATE','datetime':'TIMESTAMP'}[types[name]]
                value=exp.Cast(this=exp.Null(),to=exp.DataType.build(dtype))
            columns.append(exp.alias_(value,name,quoted=True))
        selects.append(exp.select(*columns).from_(source))
    union=selects[0]
    for select in selects[1:]:union=exp.Union(this=union,expression=select,distinct=False)
    virtual='sas_migration_set_input'
    if virtual in catalog or any(re.search(r'\bsas_migration_set_input\b',i['masked'],re.I) for i in items):raise ValueError('SET: конфликт служебного имени')
    replaced=[]
    for item in items:
        if item is sets[0]:
            text='set '+virtual+';';replaced.append({**item,'text':text,'masked':mask_noncode(text,sas=True)})
        else:replaced.append(item)
    tree,rules=compile_program(replaced,target,date_storage,{**catalog,virtual:fields},
        {**contracts,virtual:{'columns':fields,'column_types':types,'column_lengths':lengths,'numeric_domains':{k:v for k,v in domains.items() if k not in unknown_domains}}},formats)
    cte=exp.CTE(this=union,alias=exp.TableAlias(this=exp.to_identifier(virtual)))
    tree.expression.args['with_'].expressions.insert(0,cte)
    rules=set(rules);rules.discard('data.set.single');rules.add('data.set.union_all')
    if ':' in sets[0]['masked']:rules.add('data.set.prefix_catalog')
    return tree,sorted(rules)
