"""Bounded DATA program parser and sequential relational lowering.

Every assignment/filter is a new SELECT layer. Branch predicates are captured
before their bodies, so later assignments cannot change which ELSE is taken.
No source execution, mutable database writes, or model calls.
"""
import re
from sqlglot import exp
from .data_rules import NAME, dataset, identifier, names, LEX


def is_program(items):
    return any(re.match(r'(?:else|do|delete|length|by)\b', i['masked'].strip(), re.I)
               or re.match(NAME+r'\s*=', i['text'].strip(), re.I)
               or re.search(r'^if\b.*\bthen\b',i['masked'].strip(),re.I|re.S) for i in items[1:])


class Parser:
    def __init__(self, items):
        self.items=items
        self.pos=0

    def block(self, nested=False):
        result=[]
        while self.pos<len(self.items):
            if re.fullmatch(r'end\s*;',self.items[self.pos]['masked'].strip(),re.I):
                if not nested:raise ValueError('END без соответствующего DO')
                self.pos+=1
                return result
            result.append(self.statement())
        if nested:raise ValueError('DO не закрыт END')
        return result

    def statement(self, item=None, depth=0):
        if depth>40:raise ValueError('Превышена глубина вложенности DATA-ветвей')
        if item is None:
            item=self.items[self.pos];self.pos+=1
        text=item['text'].strip();mask=item['masked'].strip()
        if re.fullmatch(r'do\s*;',mask,re.I):
            return ('block',self.block(nested=True))
        if re.match(r'if\b',mask,re.I):
            then=re.search(r'\bthen\b',mask,re.I)
            if then:
                condition=text[2:then.start()].strip()
                child={'text':text[then.end():].strip(),'masked':mask[then.end():].strip()}
                yes=self.statement(child,depth+1)
                no=('block',[])
                if self.pos<len(self.items) and re.match(r'else\b',self.items[self.pos]['masked'].strip(),re.I):
                    other=self.items[self.pos];self.pos+=1
                    no=self.statement({'text':other['text'].strip()[4:].strip(),'masked':other['masked'].strip()[4:].strip()},depth+1)
                return ('branch',condition,yes,no)
            return ('filter',text[2:].rstrip(';').strip())
        if re.fullmatch(r'delete\s*;',mask,re.I):return ('delete',)
        assignment=re.fullmatch(r'('+NAME+r')\s*=\s*(.+);',text,re.I|re.S)
        if assignment:return ('assign',identifier(assignment[1]),assignment[2].strip())
        raise ValueError('DATA-программа: поддержаны присваивания, IF/ELSE, простые DO/END, subsetting IF и DELETE; другие эффекты не пропускаются')


def assignments(nodes):
    for node in nodes:
        if node[0]=='assign':yield node
        elif node[0]=='block':yield from assignments(node[1])
        elif node[0]=='branch':
            yield from assignments([node[2]])
            yield from assignments([node[3]])


def scalar(text, date_storage, types=None, formats=None):
    from .compiler import normalize, parse_one, lower_functions, validate_tree, TOKEN
    normalized,rules=normalize(text,date_storage)
    tokens=[m.group() for m in TOKEN.finditer(normalized) if not m.group().isspace()]
    for i,token in enumerate(tokens):
        if token!='.':continue
        before=tokens[i-1] if i else ''
        after=tokens[i+1] if i+1<len(tokens) else ''
        if re.fullmatch(r'[A-Za-z_]\w*',before) and (re.fullmatch(r'[A-Za-z_]\w*',after) or after.startswith('"')):continue
        if after and re.fullmatch(r'[A-Za-z_]\w*',after):raise ValueError('Специальные SAS missing .A–.Z требуют отдельного кодирования')
        tokens[i]='NULL';rules.add('data.missing.ordinary')
    normalized=' '.join(tokens)
    tree=parse_one('SELECT '+normalized)
    if not isinstance(tree,exp.Select) or len(tree.expressions)!=1 or any(v for k,v in tree.args.items() if k!='expressions'):
        raise ValueError('DATA-выражение должно быть одним скалярным выражением')
    if tree.find(exp.Alias) or tree.find(exp.Subquery) or tree.find(exp.Star):
        raise ValueError('DATA-выражение не допускает SQL-проекцию или подзапрос')
    # A single-argument DATA statistic is not a PROC SQL aggregate.
    for node in list(tree.find_all(exp.Anonymous)):
        if node.name.lower() in {'__sas_sum','__sas_mean','__sas_min','__sas_max','__sas_n','__sas_nmiss'} and len(node.expressions)==1:
            arg=node.expressions[0].copy()
            if node.name.lower() in {'__sas_n','__sas_nmiss'}:
                missing=exp.Is(this=arg,expression=exp.Null())
                yes,no=(1,0) if node.name.lower()=='__sas_nmiss' else (0,1)
                node.replace(exp.Case(ifs=[exp.If(this=missing,true=exp.Literal.number(yes))],default=exp.Literal.number(no)))
            else:node.replace(arg)
            rules.add('data.statistics.scalar')
    lower_functions(tree,rules,date_storage,types,formats)
    validate_tree(tree)
    if tree.find(exp.AggFunc) or tree.find(exp.Window):raise ValueError('Агрегат/окно не является скалярным выражением DATA')
    expr=tree.expressions[0]
    # Ordinary numeric SAS missing sorts before every nonmissing number.
    # Special missing codes remain an explicit storage-contract limitation.
    def equality(a,b):
        both=exp.and_(exp.Is(this=a.copy(),expression=exp.Null()),exp.Is(this=b.copy(),expression=exp.Null()))
        return exp.or_(both,exp.Coalesce(this=exp.EQ(this=a.copy(),expression=b.copy()),expressions=[exp.false()]))
    def less(a,b):
        missing=exp.and_(exp.Is(this=a.copy(),expression=exp.Null()),exp.Not(this=exp.Is(this=b.copy(),expression=exp.Null())))
        return exp.or_(missing,exp.Coalesce(this=exp.LT(this=a.copy(),expression=b.copy()),expressions=[exp.false()]))
    for node in reversed(list(expr.walk())):
        if isinstance(node,(exp.EQ,exp.NEQ,exp.LT,exp.LTE,exp.GT,exp.GTE)):
            a,b=node.this,node.expression
            if any(isinstance(v,exp.Literal) and v.is_string for v in (a,b)):
                # Character comparison ignores trailing blanks in SAS.
                a=exp.Trim(this=exp.Coalesce(this=a.copy(),expressions=[exp.Literal.string('')]),position='TRAILING')
                b=exp.Trim(this=exp.Coalesce(this=b.copy(),expressions=[exp.Literal.string('')]),position='TRAILING')
            if isinstance(node,exp.EQ):new=equality(a,b)
            elif isinstance(node,exp.NEQ):new=exp.Not(this=equality(a,b))
            elif isinstance(node,exp.LT):new=less(a,b)
            elif isinstance(node,exp.GT):new=less(b,a)
            elif isinstance(node,exp.LTE):new=exp.or_(less(a,b),equality(a,b))
            else:new=exp.or_(less(b,a),equality(a,b))
            if node is expr:expr=new
            else:node.replace(new)
            rules.add('data.comparison.ordinary_missing')
        elif isinstance(node,exp.In):
            if not node.expressions:raise ValueError('DATA IN: нужен явный список значений')
            new=exp.or_(*[equality(node.this,v) for v in node.expressions])
            if node is expr:expr=new
            else:node.replace(new)
            rules.add('data.in.ordinary_missing')
    for node in expr.find_all(exp.Not):
        if not isinstance(node.this,exp.Paren):node.set('this',exp.Paren(this=node.this))
    while isinstance(expr,exp.Paren):expr=expr.this
    return expr,rules-{'proc_sql.select'}


def truth(expr):
    if isinstance(expr,exp.Paren):return truth(expr.this)
    if isinstance(expr,(exp.Predicate,exp.And,exp.Or,exp.Not)):
        return exp.Coalesce(this=expr,expressions=[exp.false()])
    # SAS numeric truth: zero and missing are false.
    if isinstance(expr,exp.Literal) and expr.is_string:
        raise ValueError('Символьная истинность требует явного преобразования SAS')
    return exp.Coalesce(this=exp.NEQ(this=expr,expression=exp.Literal.number(0)),expressions=[exp.false()])


def compile_program(items,target,date_storage,catalog,contracts,formats=None):
    from .compiler import parse_one
    output,out_opts=dataset(items[0]['text'],'data')
    source=None;in_opts={};body=[];where=[];keep=None;drop=None;lengths={};by=[]
    rules={'data.program.sequential','data.set.single'}
    for item in items[1:]:
        text=item['text'].strip();mask=item['masked'].strip()
        if re.fullmatch(r'(?:run|quit|%quitrun)\s*;',mask,re.I):continue
        if re.match(r'set\b',mask,re.I):
            if source is not None or body:raise ValueError('DATA-программа: нужен один SET до выполняемых инструкций')
            source,in_opts=dataset(text,'set')
        elif re.match(r'where\b',mask,re.I):where.append(text[5:].rstrip(';').strip())
        elif re.match(r'by\b',mask,re.I):
            if by:raise ValueError('Повторный BY не поддержан')
            tokens=[m.group() for m in LEX.finditer(text[2:].rstrip(';')) if not m.group().isspace()]
            descending=False
            for token in tokens:
                if token.lower()=='descending':
                    if descending:raise ValueError('Повторный DESCENDING')
                    descending=True
                else:
                    name=names([token])[0]
                    if name.lower() in {'notsorted','groupformat'}:raise ValueError('BY NOTSORTED/GROUPFORMAT требует другого алгоритма группировки')
                    by.append((name,descending));descending=False
            if descending or not by:raise ValueError('BY: некорректный список ключей')
        elif re.match(r'(?:keep|drop)\b',mask,re.I):
            values=names([m.group() for m in LEX.finditer(text[4:].rstrip(';')) if not m.group().isspace()])
            if mask.lower().startswith('keep'):
                if keep is not None:raise ValueError('Повторный KEEP не поддержан')
                keep=values
            else:
                if drop is not None:raise ValueError('Повторный DROP не поддержан')
                drop=values
        elif re.match(r'length\b',mask,re.I):
            match=re.fullmatch(r'length\s+('+NAME+r')\s+(\$\s*)?(\d+)\s*;',text,re.I)
            if not match or body:raise ValueError('LENGTH: поддержана одна переменная до выполняемых инструкций')
            lengths[identifier(match[1])]=(int(match[3]) if match[2] else None)
        else:body.append(item)
    if source is None:raise ValueError('DATA-программа: нужен SET')
    if source.rsplit('.',1)[-1].casefold().startswith('__sas_') or output.rsplit('.',1)[-1].casefold().startswith('__sas_'):
        raise ValueError('Имя таблицы конфликтует со служебным пространством __sas_')
    known=in_opts.get('keep') or catalog.get(source.casefold())
    if known is None:raise ValueError('DATA-программа разобрана; для проекции нужен состав SET из предыдущего SELECT или входного KEEP')
    known=list(known)
    if 'drop' in in_opts:known=[c for c in known if c.casefold() not in {v.casefold() for v in in_opts['drop']}]
    rename={k.casefold():v for k,v in in_opts.get('rename',{}).items()}
    initial={rename.get(c.casefold(),c):exp.column(c,quoted=True) for c in known}
    if len({c.casefold() for c in initial})!=len(known):raise ValueError('Конфликт имён после входного RENAME')
    if any(c not in {v.casefold() for v in known} for c in rename):raise ValueError('Входной RENAME ссылается на исключённое поле')
    declared_lengths=set(lengths)
    nodes=Parser(body).block()
    columns=list(initial)
    if any(c.casefold() in {v.casefold() for v in columns} for c in lengths):
        raise ValueError('LENGTH исходного поля: нужен типизированный каталог и порядок LENGTH/SET')
    contract=contracts.get(source.casefold(),{})
    source_types=contract.get('column_types',{}) if isinstance(contract,dict) else {}
    types={rename.get(c,c).casefold(): ('char' if t=='character' else t) for c,t in source_types.items()}
    types.update({c.casefold():('char' if length is not None else 'numeric') for c,length in lengths.items()})
    domains=contract.get('numeric_domains',{}) if isinstance(contract,dict) else {}
    domains={rename.get(c,c).casefold():v for c,v in domains.items()}
    def typed_scalar(value):return scalar(value,date_storage,{**{k:('character' if v=='char' else v) for k,v in types.items()},'__domains__':domains},formats)
    # SAS creates all variables at compilation time, including untaken branches.
    for _,name,value in assignments(nodes):
        if name.casefold() not in {c.casefold() for c in columns}:
            expr,_=typed_scalar(value)
            from .sas_functions import type_of
            if name not in lengths and expr.meta.get("sas_length"):
                lengths[name]=expr.meta["sas_length"]
            if name not in lengths and type_of(expr,{})=="character" and not isinstance(expr,exp.Literal):
                raise ValueError("Вычисляемая строковая переменная: укажите LENGTH до SET/вычислений")
            if name not in lengths and isinstance(expr,exp.Literal) and expr.is_string:
                if not expr.this.isascii():raise ValueError('Новая строковая переменная: нужен LENGTH и контракт кодировки/байтов SAS')
                lengths[name]=max(1,len(expr.this))
            by_flag=isinstance(expr,exp.Column) and expr.table.casefold() in {'first','last'}
            if name not in lengths and not by_flag and not isinstance(expr,(exp.Literal,exp.Add,exp.Sub,exp.Mul,exp.Div,exp.Neg,exp.Case,exp.Predicate,exp.And,exp.Or,exp.Not,exp.Null)):
                raise ValueError('Новая вычисляемая переменная: нужен LENGTH для типа и длины результата')
            types[name.casefold()]='char' if lengths.get(name) is not None else 'numeric'
            columns.append(name)
            numeric_type=exp.DataType.build('Nullable(Float64)',dialect='clickhouse') if target=='clickhouse' else exp.DataType.build('DOUBLE PRECISION')
            initial[name]=exp.Literal.string('') if types[name.casefold()]=='char' else exp.Cast(this=exp.Null(),to=numeric_type)
    if any(c.casefold().startswith('__sas_') for c in columns):raise ValueError('Имя входной колонки конфликтует со служебным пространством __sas_')
    folded={c.casefold():c for c in columns}
    by_flags={}
    seen={c.casefold() for c in known} | {c.casefold() for c in declared_lengths}
    def check_first_references(node):
        kind=node[0]
        values=[]
        if kind=='assign':
            seen.add(node[1].casefold())
            values=[node[2]]
        elif kind in {'branch','filter'}:values=[node[1]]
        for value in values:
            expr,_=typed_scalar(value)
            if any(types.get(c.name.casefold())=='char' and c.name.casefold() not in seen for c in expr.find_all(exp.Column)):
                raise ValueError('Новая строковая переменная использована до первого присваивания: нужен явный LENGTH')
        if kind=='block':
            for child in node[1]:check_first_references(child)
        elif kind=='branch':
            check_first_references(node[2]);check_first_references(node[3])
    for node in nodes:check_first_references(node)
    def expression(text):
        expr,extra=typed_scalar(text);rules.update(extra)
        for col in list(expr.find_all(exp.Column)):
            flag=by_flags.get((col.table.casefold(),col.name.casefold()))
            if flag:
                replacement=exp.column(flag,quoted=True)
                if col is expr:expr=replacement
                else:col.replace(replacement)
                continue
            if col.table or col.name.casefold() not in folded:
                raise ValueError('DATA-выражение ссылается на неизвестное поле или состояние BY; нужен отдельный контракт')
            col.set('this',exp.to_identifier(folded[col.name.casefold()],quoted=True))
        return expr
    # Bound generated SQL size linearly with CTE layers rather than substituting
    # every previous CASE into every later expression (exponential expansion).
    ctes=[];current='__sas_stage_0';live=list(columns);sequence=0
    base=exp.select(*[exp.alias_(value,c,quoted=True) for c,value in initial.items()]).from_(source)
    if where:
        predicates=[]
        for w in where:
            predicate=expression(w)
            for col in predicate.find_all(exp.Column):
                if col.name not in initial or not isinstance(initial[col.name],exp.Column):raise ValueError('WHERE читает новую переменную DATA; допустимы только поля входа')
                col.set('this',initial[col.name].this.copy())
            predicates.append(truth(predicate))
        base.set('where',exp.Where(this=exp.and_(*predicates)))
    ctes.append(exp.CTE(this=base,alias=exp.TableAlias(this=exp.to_identifier(current))))
    def layer(changes=None,condition=None):
        nonlocal sequence,current
        sequence+=1
        if sequence>250:raise ValueError('Превышен лимит 250 слоёв DATA-программы')
        changes=changes or {}
        for key in changes:
            if key not in live:live.append(key)
        projections=[exp.alias_(changes.get(c,exp.column(c,quoted=True)),c,quoted=True) for c in live]
        query=exp.select(*projections).from_(current)
        if condition is not None:query.set('where',exp.Where(this=condition))
        current=f'__sas_stage_{sequence}'
        ctes.append(exp.CTE(this=query,alias=exp.TableAlias(this=exp.to_identifier(current))))
    if by:
        contract=contracts.get(source.casefold(),{})
        order=contract.get('order_by',[]) if isinstance(contract,dict) else []
        if not order or not contract.get('unique_order'):
            raise ValueError('BY/FIRST/LAST: задайте полный order_by и unique_order=true для входа; произвольная строка при равных ключах недопустима')
        expected=[('-' if desc else '')+name.casefold() for name,desc in by]
        if [v.casefold() for v in order[:len(by)]]!=expected or any(v.lstrip('-').casefold() not in folded for v in order):
            raise ValueError('Контракт порядка не совпадает с BY или содержит исключённое поле; после RENAME нужен отдельный контракт')
        flags={}
        for i,(key,_) in enumerate(by):
            for mode in ['first','last']:
                terms=[]
                for field in order:
                    desc=field.startswith('-')
                    if mode=='last':desc=not desc
                    terms.append(exp.Ordered(this=exp.column(folded[field.lstrip('-').casefold()],quoted=True),desc=desc,nulls_first=not desc))
                window=exp.Window(this=exp.RowNumber(),partition_by=[exp.column(folded[v.casefold()],quoted=True) for v,_ in by[:i+1]],order=exp.Order(expressions=terms))
                helper=f'__sas_{mode}_{i}'
                flags[helper]=exp.Case(ifs=[exp.If(this=exp.EQ(this=window,expression=exp.Literal.number(1)),true=exp.Literal.number(1))],default=exp.Literal.number(0))
                by_flags[(mode,key.casefold())]=helper
        layer(flags);rules.add('data.by.first_last.ordered')
    def emit(node,guard=None):
        kind=node[0]
        if kind=='block':
            for child in node[1]:emit(child,guard)
        elif kind=='assign':
            name=folded[node[1].casefold()];value=expression(node[2])
            if isinstance(value,(exp.Predicate,exp.And,exp.Or,exp.Not)):
                value=exp.Case(ifs=[exp.If(this=truth(value),true=exp.Literal.number(1))],default=exp.Literal.number(0))
            if isinstance(value,exp.Literal) and value.is_string and types.get(name.casefold())=='numeric':
                raise ValueError('Неявное присваивание строки числовой переменной требует SAS informat')
            length=lengths.get(name)
            if length is not None:
                value=exp.Substring(this=value,start=exp.Literal.number(1),length=exp.Literal.number(length))
                rules.add('data.length.character')
            if guard is not None:
                value=exp.Case(ifs=[exp.If(this=guard.copy(),true=value)],default=exp.column(name,quoted=True))
            layer({name:value})
        elif kind=='branch':
            key=f'__sas_guard_{sequence+1}'
            layer({key:truth(expression(node[1]))})
            yes=exp.column(key,quoted=True)
            no=exp.Not(this=yes.copy())
            emit(node[2],exp.and_(guard.copy(),yes) if guard is not None else yes)
            emit(node[3],exp.and_(guard.copy(),no) if guard is not None else no)
            rules.add('data.if_else.case')
        elif kind in {'filter','delete'}:
            condition=truth(expression(node[1])) if kind=='filter' else exp.false()
            if guard is not None:condition=exp.or_(exp.Not(this=exp.Paren(this=guard.copy())),condition)
            layer(condition=condition);rules.add('data.filter.sequential')
    for node in nodes:emit(node)
    selected=keep or out_opts.get('keep') or columns
    excluded={c.casefold() for c in (drop or [])+out_opts.get('drop',[])}
    selected=[folded[c.casefold()] for c in selected if c.casefold() not in excluded and c.casefold() in folded]
    if any(c.casefold() not in folded for c in (keep or out_opts.get('keep') or [])) or not selected:
        raise ValueError('Неизвестные поля или пустая проекция KEEP/DROP')
    renamed={k.casefold():v for k,v in out_opts.get('rename',{}).items()}
    if any(k not in {c.casefold() for c in selected} for k in renamed):raise ValueError('Выходной RENAME ссылается на исключённое поле')
    output_names=[renamed.get(c.casefold(),c) for c in selected]
    if len({c.casefold() for c in output_names})!=len(selected):raise ValueError('Выходной RENAME создаёт конфликт имён')
    query=exp.select(*[exp.alias_(exp.column(c,quoted=True),new,quoted=True) for c,new in zip(selected,output_names)]).from_(current)
    query.set('with_',exp.With(expressions=ctes))
    tree=exp.Create(this=parse_one('SELECT * FROM '+output).args['from_'].this,kind='TABLE',expression=query)
    return tree,sorted(rules)
