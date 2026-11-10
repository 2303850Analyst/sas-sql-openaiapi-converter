"""Atomic relational DATA rules. No PDV state or arbitrary assignments are guessed.

Input KEEP precedes input RENAME. Output KEEP precedes output RENAME.
Statement RENAME has different timing and is deliberately rejected.
"""
import re
from sqlglot import exp

NAME = r"(?:[A-Za-z_\u0080-\uffff]\w*|'(?:''|[^'])*'[nN]|\"(?:\"\"|[^\"])*\"[nN])"
TABLE = r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?'
LEX = re.compile(NAME + r'|\s+|.', re.S)


def identifier(value):
    if value.startswith(("'", '"')):
        return value[1:-2].replace(value[0]*2, value[0])
    return value.lower()


def names(tokens):
    if not tokens or any(not re.fullmatch(NAME, t) for t in tokens):
        raise ValueError('KEEP/DROP: нужен явный список полей; диапазоны и префиксы требуют каталога')
    result = [identifier(t) for t in tokens]
    if len({v.casefold() for v in result}) != len(result):
        raise ValueError('KEEP/DROP: повторяющиеся имена полей')
    return result


def dataset(text, keyword):
    match = re.fullmatch(keyword + r'\s+(' + TABLE + r')\s*(.*?)\s*;', text, re.I | re.S)
    if not match or match[1].lower() == '_null_':
        raise ValueError('DATA/SET: нужна одна явная таблица; _NULL_, списки и макропараметры требуют отдельного правила')
    tail = match[2].strip()
    if not tail:
        return match[1], {}
    if not (tail.startswith('(') and tail.endswith(')')):
        raise ValueError('DATA/SET: не распознаны опции одной таблицы')
    tokens = [m.group() for m in LEX.finditer(tail[1:-1]) if not m.group().isspace()]
    options = {}
    pos = 0
    while pos < len(tokens):
        key = tokens[pos].lower()
        if key not in {'keep', 'drop', 'rename'} or key in options or tokens[pos+1:pos+2] != ['=']:
            raise ValueError('Опции DATA/SET: поддержаны KEEP, DROP и RENAME; WHERE/IN/END/OBS требуют отдельного правила')
        pos += 2
        if key == 'rename':
            if tokens[pos:pos+1] != ['(']:
                raise ValueError('RENAME=: ожидается список (старое=новое)')
            pos += 1
            pairs = {}
            while pos < len(tokens) and tokens[pos] != ')':
                if pos+2 >= len(tokens) or tokens[pos+1] != '=':
                    raise ValueError('RENAME=: некорректная пара имён')
                old, new = names([tokens[pos]])[0], names([tokens[pos+2]])[0]
                if old.casefold() in {v.casefold() for v in pairs}:
                    raise ValueError('RENAME=: повторное исходное имя')
                pairs[old] = new
                pos += 3
            if pos >= len(tokens) or not pairs:
                raise ValueError('RENAME=: список не закрыт или пуст')
            if len({v.casefold() for v in pairs.values()}) != len(pairs):
                raise ValueError('RENAME=: повторное целевое имя')
            options[key] = pairs
            pos += 1
        else:
            start = pos
            while pos < len(tokens) and not (pos+1 < len(tokens) and tokens[pos+1] == '='):
                pos += 1
            options[key] = names(tokens[start:pos])
    if 'keep' in options and 'drop' in options:
        raise ValueError('KEEP и DROP одновременно: требуется явный каталог результата')
    return match[1], options


def compile_relational_data(items, target, date_storage, compile_query, parse_one):
    output, out_opts = dataset(items[0]['text'], 'data')
    source = None
    in_opts = {}
    keep = None
    where = None
    filters = []
    rules = ['data.set.single']
    for item in items[1:]:
        text = item['text'].strip()
        if re.fullmatch(r'(?:run|quit|%quitrun)\s*;', text, re.I):
            if text.lower().startswith('%quitrun'):
                rules.append('data.quitrun.relational_only')
            continue
        if re.match(r'set\b', text, re.I) and source is None:
            source, in_opts = dataset(text, 'set')
        elif re.match(r'keep\b', text, re.I) and keep is None and 'keep' not in out_opts:
            keep = names([m.group() for m in LEX.finditer(text[4:].rstrip(';')) if not m.group().isspace()])
        elif re.match(r'where\b', text, re.I) and where is None:
            where = text[5:].rstrip(';').strip()
            if re.match(r'(?:also|same)\b', where, re.I):
                raise ValueError('WHERE ALSO/SAME требует отдельного правила')
        elif re.match(r'if\b', text, re.I):
            if source is None:
                raise ValueError('IF перед SET: фильтр читает PDV до загрузки строки и не переносится в WHERE')
            # Use the lexical mask: THEN inside a character literal is harmless.
            if re.search(r'\bthen\b', item['masked'], re.I):
                raise ValueError('IF THEN: нужен порядок присваиваний и каталог PDV; шаблон CASE описан в справочнике')
            filters.append(text[2:].rstrip(';').strip())
        else:
            raise ValueError('DATA STEP: дополнительные инструкции требуют отдельного правила PDV; частичный SELECT не выдаётся')
    if source is None:
        raise ValueError('DATA STEP: нужен один SET; генератор строк требует отдельного правила')
    if 'drop' in in_opts:
        raise ValueError('Входной DROP: нужен каталог оставшихся колонок')
    input_columns = in_opts.get('keep')
    rename_in = in_opts.get('rename', {})
    lookup = {k.casefold(): v for k, v in rename_in.items()}
    pdv_columns = [lookup.get(v.casefold(), v) for v in input_columns] if input_columns else None
    if pdv_columns and len({v.casefold() for v in pdv_columns}) != len(pdv_columns):
        raise ValueError('Входной RENAME создаёт конфликт имён')
    if input_columns and any(k.casefold() not in {v.casefold() for v in input_columns} for k in rename_in):
        raise ValueError('Входной RENAME ссылается на поле, исключённое KEEP')
    columns = keep or out_opts.get('keep') or pdv_columns
    if 'drop' in out_opts:
        if columns is None:
            raise ValueError('Выходной DROP: нужен каталог или входной KEEP для явной проекции')
        columns = [c for c in columns if c.casefold() not in {v.casefold() for v in out_opts['drop']}]
    rename_out = out_opts.get('rename', {})
    if columns is None and (rename_in or rename_out):
        raise ValueError('RENAME со SELECT *: нужен каталог или явный KEEP, чтобы не создавать дубли колонок')
    if columns == []:
        raise ValueError('Проекция DATA STEP пуста')
    if columns and pdv_columns and any(c.casefold() not in {v.casefold() for v in pdv_columns} for c in columns):
        raise ValueError('Выходной KEEP содержит поле, отсутствующее после входного KEEP/RENAME')
    if columns and any(k.casefold() not in {c.casefold() for c in columns} for k in rename_out):
        raise ValueError('Выходной RENAME ссылается на исключённое поле')
    tree, _, _, base_rules = compile_query(f'CREATE TABLE {output} AS SELECT * FROM {source}', target, date_storage)
    inverse = {v.casefold(): k for k, v in rename_in.items()}
    def resolve(node):
        if isinstance(node, exp.Column):
            if node.table:
                raise ValueError('DATA: квалифицированные колонки требуют отдельного правила')
            if pdv_columns and node.name.casefold() not in {v.casefold() for v in pdv_columns}:
                raise ValueError('Условие ссылается на поле вне входного KEEP')
            if node.name.casefold() in lookup and node.name.casefold() not in inverse:
                raise ValueError('Условие использует старое имя после входного RENAME')
            return exp.column(inverse.get(node.name.casefold(), node.name), quoted=True)
        return node
    if columns:
        projection = []
        outputs = set()
        renamed = {k.casefold(): v for k, v in rename_out.items()}
        for name in columns:
            new = renamed.get(name.casefold(), name)
            if new.casefold() in outputs:
                raise ValueError('Выходной RENAME создаёт конфликт имён')
            outputs.add(new.casefold())
            column = exp.column(inverse.get(name.casefold(), name), quoted=True)
            projection.append(exp.alias_(column, new, quoted=True) if column.name != new else column)
        tree.expression.set('expressions', projection)
        rules.append('data.keep.explicit')
    conditions = []
    for condition, is_if in ([(where, False)] if where else []) + [(v, True) for v in filters]:
        condition_tree, _, _, condition_rules = compile_query('SELECT * FROM source WHERE ' + condition, target, date_storage)
        if any(v for k, v in condition_tree.args.items() if k not in {'expressions', 'from_', 'where'}):
            raise ValueError('DATA: условие содержит посторонний SQL-раздел')
        expr = condition_tree.args['where'].this
        if expr.find(exp.Subquery) or expr.find(exp.AggFunc):
            raise ValueError('DATA: агрегаты и подзапросы в фильтре не поддержаны')
        def predicate(node):
            if isinstance(node, (exp.Paren, exp.Not)):
                return predicate(node.this)
            if isinstance(node, (exp.And, exp.Or)):
                return predicate(node.this) and predicate(node.expression)
            return isinstance(node, exp.Predicate)
        if is_if and not predicate(expr):
            raise ValueError('Подмножество IF: нужен явный предикат; числовая истинность требует типов')
        conditions.append(expr.transform(resolve))
        rules.extend(condition_rules)
    if conditions:
        tree.expression.set('where', exp.Where(this=exp.and_(*conditions)))
    if where: rules.append('data.where.input')
    if filters: rules.append('data.if.subsetting')
    if rename_in: rules.append('data.rename.input')
    if rename_out: rules.append('data.rename.output')
    return tree, sorted((set(rules) | set(base_rules)) - {'proc_sql.select'})
