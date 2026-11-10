"""Conservative projection flow, not a database catalogue or type inference."""
import sqlglot
import re
from sqlglot import exp


def validate_catalog(value):
    if not isinstance(value,dict) or len(value)>200:raise ValueError('Каталог: ожидается объект с не более 200 таблицами')
    result={}
    for table,entry in value.items():
        columns=entry.get('columns') if isinstance(entry,dict) else entry
        if not isinstance(table,str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?',table):
            raise ValueError('Каталог: некорректное имя таблицы')
        if not isinstance(columns,list) or not 1<=len(columns)<=2000 or any(not isinstance(c,str) or not c or len(c)>128 or any(ord(x)<32 for x in c) for c in columns):
            raise ValueError('Каталог: нужен непустой список имён колонок без значений строк')
        if len({c.casefold() for c in columns})!=len(columns) or table.casefold() in result:
            raise ValueError('Каталог: повторяющиеся таблицы или колонки')
        if isinstance(entry,dict):
            allowed={'columns','order_by','unique_order','column_types','column_lengths','numeric_domains','categorical_domains','unique_keys','missing_policy','collation','encoding'}
            if set(entry)-allowed or type(entry.get('unique_order',False)) is not bool:
                raise ValueError('Каталог: неизвестное поле контракта')
            order=entry.get('order_by',[])
            if not isinstance(order,list) or any(not isinstance(c,str) or c.lstrip('-').casefold() not in {v.casefold() for v in columns} for c in order) or len({c.lstrip('-').casefold() for c in order})!=len(order):
                raise ValueError('Каталог: порядок должен содержать разные имена входных колонок; минус означает DESCENDING')
            types=entry.get('column_types',{})
            if not isinstance(types,dict) or any(not isinstance(k,str) or k.casefold() not in {c.casefold() for c in columns} or v not in {'character','numeric','date','datetime'} for k,v in types.items()):
                raise ValueError('column_types: укажите существующие поля и character/numeric/date/datetime')
            if len({k.casefold() for k in types})!=len(types):raise ValueError('Повторяющиеся поля column_types')
            lengths=entry.get('column_lengths',{})
            if not isinstance(lengths,dict) or any(not isinstance(k,str) or k.casefold() not in {c.casefold() for c in columns} or type(v) is not int or not 1<=v<=32767 for k,v in lengths.items()):raise ValueError('column_lengths: существующие поля и длина SAS в байтах 1–32767')
            domains=entry.get('numeric_domains',{})
            if not isinstance(domains,dict):raise ValueError('numeric_domains: ожидается объект')
            for k,d in domains.items():
                if k.casefold() not in {c.casefold() for c in columns} or not isinstance(d,dict) or set(d)!={'integer','min','max'} or d['integer'] is not True or type(d['min']) is not int or type(d['max']) is not int or not -(2**53-1)<=d['min']<=d['max']<=2**53-1:raise ValueError('numeric_domains: поле, integer:true и целые min/max в точно представимом диапазоне SAS')
            categorical=entry.get('categorical_domains',{})
            if not isinstance(categorical,dict):raise ValueError('categorical_domains: ожидается объект')
            normalized_categories={}
            for k,values in categorical.items():
                if k.casefold() not in {c.casefold() for c in columns} or not isinstance(values,list) or not 1<=len(values)<=200 or any(type(v) not in {str,int,float} or isinstance(v,bool) or isinstance(v,str) and (not v or len(v)>128) for v in values):
                    raise ValueError('categorical_domains: поле и 1–200 строковых/числовых значений')
                if len({(type(v).__name__,str(v)) for v in values})!=len(values):raise ValueError('categorical_domains: повторяющиеся значения')
                normalized_categories[k.casefold()]=list(values)
            unique_keys=entry.get('unique_keys',[])
            if not isinstance(unique_keys,list) or any(not isinstance(key,list) or not key or any(not isinstance(c,str) or c.casefold() not in {v.casefold() for v in columns} for c in key) or len({c.casefold() for c in key})!=len(key) for key in unique_keys):
                raise ValueError('unique_keys: ожидается список непустых ключей из существующих колонок')
            missing=entry.get('missing_policy','')
            if missing not in {'','ordinary_null','encoded_special'}:raise ValueError('missing_policy: ordinary_null или encoded_special')
            collation=entry.get('collation','')
            encoding=entry.get('encoding','')
            if not isinstance(collation,str) or len(collation)>128 or not isinstance(encoding,str) or len(encoding)>64:raise ValueError('collation/encoding: некорректное значение')
            result[table.casefold()]={'numeric_domains':{k.casefold():v for k,v in domains.items()},'categorical_domains':normalized_categories,'unique_keys':[[c.casefold() for c in key] for key in unique_keys], 'column_lengths':{k.casefold():v for k,v in lengths.items()},'columns':list(columns),'order_by':order,'unique_order':entry.get('unique_order',False),'column_types':{k.casefold():v for k,v in types.items()},'missing_policy':missing,'collation':collation,'encoding':encoding}
        else:result[table.casefold()]=list(columns)
    return result


def table_key(table):
    return '.'.join(v for v in [table.catalog,table.db,table.name] if v).casefold()


def observe(op,catalog,dialect):
    if op['status']!='candidate':return
    tree=sqlglot.parse_one(op['sql'],read=dialect)
    if not isinstance(tree,exp.Create):return
    key=table_key(tree.this)
    query=tree.expression
    if not isinstance(query,exp.Select):catalog.pop(key,None);return
    columns=[]
    # A CTAS retains known SAS types only for direct, unambiguous columns.
    # Expressions, joins and CTEs need semantic type inference and must not
    # silently inherit a similarly named input field's contract.
    source=query.args.get('from_')
    simple_source=(source and isinstance(source.this,exp.Table)
                   and not query.args.get('joins') and not query.args.get('with_'))
    inherited=catalog.get(table_key(source.this)) if simple_source else None
    source_columns=inherited.get('columns',[]) if isinstance(inherited,dict) else inherited
    source_types=inherited.get('column_types',{}) if isinstance(inherited,dict) else {}
    source_lengths=inherited.get('column_lengths',{}) if isinstance(inherited,dict) else {}
    types={}
    lengths={}
    for expr in query.expressions:
        if expr.is_star:
            if not simple_source:
                catalog.pop(key,None);return
            if source_columns is None:catalog.pop(key,None);return
            columns.extend(source_columns)
            for col in source_columns:
                if col.casefold() in source_types:types[col.casefold()]=source_types[col.casefold()]
                if col.casefold() in source_lengths:lengths[col.casefold()]=source_lengths[col.casefold()]
        elif expr.alias_or_name:
            output=expr.alias_or_name
            columns.append(output)
            base=expr.this if isinstance(expr,exp.Alias) else expr
            if simple_source and isinstance(base,exp.Column) and not base.is_star:
                original=base.name.casefold()
                if source_columns and original in {col.casefold() for col in source_columns}:
                    if original in source_types:types[output.casefold()]=source_types[original]
                    if original in source_lengths:lengths[output.casefold()]=source_lengths[original]
        else:
            catalog.pop(key,None);return
    if not columns or len({c.casefold() for c in columns})!=len(columns):catalog.pop(key,None);return
    contract={'columns':columns,'column_types':types,'column_lengths':lengths} if types or lengths else columns
    catalog[key]=contract
    if tree.find(exp.TemporaryProperty) and "." not in key:catalog["work."+key]=contract
    logical = op.get('logical_output')
    if logical:
        catalog[logical.casefold()] = contract
        if '.' not in logical:
            catalog['work.' + logical.casefold()] = contract

