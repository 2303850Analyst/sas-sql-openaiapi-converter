"""Structural SAS steps, without macro expansion or execution.

Every lexical statement belongs to exactly one node. Macro definitions own
their delimiters and contain child steps; RUN groups stay inside procedures.
See SAS_DOCUMENTATION_MAP.md for the boundary rules and limitations.
"""
import hashlib
import re

RUN_GROUP = {'sql', 'datasets', 'catalog', 'plot', 'pmenu', 'trantab',
             'reg', 'glm', 'gplot', 'gchart', 'gmap', 'greplay', 'iml'}
# Only documented batch procedures are closed by RUN. Unknown procedures stay
# open conservatively until QUIT or the next DATA/PROC boundary.
RUN_CLOSE = {'sort', 'print', 'means', 'summary', 'transpose', 'freq', 'contents',
             'append', 'copy', 'export', 'import', 'tabulate', 'report', 'univariate',
             'rank', 'fcmp', 'http', 'delete', 'odstext', 'format'}


def statement_id(number, item):
    if item.get('id'):
        return item['id']
    return f's{number+1}-' + hashlib.sha256(item.get('original_text',item['text']).encode()).hexdigest()[:10]


def partition(items, cutoff=None):
    nodes = []
    stack = []
    active = None

    def new(kind, title, item, number, procedure=''):
        node = {'id': 'block-' + statement_id(number, item), 'kind': kind,
                'title': title, 'line': item['line'], 'end_line': item['end_line'],
                'offset': item['offset'], 'end_offset': item['end_offset'],
                'operation_ids': [], 'children': [], 'procedure': procedure,
                'termination': '', 'boundary_issue': '', 'run_groups': [],
                'parent_id': stack[-1]['id'] if stack else None}
        (stack[-1]['children'] if stack else nodes).append(node)
        return node

    def add(node, item, number):
        node['operation_ids'].append(statement_id(number, item))
        node['end_line'] = item['end_line']
        node['end_offset'] = item['end_offset']
        for parent in stack:
            parent['end_line'] = item['end_line']
            parent['end_offset'] = item['end_offset']

    def close(reason):
        nonlocal active
        if active:
            active['termination'] = reason
            active = None

    for number, item in enumerate(items):
        masked = item['masked'].strip()
        if re.match(r'%macro\b', masked, re.I):
            close('macro_definition_boundary')
            match = re.match(r'%macro\s+(\w+)', masked, re.I)
            node = new('macro', '%MACRO ' + (match[1] if match else '?'), item, number)
            add(node, item, number)
            stack.append(node)
        elif re.match(r'%mend\b', masked, re.I):
            close('macro_definition_end')
            if stack:
                add(stack[-1], item, number)
                stack.pop()['termination'] = '%mend'
            else:
                node = new('unknown', '%MEND без %MACRO', item, number)
                add(node, item, number)
                node['boundary_issue'] = 'Закрытие без определения макроса'
        elif re.match(r'(?:proc|data)\b', masked, re.I):
            close('next_step')
            proc = re.match(r'proc\s+(\w+)', masked, re.I)
            if proc:
                name = proc[1].lower()
                active = new('proc_sql' if name == 'sql' else 'procedure', 'PROC ' + name.upper(), item, number, name)
            else:
                active = new('data', 'DATA', item, number)
            add(active, item, number)
        elif re.fullmatch(r'(?:run(?:\s+cancel)?|quit)\s*;', masked, re.I):
            word = masked[:-1].strip().lower()
            if active:
                add(active, item, number)
                active['run_groups'].append({'line': item['line'], 'boundary': word})
                if word == 'quit' or (word == 'run cancel' and active['procedure'] in RUN_GROUP - {'sql'}) or (active['kind'] == 'data' or active['procedure'] in RUN_CLOSE):
                    close(word)
            else:
                node = new('boundary', word.upper(), item, number)
                add(node, item, number)
                node['termination'] = word
        elif re.match(r'%quitrun\b', masked, re.I):
            node = active or new('macro_call', '%QUITRUN', item, number)
            add(node, item, number)
            close('custom_macro_boundary')
        elif re.fullmatch(r'endsas\s*;', masked, re.I):
            node = active or new('global', 'ENDSAS', item, number)
            add(node, item, number)
            close('endsas')
        else:
            if active is None:
                kind = 'macro_call' if masked.startswith('%') else 'global'
                match = re.match(r'%?\w+', masked)
                active = new(kind, match[0].upper() if match else 'Инструкция', item, number)
            add(active, item, number)
            if item.get('inline_data'):
                close('inline_data_terminator')
            elif active['kind'] in {'global', 'macro_call'}:
                close('statement')
    if active:
        active['termination'] = 'lexical_cutoff' if cutoff else 'eof'
        if active['kind'] in {'data', 'procedure', 'proc_sql'}:
            active['boundary_issue'] = 'Нет явного завершения; EOF требует контракта режима запуска SAS'
    for parent in stack:
        parent['termination'] = 'lexical_cutoff' if cutoff else 'eof'
        parent['boundary_issue'] = 'Нет %MEND; определение макроса не завершено'
    return nodes


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node['children'])
