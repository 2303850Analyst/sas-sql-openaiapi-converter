"""Bounded SAS PROC SQL compiler and editable relational plan. No execution/LLM.

Rules produce candidates with explicit semantic prerequisites. They never prove
SAS/target equivalence. Unknown statements remain in the operation ledger.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from datetime import datetime

import sqlglot
from sqlglot import exp

VERSION = "sas-compiler-project-ir-1.0.0"
# SQLGlot's fallback warning includes SQL excerpts. Report our own safe diagnostic.
logging.getLogger('sqlglot').setLevel(logging.ERROR)
DIALECTS = {"postgresql": "postgres", "greenplum": "postgres", "clickhouse": "clickhouse"}
FIELDS = {"output", "select", "from", "where", "group_by", "having", "order_by", "distinct"}
TOKEN = re.compile(r"/\*.*?\*/|\s+|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_\u0080-\uffff][\w]*|\d+(?:\.\d+)?|\^=|~=|\*\*|<=|>=|<>|!=|.", re.S)


def parse_one(text):
    trees = sqlglot.parse(text, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    if len(trees) != 1 or trees[0] is None or trees[0].find(exp.Command):
        raise ValueError("Ожидалась одна SQL-операция")
    return trees[0]


def normalize(text, date_storage="unknown", attributes=None):
    tokens = [m.group() for m in TOKEN.finditer(text) if not m.group().isspace() and not m.group().startswith("/*")]
    from .sas_functions import format_tokens, in_lists, NAMES
    if attributes is not None:
        from .sas_functions import projection_attributes
        tokens, extracted = projection_attributes(tokens)
        attributes.extend(extracted)
    tokens = format_tokens(in_lists(tokens))
    result, rules = [], {"proc_sql.select"}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        low = t.lower()
        nxt = tokens[i+1].lower() if i+1 < len(tokens) else ""
        if t in {"%", "&"} or (t.startswith('"') and re.search(r"[&%]", t)):
            raise ValueError("Не раскрыт макропараметр или вызов макроса")
        if t.startswith(("'", '"')):
            value = t[1:-1].replace(t[0]*2, t[0])
            if nxt == "n":
                result.append('"' + value.replace('"', '""') + '"')
                rules.add("sas.name_literal"); i += 2; continue
            if nxt in {"d", "dt", "t"}:
                if nxt == "t" or date_storage != "native":
                    raise ValueError("SAS-дата: укажите контракт DATE/TIMESTAMP; числовые SAS-даты пока не поддержаны")
                try:
                    months = {m:n for n,m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(),1)}
                    match = re.fullmatch(r"(\d{1,2})([A-Za-z]{3})(\d{4})(?::(\d{2}):(\d{2}):(\d{2}))?",value)
                    if not match: raise ValueError()
                    day,mon,year,hh,mm,ss = match.groups()
                    if (nxt == "dt") != (hh is not None): raise ValueError()
                    stamp = datetime(int(year),months[mon.upper()],int(day),int(hh or 0),int(mm or 0),int(ss or 0))
                    result.append(("DATE '"+stamp.strftime("%Y-%m-%d") if nxt=="d" else "TIMESTAMP '"+stamp.strftime("%Y-%m-%d %H:%M:%S"))+"'")
                except (ValueError,KeyError):
                    raise ValueError("Поддержаны только SAS-даты с четырёхзначным годом и обычный datetime") from None
                rules.add("sas.date_literal.native"); i += 2; continue
            result.append("'"+value.replace("'","''")+"'")
        elif low == "not" and nxt in {"=", "eq"}:
            result.append("<>"); rules.add("sas.comparison_operator"); i += 2; continue
        elif low == "calculated":
            if not re.fullmatch(r"[A-Za-z_]\w*", nxt):
                raise ValueError("CALCULATED: нужен простой уникальный псевдоним")
            result.append("__sas_calculated_"+nxt); rules.add("sas.calculated.expand"); i += 2; continue
        elif low in {"format", "informat", "label", "length", "into", "outobs", "inobs"} and nxt != "(" and not (i and tokens[i-1].lower()=="as"):
            raise ValueError("Атрибуты SAS FORMAT/LENGTH/LABEL, INTO и ограничения строк требуют отдельного контракта")
        elif nxt == "(" and low in {"sum", "mean", "min", "max", "n", "nmiss", "compress", "lowcase", "upcase", "year", "month", "datepart"} | NAMES:
            result.append("__sas_"+low)
        elif low in {"eq", "ne", "gt", "ge", "lt", "le"}:
            result.append({"eq":"=","ne":"<>","gt":">","ge":">=","lt":"<","le":"<="}[low]); rules.add("sas.comparison_operator")
        elif t in {"^=", "~="}: result.append("<>"); rules.add("sas.comparison_operator")
        else: result.append(t)
        i += 1
    return " ".join(result), rules


def lower_functions(tree, rules, date_storage, types=None, formats=None):
    from .sas_functions import NAMES, lower
    types=types or {}
    # Postorder ensures nested functions are handled before their parents.
    for node in reversed(list(tree.walk())):
        if not isinstance(node, exp.Anonymous): continue
        name, args = node.name.lower(), node.expressions
        if not name.startswith("__sas_"):
            raise ValueError(f"Нет правила функции {node.name.upper()}; нужны типы/форматы или отдельная реализация")
        name = name[6:]
        if name in NAMES:
            replacement=lower(name,args,rules,date_storage,types,formats,node)
        elif name in {'sum','mean','min','max','n','nmiss'} and args:
            if len(args)>16:
                raise ValueError('Построчная статистика: поддержано не более 16 явных аргументов')
            if name in {'n','nmiss'} and any(isinstance(a,exp.Distinct) for a in args):
                raise ValueError('N/NMISS DISTINCT требует отдельного правила missing')
            if len(args) == 1:
                if name=='nmiss':
                    replacement = parse_one(f'SELECT COUNT(CASE WHEN ({args[0].sql()}) IS NULL THEN 1 END)').expressions[0]
                else:
                    replacement = {'sum':exp.Sum,'mean':exp.Avg,'min':exp.Min,'max':exp.Max,'n':exp.Count}[name](this=args[0].copy())
                rules.add('sas.'+name+'.aggregate')
            else:
                if any(a.find(exp.AggFunc) or isinstance(a,exp.Distinct) for a in args):
                    raise ValueError('Построчная статистика с вложенным агрегатом не поддержана')
                condition = " AND ".join(f"({a.sql()}) IS NULL" for a in args)
                total = " + ".join(f"COALESCE(({a.sql()}), 0)" for a in args)
                count = ' + '.join(f'CASE WHEN ({a.sql()}) IS NULL THEN 0 ELSE 1 END' for a in args)
                if name in {'min','max'}:
                    cmp = '<=' if name=='min' else '>='
                    clauses = []
                    for index, arg in enumerate(args):
                        others = ' AND '.join(f'(({b.sql()}) IS NULL OR ({arg.sql()}) {cmp} ({b.sql()}))' for j,b in enumerate(args) if j!=index)
                        clauses.append(f'WHEN ({arg.sql()}) IS NOT NULL AND {others} THEN ({arg.sql()})')
                    expression = 'CASE ' + ' '.join(clauses) + ' ELSE NULL END'
                elif name in {'n','nmiss'}:
                    expression = f'{len(args)} - ({count})' if name=='nmiss' else count
                else:
                    value = f'(1.0 * ({total})) / NULLIF(({count}),0)' if name=='mean' else total
                    expression = f'CASE WHEN {condition} THEN NULL ELSE {value} END'
                replacement = parse_one('SELECT '+expression).expressions[0]
                rules.add('sas.'+name+'.row_missing')
        elif name in {"lowcase", "upcase"} and len(args) == 1:
            replacement = (exp.Lower if name=="lowcase" else exp.Upper)(this=args[0].copy())
            rules.add("sas."+name)
        elif name == "compress" and len(args) == 2:
            if not isinstance(args[1],exp.Literal) or not args[1].is_string or len(args[1].this)>128:
                raise ValueError("COMPRESS: нужен литеральный список удаляемых символов без модификаторов")
            replacement=args[0].this.copy() if isinstance(args[0],exp.Pad) and args[0].meta.get("sas_length") and args[1].this==" " else args[0].copy()
            for char in dict.fromkeys(args[1].this):
                replacement=exp.Replace(this=replacement,expression=exp.Literal.string(char),replacement=exp.Literal.string(""))
            rules.add("sas.compress.characters")
        elif name == "compress" and len(args) == 1:
            replacement = exp.Replace(this=args[0].copy(),expression=exp.Literal.string(" "),replacement=exp.Literal.string(""))
            rules.add("sas.compress.blanks")
        elif name in {"year", "month", "datepart"} and len(args) == 1:
            if date_storage != "native":
                raise ValueError("YEAR/MONTH/DATEPART: нужен явный контракт DATE/TIMESTAMP вместо чисел SAS")
            replacement = (exp.Cast(this=args[0].copy(),to=exp.DataType.build("DATE")) if name=="datepart"
                           else exp.Extract(this=exp.Var(this=name.upper()),expression=args[0].copy()))
            rules.add("sas."+name+".native")
        else:
            raise ValueError(f"Функция {name.upper()}: этот набор аргументов не поддержан")
        if name=="compress" and args[0].meta.get("sas_length"):
            replacement.meta.update(sas_type="character",sas_length=args[0].meta["sas_length"])
        node.replace(replacement)


def expand_calculated(tree):
    marked = [n for n in tree.find_all(exp.Column) if n.name.startswith("__sas_calculated_")]
    if not marked: return
    query = tree.expression if isinstance(tree,exp.Create) else tree
    if len(list(query.find_all(exp.Select))) != 1:
        raise ValueError("CALCULATED во вложенных запросах требует анализа областей видимости")
    aliases = {}
    for projection in query.expressions:
        for col in list(projection.find_all(exp.Column)):
            if col.name.startswith("__sas_calculated_"):
                alias = col.name[len("__sas_calculated_"):].lower()
                if alias not in aliases: raise ValueError("CALCULATED: ссылка вперёд, цикл или неизвестный псевдоним")
                col.replace(exp.Paren(this=aliases[alias].copy()))
        if projection.alias:
            key = projection.alias.lower()
            if key in aliases: raise ValueError("CALCULATED: неоднозначный псевдоним")
            aliases[key] = projection.this.copy()
    for col in list(query.find_all(exp.Column)):
        if col.name.startswith("__sas_calculated_"):
            value = aliases.get(col.name[len("__sas_calculated_"):].lower())
            if value is None or value.find(exp.AggFunc):
                raise ValueError("CALCULATED: агрегат вне SELECT или неизвестный псевдоним")
            col.replace(exp.Paren(this=value.copy()))


ALLOWED_FUNCTIONS = (exp.Sum, exp.Count, exp.Min, exp.Max, exp.Avg, exp.Coalesce, exp.Lower, exp.Upper,
                     exp.Replace, exp.Cast, exp.Extract, exp.Case, exp.If, exp.And, exp.Or, exp.Not, exp.Nullif,
                     exp.Trim, exp.Substring, exp.Concat, exp.ConcatWs, exp.TimeToStr, exp.TimestampTrunc, exp.Length,
                     exp.Pad, exp.RegexpLike, exp.StrPosition, exp.Floor, exp.Mod)


def validate_tree(tree):
    query = tree.expression if isinstance(tree,(exp.Create,exp.Insert)) else tree
    if isinstance(tree,exp.Create):
        if tree.args.get("kind") != "TABLE" or not isinstance(tree.this,exp.Table) or tree.args.get("properties"):
            raise ValueError("Поддержан CREATE TABLE AS SELECT без физического DDL")
    elif isinstance(tree,exp.Insert):
        if not isinstance(tree.this,exp.Table) or not isinstance(query,(exp.Select,exp.Union)):
            raise ValueError('INSERT: поддержан только INSERT INTO ... SELECT')
    elif not isinstance(tree,(exp.Select,exp.Union)):
        raise ValueError("Поддержаны SELECT, UNION, CTAS и INSERT SELECT")
    if not isinstance(query,(exp.Select,exp.Union)): raise ValueError("Некорректное дерево запроса")
    for node in query.walk():
        if isinstance(node,exp.AggFunc) and (node.args.get('expressions') or node.this is None):
            raise ValueError('Агрегат с несколькими аргументами требует отдельного построчного SAS-правила')
        if isinstance(node,exp.Func) and not isinstance(node,ALLOWED_FUNCTIONS):
            raise ValueError("Нет правила функции "+node.sql_name())
        if isinstance(node,(exp.Window,exp.Into,exp.Command)):
            raise ValueError("Оконная функция или побочный эффект не входит в текущий набор правил")
    for select in query.find_all(exp.Select):
        for clause in [select.args.get('where'), *select.args.get('joins',[])]:
            if clause and any(a.find_ancestor(exp.Select) is select for a in clause.find_all(exp.AggFunc)):
                raise ValueError('Агрегат в WHERE/ON запрещён PROC SQL')
        # SAS can remerge aggregates back to source rows. Never silently GROUP BY.
        group = select.args.get("group")
        grouped = {g.sql().lower() for g in group.expressions} if group else set()
        if any(a.find_ancestor(exp.Select) is select for a in select.find_all(exp.AggFunc)):
            for p in select.expressions:
                expr = p.this if isinstance(p,exp.Alias) else p
                outside = [c for c in expr.find_all(exp.Column) if not c.find_ancestor(exp.AggFunc)]
                if outside and expr.sql().lower() not in grouped:
                    raise ValueError("SAS aggregate remerge: неагрегированные поля не совпадают с GROUP BY")
                if expr.find(exp.Star) and not expr.find(exp.AggFunc):
                    raise ValueError("SAS aggregate remerge со * требует каталога")
            having = select.args.get('having')
            if having:
                aggregate_aliases = {p.alias.lower() for p in select.expressions if p.alias and p.find(exp.AggFunc)}
                for col in having.find_all(exp.Column):
                    if col.find_ancestor(exp.Select) is select and not col.find_ancestor(exp.AggFunc) and col.sql().lower() not in grouped and col.name.lower() not in aggregate_aliases:
                        raise ValueError('SAS aggregate remerge в HAVING: поле не входит в GROUP BY')
        elif group:
            raise ValueError("GROUP BY без агрегатов в SAS может означать сортировку; требуется отдельное правило")
    return query


def describe(tree):
    query = tree.expression if isinstance(tree,(exp.Create,exp.Insert)) else tree
    if not isinstance(query,exp.Select):
        return {"output":tree.this.sql(dialect="postgres") if isinstance(tree,(exp.Create,exp.Insert)) else "",
                "select":"","from":"","where":"","group_by":"","having":"","order_by":"","distinct":False},False
    def clause(key, prefix):
        value=query.args.get(key)
        return value.sql(dialect="postgres")[len(prefix):] if value else ""
    source = clause("from_", "FROM ")
    joins = " ".join(j.sql(dialect="postgres") for j in query.args.get("joins", []))
    editable = not query.args.get("with_") and len(list(query.find_all(exp.Select)))==1 and not query.args.get("limit") and not query.args.get("offset")
    return {"output":tree.this.sql(dialect="postgres") if isinstance(tree,exp.Create) else "",
            "select":",\n".join(p.sql(dialect="postgres") for p in query.expressions),
            "from":(source+" "+joins).strip(),"where":clause("where","WHERE "),
            "group_by":clause("group","GROUP BY "),"having":clause("having","HAVING "),
            "order_by":clause("order","ORDER BY "),"distinct":bool(query.args.get("distinct"))},bool(editable)


def from_fields(fields):
    if not isinstance(fields,dict) or set(fields)!=FIELDS:
        raise ValueError("Нужны все поля логической операции")
    if type(fields['distinct']) is not bool or any(not isinstance(v,str) or len(v)>40000 for k,v in fields.items() if k!='distinct'):
        raise ValueError("Некорректный формат полей операции")
    query=parse_one("SELECT "+fields['select'])
    if not isinstance(query,exp.Select) or any(v for k,v in query.args.items() if k not in {'expressions'}):
        raise ValueError("В поле SELECT допустим только список выражений")
    query.set('distinct',exp.Distinct() if fields['distinct'] else None)
    if fields['from'].strip():
        src=parse_one("SELECT * FROM "+fields['from'])
        if not isinstance(src,exp.Select) or any(v for k,v in src.args.items() if k not in {'expressions','from_','joins'}):
            raise ValueError("В поле источников допустимы только FROM и JOIN")
        query.set('from_',src.args['from_'].copy())
        query.set('joins',[j.copy() for j in src.args.get('joins',[])])
    for key,ast_key,keyword in [('where','where','WHERE'),('group_by','group','GROUP BY'),('having','having','HAVING'),('order_by','order','ORDER BY')]:
        if fields[key].strip():
            part=parse_one('SELECT 1 '+keyword+' '+fields[key])
            if not isinstance(part,exp.Select) or not part.args.get(ast_key) or any(v for k,v in part.args.items() if k not in {'expressions',ast_key}):
                raise ValueError('Недопустимое содержимое поля '+keyword)
            query.set(ast_key,part.args[ast_key].copy())
    tree=query
    if fields['output'].strip():
        table_query=parse_one('SELECT * FROM '+fields['output'])
        table=table_query.args.get('from_').this
        if not isinstance(table,exp.Table) or table.alias or len(list(table_query.find_all(exp.Table)))!=1 or any(v for k,v in table_query.args.items() if k not in {'expressions','from_'}):
            raise ValueError('Выход должен быть именем одной таблицы')
        tree=exp.Create(this=table.copy(),kind='TABLE',expression=query)
    validate_tree(tree)
    return tree


def render(tree,target):
    from .target_adapters import adapter_for
    return adapter_for(target).render(tree)


def compile_query(text,target,date_storage='unknown',source_catalog=None,format_catalog=None):
    attributes=[]
    normalized,rules=normalize(text,date_storage,attributes)
    tree=parse_one(normalized)
    if isinstance(tree,exp.Create) and isinstance(tree.expression,exp.Union) or isinstance(tree,exp.Union):
        if any(isinstance(node,(exp.Intersect,exp.Except)) for node in tree.walk()):
            raise ValueError('Поддержаны UNION и UNION ALL; INTERSECT/EXCEPT требуют отдельного правила SAS')
        rules.add('proc_sql.union')
    if attributes:
        tree.meta["sas_attributes"]=attributes
        rules.add("sas.projection.display_attributes")
    from .sas_functions import query_types
    lower_functions(tree,rules,date_storage,query_types(tree,source_catalog or {}),format_catalog)
    expand_calculated(tree)
    validate_tree(tree)
    fields,editable=describe(tree)
    return tree,fields,editable,sorted(rules)


def compile_data(items, target, date_storage, catalog=None, contracts=None, formats=None):
    from .data_rules import compile_relational_data
    from .data_program import is_program, compile_program
    if any(re.match(r"^\s*merge\b",i["masked"],re.I) for i in items):
        from .merge_rules import compile_merge
        return compile_merge(items,target,date_storage,catalog or {},contracts or {},formats or {})
    from .data_rules import NAME
    if any(re.match(r"^\s*"+NAME+r"\s*\+",i["text"],re.I) for i in items[1:]):
        from .accumulator_rules import compile_accumulator
        return compile_accumulator(items,target,date_storage,catalog or {},contracts or {},formats or {})
    from .set_lists import sources,compile_list
    set_items=[i for i in items if re.match(r"^\s*set\b",i["masked"],re.I)]
    multiple=len(set_items)>1 or (len(set_items)==1 and (":" in set_items[0]["masked"] or len(sources(set_items[0]["text"],catalog or {}))>1))
    if multiple:
        tree,rules=compile_list(items,target,date_storage,catalog or {},contracts or {},formats or {})
        return tree,{},False,rules
    if is_program(items):
        tree, rules = compile_program(items, target, date_storage, catalog or {}, contracts or {}, formats or {})
    else:
        tree, rules = compile_relational_data(items, target, date_storage, compile_query, parse_one)
    fields, editable = describe(tree)
    return tree, fields, editable, sorted(rules)


def _logical_table_key(table: exp.Table) -> str:
    value = '.'.join(part for part in (table.catalog, table.db, table.name) if part).casefold()
    return value[5:] if value.startswith('work.') else value


def _rewrite_physical_tables(tree: exp.Expression, physical_names: dict | None) -> tuple[list[str], str]:
    """Apply a precomputed dataset-version plan without changing SAS lowering.

    Reads and writes have separate maps so a self-write reads the previous
    version and writes the next one. CTE references stay local to the query.
    """
    output = tree.this if isinstance(tree, (exp.Create, exp.Insert)) and isinstance(tree.this, exp.Table) else None
    logical_output = _logical_table_key(output) if output is not None else ''
    cte_names = {cte.alias.casefold() for cte in tree.find_all(exp.CTE)}
    logical_inputs: list[str] = []
    reads = (physical_names or {}).get('reads', {})
    for table in list(tree.find_all(exp.Table)):
        if table is output or (not table.db and table.name.casefold() in cte_names):
            continue
        key = _logical_table_key(table)
        logical_inputs.append(key)
        physical = reads.get(key)
        if physical:
            table.replace(exp.to_table(physical))
    physical_output = (physical_names or {}).get('writes', {}).get(logical_output)
    if output is not None and physical_output:
        tree.set('this', exp.to_table(physical_output))
    return list(dict.fromkeys(logical_inputs)), logical_output


def candidate(op, compiled, target, edits, physical_names=None):
    tree, fields, editable, rules = compiled
    original_sql = render(tree, target)
    if op['id'] in edits:
        if not editable: raise ValueError('Эта операция пока не редактируется структурно')
        tree = from_fields(edits[op['id']])
        updated, _ = describe(tree)
        op['changed'] = updated != fields
        fields = updated
    from .storage_rules import target_tree
    source_output=tree.this.sql() if isinstance(tree,(exp.Create,exp.Insert)) else ""
    logical_inputs, logical_output = _rewrite_physical_tables(tree, physical_names)
    tree=target_tree(tree)
    output = tree.this if isinstance(tree,(exp.Create,exp.Insert)) else None
    cte_names={cte.alias.casefold() for cte in tree.find_all(exp.CTE)}
    inputs = sorted({exp.Table(this=t.this.copy(), db=t.args.get('db').copy() if t.args.get('db') else None,
                               catalog=t.args.get('catalog').copy() if t.args.get('catalog') else None).sql()
                     for t in tree.find_all(exp.Table) if t is not output and not (not t.db and t.name.casefold() in cte_names)})
    from .handler_registry import trace_rules
    op.update(status='candidate', reason='', fields=fields, editable=editable, rules=rules, rule_trace=trace_rules(rules),
              sql=render(tree, target), original_sql=original_sql, inputs=inputs,
              output=output.sql() if output is not None else '',
              sas_attributes=tree.meta.get('sas_attributes',[]), order_contract=tree.meta.get('order_contract'),
              source_output=source_output, logical_inputs=logical_inputs, logical_output=logical_output,
              physical_version=bool(physical_names),
              source_output_physical=output.sql() if output is not None else '',
              storage='append' if isinstance(tree,exp.Insert) else 'temporary' if isinstance(tree,exp.Create) and tree.find(exp.TemporaryProperty) else 'schema' if output is not None else 'query',
              requirements=['Типы, SAS missing, сравнение строк и collation требуют каталога и контрольных данных'])
    if output is not None and output.sql().lower() in {i.lower() for i in inputs}:
        op['requirements'].append('Чтение и перезапись одной таблицы: нужна новая версия набора и атомарная публикация')


def analyze(source,target,edits=None,date_storage='unknown',source_catalog=None,format_catalog=None,parameters=None,semantic_ir=None,physical_plan=None):
    from .sas_blocks import partition, walk, statement_id
    from .rule_catalog import classify
    from .schema_flow import validate_catalog
    contracts=validate_catalog(source_catalog or {})
    from .format_catalog import validate_formats
    formats=validate_formats(format_catalog or {})
    if semantic_ir is None:
        from .semantic_engine import SasSemanticEngine
        semantic_ir = SasSemanticEngine().analyze(source, source_catalog, parameters)
    from .semantic_engine import compiler_items
    items, cutoff, steps = compiler_items(semantic_ir)
    if not steps:
        steps = partition(items, cutoff)
    owners = {oid: node for node in walk(steps) for oid in node['operation_ids']}
    from .macro_scope import blocked_statements
    blocked = blocked_statements(items)
    tainted_steps = {owners[statement_id(n,item)]["id"] for n,item in enumerate(items) if blocked[n]}
    operations=[]; in_sql=False; macro_depth=0; stopped=False
    edits=edits or {}
    for number,item in enumerate(items):
        text,masked=item['text'],item['masked'].strip()
        ident=statement_id(number,item)
        family, reason = classify(masked)
        op={'id':ident,'line':item['line'],'end_line':item['end_line'],'step_id':owners[ident]['id'], 'family':family,
            'status':'unsupported','kind':'query' if owners[ident]['kind']=='proc_sql' and re.match(r'(?:select|create\s+table)\b',masked,re.I) else 'sas_statement','reason':reason,'rules':[], 'editable':False,'changed':False}
        structural = bool(re.fullmatch(r'(?:quit|run(?:\s+cancel)?|proc\s+sql(?:\s+noprint)?)\s*;',masked,re.I))
        if structural:
            op.update(status='control',kind='proc_sql' if re.match(r'proc\b',masked,re.I) else 'boundary',rules=['sas.syntax.omitted'],reason='Служебная граница SAS: в SQL не переносится')
        if stopped:
            op.update(scope_blocked=True,reason='Код после ENDSAS: не исполняется в этой сессии SAS')
            operations.append(op); continue
        if re.match(r'%macro\b',masked,re.I): macro_depth+=1
        if macro_depth:
            op['scope_blocked'] = True
            op['reason']='Определение макроса: тело не разворачивается и не исполняется автоматически'
            if re.match(r'%mend\b',masked,re.I): macro_depth-=1
            operations.append(op); continue
        if blocked[number] or owners[ident]['id'] in tainted_steps:
            in_sql = False
            op['scope_blocked'] = True
            op['reason']='Условная макрообласть: нужны параметры и раскрытие ветвей'
            operations.append(op); continue
        if re.match(r'proc\s+sql\b',masked,re.I):
            in_sql=bool(re.fullmatch(r'proc\s+sql(?:\s+noprint)?\s*;',masked,re.I))
            op.update(status='control' if in_sql else 'unsupported',kind='proc_sql',reason='' if in_sql else 'Параметры PROC SQL пока не поддержаны')
        elif re.fullmatch(r'(?:quit|run(?:\s+cancel)?)\s*;',masked,re.I):
            if masked.lower().startswith('quit'): in_sql=False
            op.update(status='control',kind='boundary',reason='Служебная граница SAS: в SQL не переносится',rules=['sas.syntax.omitted'])
        elif re.match(r'%quitrun\b',masked,re.I):
            in_sql=False; op.update(kind='error_policy',reason='Обёртка обработки ошибок/уведомлений требует переноса в оркестратор')
        elif in_sql and owners[ident]['kind']=='proc_sql' and re.match(r'(?:select|create\s+table)\b',masked,re.I):
            op['kind']='query'
            try:
                candidate(op, compile_query(text,target,date_storage,contracts,formats), target, edits,
                          ((physical_plan or {}).get(owners[ident]['id']) or {}).get('operations', {}).get(ident,
                              (physical_plan or {}).get(owners[ident]['id'])))
            except Exception as error:
                # Do not leak parser excerpts/literals into diagnostics.
                op['reason']=str(error) if isinstance(error,ValueError) and not isinstance(error,sqlglot.errors.ParseError) else 'SQL вне поддержанного синтаксиса: требуется отдельное правило'
        else:
            if re.match(r'(?:data|proc)\b',masked,re.I): in_sql=False
            if re.fullmatch(r'endsas\s*;',masked,re.I): stopped=True
            if re.search(r'[%&]',masked): op['reason']='Макровызов или параметр: нет детерминированного раскрытия'
        operations.append(op)
    by_id = {op['id']: op for op in operations}
    item_by_id = {statement_id(n,item): item for n,item in enumerate(items)}
    from .schema_flow import observe,validate_catalog
    contracts=validate_catalog(source_catalog or {})
    declared_catalog={k:(v['columns'] if isinstance(v,dict) else v) for k,v in contracts.items()}
    catalog=dict(declared_catalog)
    for node in walk(steps):
        owned = [by_id[oid] for oid in node['operation_ids']]
        if node['kind'] == 'data' and not any(op.get('scope_blocked') for op in owned):
            head = owned[0]
            head['kind'] = 'data_step'
            try:
                if node['boundary_issue'] or node['termination'] in {'quit', 'run cancel', 'macro_definition_boundary', 'macro_definition_end'}:
                    raise ValueError(node['boundary_issue'] or 'DATA STEP отменён или пересекает неопределённую макрограницу')
                candidate(head, compile_data([item_by_id[oid] for oid in node['operation_ids']],target,date_storage,{**declared_catalog,**catalog},contracts,formats),target,edits,
                          (physical_plan or {}).get(node['id']))
                head['requirements'].append('DATA SET: каталог должен подтвердить состав/порядок колонок, длины и форматы; порядок строк не гарантируется SQL-таблицей')
                if 'data.program.sequential' in head['rules']:
                    head['requirements'].append('DATA-программа: заданный/выведенный состав входа — предусловие; типы, длины существующих строк, SAS missing и побайтовое усечение требуют проверки. CTE не меняют физическую таблицу на месте.')
                if 'data.by.first_last.ordered' in head['rules']:
                    head['requirements'].append('BY: полный порядок и уникальность order_by заявлены в контракте входа, но не проверены на данных. Контракт должен действовать в точке чтения SET.')
                for op in owned[1:]:
                    if op['kind'] not in {'boundary', 'error_policy'}:
                        op.update(status='covered',reason='',covered_by=head['id'],rules=head['rules'])
            except Exception as error:
                head['reason'] = str(error) if type(error) is ValueError else 'DATA STEP вне поддержанного синтаксиса'
        if node.get('procedure')=='sort' and not any(op.get('scope_blocked') for op in owned):
            head=owned[0];head['kind']='sort_step'
            try:
                if node['boundary_issue'] or node['termination']=='run cancel':raise ValueError('SORT отменён или имеет незавершённую границу')
                from .procedure_rules import compile_sort
                candidate(head,compile_sort([item_by_id[oid] for oid in node['operation_ids']],{**declared_catalog,**catalog},contracts),target,edits,
                          (physical_plan or {}).get(node['id']))
                head['requirements'].append('SORT: SQL-таблица не хранит гарантированный порядок; потребитель обязан использовать явный ORDER BY. EQUALS/NODUPKEY зависит от подтверждённого порядка входа.')
                for op in owned[1:]:
                    if op['kind'] not in {'boundary','error_policy'}:op.update(status='covered',reason='',covered_by=head['id'],rules=head['rules'])
            except ValueError as error:head['reason']=str(error)
        if node.get('procedure') in {'append','transpose','summary','means'} and not any(op.get('scope_blocked') for op in owned):
            head=owned[0];head['kind']=node['procedure']+'_step'
            try:
                if node['boundary_issue'] or node['termination']=='run cancel':raise ValueError('Процедура отменена или имеет незавершённую границу')
                from .procedure_coverage import compile_procedure
                candidate(head,compile_procedure([item_by_id[oid] for oid in node['operation_ids']],node['procedure'],contracts),target,edits,
                          (physical_plan or {}).get(node['id']))
                for op in owned[1:]:
                    if op['kind'] not in {'boundary','error_policy'}:op.update(status='covered',reason='',covered_by=head['id'],rules=head['rules'])
            except ValueError as error:head['reason']=str(error)
        if node.get('kind')=='procedure' and node.get('procedure') not in {'sort','append','transpose','summary','means'} and not any(op.get('scope_blocked') for op in owned):
            from .procedure_coverage import orchestrator_plan
            plan=orchestrator_plan(node.get('procedure'))
            if plan:
                head=owned[0]
                head.update(status='orchestrator',kind='orchestrator_action',reason='',rules=[],editable=False,
                            orchestrator_plan={'action':plan.action,'requirements':list(plan.requirements)})
                for op in owned[1:]:
                    if op['kind'] not in {'boundary','error_policy'}:
                        op.update(status='covered',reason='',covered_by=head['id'],rules=[])
        # An unknown operation can mutate schemas. Never use a stale projection
        # across such a boundary, or infer a schema from a later SELECT.
        if any(o['status']=='unsupported' and o['kind']!='error_policy' for o in owned):
            catalog.clear()
        else:
            for op in owned:observe(op,catalog,DIALECTS[target])
        if node['boundary_issue']:
            diagnostic = {'id': node['id']+'-boundary', 'step_id': node['id'], 'line':node['end_line'],
                          'end_line':node['end_line'], 'status':'unsupported', 'kind':'structure', 'reason':node['boundary_issue'],
                          'rules':[], 'editable':False, 'changed':False}
            operations.append(diagnostic)
            by_id[diagnostic['id']] = diagnostic
            node['operation_ids'].append(diagnostic['id'])
    # A syntactically valid fragment is not usable if it overwrites its own
    # input or reads a data set whose preceding producer was not converted.
    # Keep the source operation, but remove its SQL from the draft.
    ir_steps = {step['id']: step for step in semantic_ir.get('steps', [])}
    blocked_steps = set()
    for node in walk(steps):
        owned = [by_id[oid] for oid in node['operation_ids']]
        reads = ir_steps.get(node['id'], {}).get('reads', [])
        unavailable = [read['dataset'] for read in reads if read.get('producer_step_id') in blocked_steps]
        for op in owned:
            if op['status'] != 'candidate':
                continue
            self_write = op.get('output', '').casefold() in {name.casefold() for name in op.get('inputs', [])}
            if self_write or unavailable:
                op.update(status='unsupported', reason=(
                    'Чтение и перезапись одной таблицы: нужен версионированный результат и публикация'
                    if self_write else 'Предыдущий шаг не преобразован; недоступен входной набор данных'))
                op.pop('sql', None)
                op.pop('original_sql', None)
        if any(op['status'] == 'unsupported' for op in owned):
            blocked_steps.add(node['id'])
            for op in owned:
                if op['status'] == 'covered':
                    op.update(status='unsupported', reason='Шаг преобразуется целиком; SQL для него не сформирован')

    if cutoff is not None:
        operations.append({'id':'unparsed-tail','line':cutoff,'status':'unsupported','kind':'unparsed_tail','reason':'Неразобранный хвост: число операций неизвестно','rules':[],'editable':False,'changed':False})
        steps.append({'id':'unparsed-tail-block','kind':'unknown','title':'Неразобранный хвост','line':cutoff,'end_line':len(source.splitlines()),
                      'operation_ids':['unparsed-tail'],'children':[],'termination':'lexical_cutoff','boundary_issue':'Разбор остановлен','parent_id':None})
    by_id = {op['id']: op for op in operations}
    for node in reversed(list(walk(steps))):
        ids = node['operation_ids'] + [oid for child in walk(node['children']) for oid in child['operation_ids']]
        node['candidate_count'] = sum(by_id[oid]['status']=='candidate' for oid in ids)
        node['unsupported_count'] = sum(by_id[oid]['status']=='unsupported' for oid in ids)
        node['orchestrator_count'] = sum(by_id[oid]['status']=='orchestrator' for oid in ids)
        node['status'] = 'blocked' if node['unsupported_count'] else 'candidate' if node['candidate_count'] else 'orchestrator' if node['orchestrator_count'] else 'control'
        node['statement_count'] = len(ids)
    unknown=set(edits)-{o['id'] for o in operations if o['editable']}
    if unknown: raise ValueError('Правка ссылается на отсутствующую или неподдержанную операцию')
    from .migration_cases import annotate
    annotate(by_id, items, owners, statement_id)
    candidates=[o for o in operations if o['status']=='candidate']
    return {'steps':steps,'operations':operations,'candidates':candidates,'unsupported':[{'id':o['id'],'line':o['line'],'reason':o['reason']} for o in operations if o['status']=='unsupported'],
            'cutoff':cutoff,'inputs':sorted({v for o in candidates for v in o['inputs']}),'outputs':sorted({o['output'] for o in candidates if o['output']}),
            'ir_hash':semantic_ir.get('ir_hash'),'ir_diagnostics':semantic_ir.get('diagnostics',[]),'dependencies':semantic_ir.get('dependencies',[])}
