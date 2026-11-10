"""SAS sum statement with literal initial/reset value and explicit row order."""
import re
from sqlglot import exp
from .data_rules import NAME,dataset,identifier
from .procedure_rules import by_fields

def compile_accumulator(items,target,date_storage,catalog,contracts,formats):
    from .compiler import parse_one
    from .data_program import scalar,truth,compile_program
    from tools.inspect_sources import mask_noncode
    sum_items=[i for i in items[1:] if re.match(r'^\s*'+NAME+r'\s*\+',i['text'],re.I)]
    if len(sum_items)!=1:raise ValueError('Накопитель: поддержан один sum statement')
    m=re.fullmatch(r'\s*('+NAME+r')\s*\+\s*(.+);',sum_items[0]['text'],re.I|re.S)
    if not m:raise ValueError('Некорректный sum statement')
    name=identifier(m[1]);value=m[2];source=None;by=[];initial='0';reset=None;where=[];replaced=[]
    seen_sum=False
    for item in items[1:]:
        text=item['text'].strip();mask=item['masked'].strip()
        if item is sum_items[0]:seen_sum=True;continue
        if re.match(r'set\b',mask,re.I):
            if source or seen_sum:raise ValueError('Накопитель: нужен один SET перед sum statement')
            source,opts=dataset(text,'set')
            if opts:raise ValueError('Накопитель: опции SET требуют отдельного правила')
        elif re.match(r'by\b',mask,re.I):by=by_fields(text[2:])
        elif re.match(r'retain\b',mask,re.I):
            r=re.fullmatch(r'retain\s+('+NAME+r')\s+(-?\d+(?:\.\d+)?)\s*;',text,re.I)
            if not r or identifier(r[1])!=name or seen_sum:raise ValueError('RETAIN: поддержан один накопитель и литеральное начальное значение до sum statement')
            initial=r[2]
        elif re.match(r'if\b',mask,re.I):
            r=re.fullmatch(r'if\s+first\.('+NAME+r')\s+then\s+('+NAME+r')\s*=\s*(-?\d+(?:\.\d+)?)\s*;',text,re.I)
            if not r or identifier(r[2])!=name or seen_sum or reset:raise ValueError('Накопитель: поддержан только IF FIRST.key THEN accumulator=literal перед sum statement')
            reset=(identifier(r[1]),r[3])
        elif re.match(r'where\b',mask,re.I):where.append(text[5:].rstrip(';'))
        elif re.match(r'(?:keep|drop|run|quit|%quitrun)\b',mask,re.I):replaced.append(item)
        else:raise ValueError('Накопитель: дополнительные изменения состояния требуют отдельного алгоритма')
    if source is None:raise ValueError('Накопитель: отсутствует SET')
    entry=contracts.get(source.casefold(),{});columns=catalog.get(source.casefold())
    if not columns or not isinstance(entry,dict) or not entry.get('order_by') or not entry.get('unique_order'):raise ValueError('Накопитель: нужны каталог и полный уникальный order_by')
    folded={c.casefold():c for c in columns}
    if name.casefold() in folded:raise ValueError('Накопитель уже читается из SET: его перезапись входом требует другого правила')
    types=entry.get('column_types',{});expr,_=scalar(value,date_storage,types,formats)
    if any(c.table or types.get(c.name.casefold())!='numeric' for c in expr.find_all(exp.Column)):raise ValueError('Накопитель: выражение должно читать подтверждённые числовые поля входа')
    def numeric(node):
        if isinstance(node,exp.Column):return types.get(node.name.casefold())=='numeric'
        if isinstance(node,exp.Literal):return not node.is_string
        if isinstance(node,(exp.Paren,exp.Neg)):return numeric(node.this)
        if isinstance(node,(exp.Add,exp.Sub,exp.Mul,exp.Div)):return numeric(node.this) and numeric(node.expression)
        return False
    if not numeric(expr):raise ValueError('Sum statement requires a numeric literal, column or arithmetic expression')
    if len(where)>1:raise ValueError('Repeated WHERE needs explicit SAS replacement semantics')
    if by and [v.casefold() for v in entry['order_by'][:len(by)]] != [('' if not desc else '-')+key.casefold() for key,desc in by]:raise ValueError('Accumulator order_by must start with BY keys and directions')
    partition=[]
    if reset:
        names=[n.casefold() for n,_ in by]
        if reset[0].casefold() not in names:raise ValueError('IF FIRST: поле отсутствует в BY')
        partition=names[:names.index(reset[0].casefold())+1];initial=reset[1]
    if any(k.casefold() not in folded for k,_ in by):raise ValueError('BY: неизвестный ключ')
    order=exp.Order(expressions=[exp.Ordered(this=exp.column(folded[v.lstrip('-').casefold()],quoted=True),desc=v.startswith('-'),nulls_first=not v.startswith('-')) for v in entry['order_by']])
    window=exp.Window(this=exp.Sum(this=exp.Coalesce(this=expr,expressions=[exp.Literal.number(0)])),partition_by=[exp.column(folded[k],quoted=True) for k in partition],order=order,
                      spec=exp.WindowSpec(kind='ROWS',start='UNBOUNDED',start_side='PRECEDING',end='CURRENT ROW'))
    total=exp.Add(this=exp.Literal.number(initial),expression=window)
    query=exp.select(*[exp.column(c,quoted=True) for c in columns],exp.alias_(total,name,quoted=True)).from_(source)
    for condition in where:
        predicate,_=scalar(condition,date_storage,types,formats)
        if any(c.table or c.name.casefold() not in folded for c in predicate.find_all(exp.Column)):raise ValueError('WHERE накопителя: допустимы только входные поля')
        query=query.where(truth(predicate))
    virtual='sas_migration_accumulator'
    if any(re.search(r'\bsas_migration_accumulator\b',i['masked'],re.I) for i in items):raise ValueError('Накопитель: конфликт служебного имени')
    text='set '+virtual+';';new_items=[items[0],{'text':text,'masked':mask_noncode(text,sas=True)}]+replaced
    tree,rules=compile_program(new_items,target,date_storage,{**catalog,virtual:columns+[name]},
        {**contracts,virtual:{'columns':columns+[name],'column_types':{**types,name:'numeric'}}},formats)
    cte=exp.CTE(this=query,alias=exp.TableAlias(this=exp.to_identifier(virtual)))
    tree.expression.args['with_'].expressions.insert(0,cte)
    rules=set(rules);rules.add('data.sum_statement.window');rules.discard('data.set.single')
    return tree,{},False,sorted(rules)
