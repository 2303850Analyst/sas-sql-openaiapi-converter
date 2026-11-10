"""Actionable reasons for unlowered SAS families. No generic keyword rewrite."""
import re

FAMILIES = [
    (r'(?:merge|update|modify)\b', 'pdv_merge', 'MERGE/UPDATE/MODIFY: нужны ключи, кратность BY-групп, PDV и политика изменения таблицы; обычный JOIN не эквивалентен'),
    (r'(?:by|retain)\b|\b(?:first|last)\.', 'row_state', 'BY/FIRST/LAST/RETAIN: нужны порядок строк, границы групп и состояние между наблюдениями'),
    (r'(?:if|else|do|end|select|when|otherwise)\b', 'control_flow', 'Управление DATA STEP: требуется модель ветвей, порядка вычислений и неявного OUTPUT'),
    (r'(?:output|delete|stop|abort|return|link|goto)\b', 'data_effect', 'Управление наблюдениями/выполнением: нужно сохранить число выходных строк и точку остановки'),
    (r'(?:format|informat|length|attrib|label|rename|drop)\b', 'metadata', 'Атрибуты/состав колонок: нужен каталог типов, длин, форматов и исходных имён'),
    (r'(?:input|infile|file|put|datalines4?|cards4?)\b', 'io', 'Ввод/вывод данных: нужен отдельный контракт загрузки или публикации; это не SQL-выражение'),
    (r'(?:array|declare|dcl|call)\b', 'runtime', 'ARRAY/HASH/CALL: нужна модель состояния и побочных эффектов, прямой замены ключевого слова нет'),
    (r'(?:libname|filename|options|ods|title\d*|footnote\d*|endsas|x)\b', 'environment', 'Окружение/отчёт/внешний эффект: переносится в контракт запуска, а не в SELECT'),
    (r'proc\s+sort\b', 'sort', 'PROC SORT: нужен контракт порядка, сравнения и выбора строки для NODUPKEY; DISTINCT не является общей заменой'),
    (r'proc\s+transpose\b', 'transpose', 'PROC TRANSPOSE: нужны список выходных колонок, BY/ID/VAR и политика повторяющихся ID'),
    (r'proc\s+(?:means|summary|freq|tabulate)\b', 'summary', 'Агрегирующая процедура: нужны CLASS/BY/VAR/OUTPUT, уровни группировки и политика missing'),
    (r'proc\s+(?:datasets|append|copy|contents)\b', 'catalog', 'Операции наборами: нужен каталог и политика DDL/загрузки/атомарной публикации'),
    (r'proc\b', 'procedure', 'Процедура распознана; отдельное правило этой процедуры ещё не реализовано'),
    (r'%|.*&', 'macro', 'Макропроцессор: нужны параметры, область видимости и детерминированное раскрытие'),
    (r'(?:data|set|keep|where)\b', 'data_step', 'DATA STEP проверяется целиком: поддержаны один SET, один WHERE и простой KEEP без других операций'),
]


def classify(masked):
    for pattern, family, reason in FAMILIES:
        if re.search('^(?:' + pattern + ')', masked.strip(), re.I):
            return family, reason
    return 'unclassified', 'Конструкция вне проверенного подмножества SAS; нужен отдельный разбор по документации'
