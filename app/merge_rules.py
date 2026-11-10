"""Two-stream SAS match merge with disjoint payloads and known input order.

Repeat the final observation of an exhausted stream within each BY group.
This deliberately does not produce the SQL many-to-many Cartesian product.
"""
import re
from sqlglot import exp
from .data_rules import TABLE
from .procedure_rules import by_fields

def compile_merge(items,target,date_storage,catalog,contracts,formats):
    from .data_program import compile_program
    from tools.inspect_sources import mask_noncode
    merges=[i for i in items if re.match(r'^\s*merge\b',i['masked'],re.I)]
    bys=[i for i in items if re.match(r'^\s*by\b',i['masked'],re.I)]
    if len(merges)!=1 or len(bys)!=1 or any(re.match(r'^\s*set\b',i['masked'],re.I) for i in items):raise ValueError('MERGE: нужен один MERGE, один BY и отсутствие SET')
    if any(re.search(r'\b(?:first|last)\.',i['masked'],re.I) for i in items):raise ValueError('MERGE с FIRST/LAST: требуется отдельная передача порядка выходных групп')
    by=by_fields(bys[0]['text'].strip()[2:]);keys=[n for n,_ in by]
    token=re.compile(r'\s*('+TABLE+r')(?:\s*\(\s*in\s*=\s*([A-Za-z_]\w*)\s*\))?',re.I)
    tail=re.sub(r'^\s*merge\s+','',merges[0]['text'],flags=re.I).rstrip().removesuffix(';')
    pos=0;sources=[]
    while pos<len(tail) and tail[pos:].strip():
        match=token.match(tail,pos)
        if not match:raise ValueError('MERGE: поддержаны явные таблицы и IN=; остальные dataset options требуют отдельного правила')
        sources.append((match[1],match[2]));pos=match.end()
    if len(sources)!=2:raise ValueError('MERGE: текущий алгоритм поддерживает два входных потока')
    types={};lengths={};columns=list(keys);ctes=[];payloads=[];flags=[]
    for index,(source,flag) in enumerate(sources):
        entry=contracts.get(source.casefold(),{})
        if not isinstance(entry,dict) or not entry.get('unique_order') or not entry.get('order_by'):raise ValueError('MERGE: для каждого входа нужен полный уникальный order_by')
        cols=catalog.get(source.casefold());declared=entry.get('column_types',{})
        if not cols or any(k.casefold() not in {c.casefold() for c in cols} for k in keys):raise ValueError('MERGE: нужны входные колонки и все BY-ключи')
        folded={c.casefold():c for c in cols}
        for col in cols:
            key=col.casefold();typ=declared.get(key)
            if typ is None:raise ValueError('MERGE: нужны типы всех входных полей')
            if key in types and (key not in {k.casefold() for k in keys} or types[key]!=typ):raise ValueError('MERGE: общие неключевые поля или несовместимые типы требуют отдельной модели перезаписи PDV')
            types[key]=typ
            if typ=='character':
                length=entry.get('column_lengths',{}).get(key)
                if length is None or (key in lengths and lengths[key]!=length):raise ValueError('MERGE: нужны согласованные длины строковых полей')
                lengths[key]=length
        if any(c.casefold().startswith('sas_merge_') for c in cols):raise ValueError('MERGE: конфликт служебных имён')
        alias=f'sas_merge_source_{index}'
        order=[exp.Ordered(this=exp.column(folded[v.lstrip('-').casefold()],quoted=True),desc=v.startswith('-'),nulls_first=not v.startswith('-')) for v in entry['order_by']]
        partition=[exp.column(folded[k.casefold()],quoted=True) for k in keys]
        row=exp.Window(this=exp.RowNumber(),partition_by=partition,order=exp.Order(expressions=order))
        count=exp.Window(this=exp.Count(this=exp.Star()),partition_by=[v.copy() for v in partition])
        key_names={k.casefold():k for k in keys}
        query=exp.select(*[exp.alias_(exp.column(c,quoted=True),key_names[c.casefold()],quoted=True) if c.casefold() in key_names else exp.column(c,quoted=True) for c in cols],exp.alias_(row,'sas_merge_rn',quoted=True),exp.alias_(count,'sas_merge_count',quoted=True)).from_(source)
        ctes.append(exp.CTE(this=query,alias=exp.TableAlias(this=exp.to_identifier(alias))))
        payload=[c for c in cols if c.casefold() not in {k.casefold() for k in keys}]
        columns.extend(payload);payloads.append(payload);flags.append(flag.casefold() if flag else None)
    if len({c.casefold() for c in columns})!=len(columns):raise ValueError('MERGE: конфликт имён выходных полей')
    if len([v for v in flags if v])!=len(set(v for v in flags if v)) or any(v in {c.casefold() for c in columns} for v in flags if v):raise ValueError('MERGE: конфликт IN-флагов')
    grids=[exp.select(*[exp.column(k,quoted=True) for k in keys],exp.column('sas_merge_rn',quoted=True)).from_(f'sas_merge_source_{i}') for i in range(2)]
    grid=exp.Union(this=grids[0],expression=grids[1],distinct=True)
    ctes.append(exp.CTE(this=grid,alias=exp.TableAlias(this=exp.to_identifier('sas_merge_grid'))))
    projections=[exp.column(k,table='g',quoted=True) for k in keys]
    query=exp.select().from_('sas_merge_grid g')
    for index,payload in enumerate(payloads):
        alias=f'm{index}';rn=exp.column('sas_merge_rn',table=alias,quoted=True);count=exp.column('sas_merge_count',table=alias,quoted=True);ordinal=exp.column('sas_merge_rn',table='g',quoted=True)
        match=exp.or_(exp.EQ(this=rn.copy(),expression=ordinal.copy()),exp.and_(exp.EQ(this=rn.copy(),expression=count.copy()),exp.GT(this=ordinal.copy(),expression=count.copy())))
        key_conditions=[exp.NullSafeEQ(this=exp.column(k,table='g',quoted=True),expression=exp.column(k,table=alias,quoted=True)) for k in keys]
        query=query.join(f'sas_merge_source_{index} {alias}',on=exp.and_(*key_conditions,match),join_type='LEFT')
        for c in payload:
            value=exp.column(c,table=alias,quoted=True)
            if types[c.casefold()]=='character':value=exp.Coalesce(this=value,expressions=[exp.Literal.string('')])
            projections.append(exp.alias_(value,c,quoted=True))
        if flags[index]:
            flag=flags[index];columns.append(flag);types[flag]='numeric'
            value=exp.Case(ifs=[exp.If(this=exp.Is(this=rn.copy(),expression=exp.Null()),true=exp.Literal.number(0))],default=exp.Literal.number(1))
            projections.append(exp.alias_(value,flag,quoted=True))
    query.set('expressions',projections)
    virtual='sas_migration_merge_input'
    if any(re.search(r'\bsas_migration_merge_input\b',i['masked'],re.I) for i in items):raise ValueError('MERGE: конфликт служебного имени')
    ctes.append(exp.CTE(this=query,alias=exp.TableAlias(this=exp.to_identifier(virtual))))
    replaced=[]
    for item in items:
        if item is bys[0]:continue
        if item is merges[0]:
            text='set '+virtual+';';replaced.append({**item,'text':text,'masked':mask_noncode(text,sas=True)})
        else:replaced.append(item)
    tree,rules=compile_program(replaced,target,date_storage,{**catalog,virtual:columns},{**contracts,virtual:{'columns':columns,'column_types':types,'column_lengths':lengths}},formats)
    tree.expression.args['with_'].set('expressions',ctes+tree.expression.args['with_'].expressions)
    # IN flags are temporary SAS variables and not written unless explicitly
    # assigned by the program (that advanced case is rejected here).
    for flag in [f for f in flags if f]:
        if any(re.match(r'\s*'+re.escape(flag)+r'\s*=',i['masked'],re.I) for i in items):raise ValueError('MERGE: присваивание IN-флагу требует отдельного правила')
    tree.expression.set('expressions',[e for e in tree.expression.expressions if e.alias_or_name.casefold() not in flags and not (isinstance(e,exp.Alias) and isinstance(e.this,exp.Column) and e.this.name.casefold() in flags)])
    rules=set(rules);rules.discard('data.set.single');rules.add('data.merge.positional_two_streams')
    return tree,{},False,sorted(rules)
