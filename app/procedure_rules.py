"""Whole-procedure transformations with explicit ordering prerequisites."""
import re
from sqlglot import exp
from .data_rules import TABLE,LEX,names

def by_fields(text):
    tokens=[m.group() for m in LEX.finditer(text.rstrip(';')) if not m.group().isspace()]
    result=[];descending=False
    for token in tokens:
        if token.lower()=='descending':
            if descending:raise ValueError('Повтор DESCENDING')
            descending=True
        else:
            name=names([token])[0]
            if name in {'notsorted','groupformat'}:raise ValueError('Этот вариант BY требует отдельного правила')
            result.append((name,descending));descending=False
    if not result or descending or len({n.casefold() for n,_ in result})!=len(result):raise ValueError('Некорректный список BY')
    return result

def compile_sort(items,catalog,contracts):
    from .compiler import parse_one
    text=items[0]['text'].strip()
    match=re.fullmatch(r'proc\s+sort\s+(.+);',text,re.I|re.S)
    if not match:raise ValueError('SORT: нужны параметры DATA/OUT')
    options=match[1];values={}
    for m in re.finditer(r'\b(data|out)\s*=\s*('+TABLE+r')\b',options,re.I):
        if m[1].lower() in values:raise ValueError('SORT: повтор параметра')
        values[m[1].lower()]=m[2]
    rest=re.sub(r'\b(data|out)\s*=\s*('+TABLE+r')\b',' ',options,flags=re.I).split()
    if any(t.lower() not in {'nodupkey','equals'} for t in rest) or len(rest)!=len(set(t.lower() for t in rest)):
        raise ValueError('SORT: поддержаны DATA/OUT, EQUALS и NODUPKEY; прочие опции требуют отдельного правила')
    if 'data' not in values:raise ValueError('SORT: нужен явный DATA')
    source=values['data'];output=values.get('out',source);by=None
    for item in items[1:]:
        mask=item['masked'].strip()
        if re.fullmatch(r'(?:run|quit|%quitrun)\s*;',mask,re.I):continue
        if re.match(r'by\b',mask,re.I) and by is None:by=by_fields(item['text'].strip()[2:])
        else:raise ValueError('SORT: поддержан один BY без дополнительных statements')
    if not by:raise ValueError('SORT: отсутствует BY')
    ordering=[exp.Ordered(this=exp.column(n,quoted=True),desc=d,nulls_first=not d) for n,d in by]
    rules=['proc.sort.order_contract']
    if 'nodupkey' not in {t.lower() for t in rest}:
        query=exp.select('*').from_(source);query.set('order',exp.Order(expressions=ordering))
    else:
        entry=contracts.get(source.casefold(),{})
        cols=catalog.get(source.casefold())
        if not isinstance(entry,dict) or not cols or not entry.get('unique_order') or not entry.get('order_by'):
            raise ValueError('SORT NODUPKEY: нужны полный каталог и уникальный исходный order_by для выбора первой строки')
        folded={c.casefold():c for c in cols}
        if any(n.casefold() not in folded for n,_ in by):raise ValueError('SORT: неизвестное поле BY')
        if '__sas_sort_rank' in folded:raise ValueError('SORT: конфликт служебного поля')
        source_order=[exp.Ordered(this=exp.column(folded[n.lstrip('-').casefold()],quoted=True),desc=n.startswith('-'),nulls_first=not n.startswith('-')) for n in entry['order_by']]
        rank=exp.Window(this=exp.RowNumber(),partition_by=[exp.column(n,quoted=True) for n,_ in by],order=exp.Order(expressions=source_order))
        inner=exp.select(*[exp.column(c,quoted=True) for c in cols],exp.alias_(rank,'__sas_sort_rank',quoted=True)).from_(source)
        query=exp.select(*[exp.column(c,quoted=True) for c in cols]).from_(inner.subquery('sas_sorted')).where(exp.EQ(this=exp.column('__sas_sort_rank',quoted=True),expression=exp.Literal.number(1)))
        query.set('order',exp.Order(expressions=ordering));rules.append('proc.sort.nodupkey.first')
    tree=exp.Create(this=parse_one('select * from '+output).args['from_'].this,kind='TABLE',expression=query)
    tree.meta['order_contract']={'by':[('-' if d else '')+n for n,d in by], 'physical_order_guaranteed':False}
    return tree,{},False,rules
