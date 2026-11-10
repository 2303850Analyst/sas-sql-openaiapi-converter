"""Deterministic evidence score. It is NOT a probability of correct conversion."""
from collections import Counter

METHOD_VERSION = 'readiness-v2'


def assess(project, build):
    blocks=build['blocks']
    ops=[o for b in blocks for o in b['operations'] if o['status']!='control' and o['kind'] not in {'unparsed_tail','structure'}]
    total=len(ops)
    converted=sum(o['status'] in {'candidate','covered'} for b in blocks if not b['manual'] for o in b['operations'])
    coverage=converted/total if total else 0
    sql_total=sum(o['kind']=='query' for o in ops)
    sql_converted=sum(o['kind']=='query' and o['status']=='candidate' for b in blocks if not b['manual'] for o in b['operations'])
    incomplete=any(b['cutoff'] is not None or b['display_source_redacted'] or any(o['kind']=='structure' for o in b['operations']) for b in blocks)
    manual=sum(b['manual'] for b in blocks)
    changed=sum(o.get('changed',False) for o in ops)
    syntax=sum(b['checks'][0]['status']=='passed' for b in blocks)
    have_code=[b for b in blocks if b['checks'][0]['status']!='not_run']
    syntax_ratio=syntax/len(have_code) if have_code else 0
    outputs=Counter(o.get('logical_output',o.get('output','')).lower() for o in ops if o.get('logical_output') or o.get('output'))
    repeated=sorted(k for k,v in outputs.items() if v>1)
    self_reads=sum(o.get('logical_output',o.get('output','')).lower() in
                   {i.lower() for i in o.get('logical_inputs',o.get('inputs',[]))}
                   for o in ops if o.get('logical_output') or o.get('output'))
    def gate(ident,name,passed,detail):
        return {'id':ident,'name':name,'status':'passed' if passed else 'blocked','detail':detail}
    gates=[
        gate('source','Полнота исходника',not incomplete,'Исходник частично разобран или маскирован' if incomplete else 'Просмотрены все доступные statements'),
        gate('coverage','Покрытие правилами',bool(total) and converted==total and not manual,f'{converted} из {total} наблюдаемых операций; ручных блоков: {manual}'),
        gate('syntax','SQL-синтаксис',bool(have_code) and syntax==len(have_code),f'{syntax} из {len(have_code)} непустых блоков прошли AST-проверку'),
        gate('write_policy','Конфликты записи',not manual and not repeated and not self_reads and bool(total),f'Повторные выходы: {len(repeated)}; чтение собственного выхода: {self_reads}; ручных блоков: {manual}. Полные зависимости и внешние входы требуют каталога.'),
        gate('catalog','Типы и семантика SAS',False,'Нет проверенного каталога: missing, ключи, collation, длины строк и типы не подтверждены'),
        gate('target','Профиль целевой СУБД',False,'Нужны проверенная версия и физическая политика таблиц/публикации'),
        gate('execution','Исполнение в целевой СУБД',False,'Не выполнялось'),
        gate('comparison','Совпадение с SAS',False,'Нет контрольных запусков на одинаковых входах'),
    ]
    # Missing evidence scores zero. Denominators and weights are always visible.
    components=[
        {'name':'Покрытие наблюдаемых операций','weight':25,'points':round(25*coverage,2)},
        {'name':'Полнота доступного исходника','weight':10,'points':10 if not incomplete and total else 0},
        {'name':'Синтаксис непустых SQL-блоков','weight':10,'points':round(10*syntax_ratio,2)},
        {'name':'Статическая проверка записи','weight':5,'points':5 if gates[3]['status']=='passed' else 0},
        {'name':'Каталог и профиль СУБД','weight':10,'points':0},
        {'name':'Исполнение в целевой СУБД','weight':20,'points':0},
        {'name':'Сравнение с SAS','weight':20,'points':0},
    ]
    return {'method':METHOD_VERSION,'readiness_score':round(sum(c['points'] for c in components),2),
            'accuracy':None,'accuracy_status':'not_measured','observed_operations':total,'rule_converted_operations':converted,
            'observed_sql_operations':sql_total,'sql_coverage_percent':round(100*sql_converted/sql_total,2) if sql_total else 0,
            'coverage_percent':round(100*coverage,2),'coverage_scope':'observed_only' if incomplete else 'available_source',
            'manual_blocks':manual,'changed_operations':changed,'repeated_outputs':repeated,
            'components':components,'gates':gates,'release_allowed':False,
            'limitation':'Индекс готовности отражает наличие доказательств, не вероятность правильности. Максимум текущих статических проверок — 50/100. Любая блокировка запрещает выпуск.'}


def report_text(q):
    rows=['# Детерминированный отчёт о качестве','',f"Методика: {q['method']}",
          f"Индекс готовности: {q['readiness_score']}/100. Точность: не измерена.",
          f"Покрытие правилами: {q['rule_converted_operations']}/{q['observed_operations']} наблюдаемых операций ({q['coverage_percent']}%).",
          '',q['limitation'],'','| Компонент | Баллы | Максимум |','|---|---:|---:|']
    rows += [f"| {c['name']} | {c['points']} | {c['weight']} |" for c in q['components']]
    rows += ['','## Проверки','']+[f"- {g['name']}: {g['status']}. {g['detail']}" for g in q['gates']]
    rows += ['',f"Изменённых пользователем операций: {q['changed_operations']}. Проверка исходной эквивалентности не заменяет приёмку новой бизнес-логики."]
    return '\n'.join(rows)+'\n'
