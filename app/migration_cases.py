"""Migration recipes for constructs observed in 348; recipes are not converters.

The compiler alone assigns candidate/covered status. A recognized recipe never
clears a blocker. This catalogue can be used by the editor and offline audits.
"""
import re

SAS = 'https://support.sas.com/documentation/cdl/en/'
SOURCES = {
    'data': SAS+'lrcon/65287/HTML/default/p10abuffsctmo1n1dp2gasb34p76.htm',
    'operators': SAS+'lrcon/65287/HTML/default/n1bhfu7h99wzgwn1md27dqxx4b5y.htm',
    'macro': SAS+'mcrolref/62978/HTML/default/p08ksb4ivhj2l5n1blya220dr98z.htm',
    'sort': SAS+'proc/65145/HTML/default/p02bhn81rn4u64n1b6l00ftdnxge.htm',
    'merge': 'https://support.sas.com/kb/24/752.html',
    'transpose': SAS+'proc/65145/HTML/default/p1r2tjnp8ewe3sn1acnpnrs3xbad.htm',
    'statements': SAS+'lestmtsref/63323/PDF/default/lestmtsref.pdf',
}

# id, label, proposed lowering, prerequisite, source key
RECIPES = [
 ('sql.query','PROC SQL / SELECT / CTAS','Разобрать SQL AST; нормализовать операторы, имена, функции; сгенерировать целевой диалект.','Каталог, missing, типы дат, collation; remerge и побочные эффекты проверяются отдельно.','operators'),
 ('data.pipeline','DATA / SET','Один SET → SELECT; несколько SET-наборов → UNION ALL только после выравнивания PDV по именам и типам.','Состав/длины колонок; префиксные списки раскрываются из каталога. SET с BY требует отдельного слияния потоков.','data'),
 ('data.projection','KEEP / DROP / RENAME','Входной KEEP/DROP → входной RENAME → вычисления → выходной KEEP/DROP → выходной RENAME.','Явный состав колонок. RENAME statement имеет иной порядок относительно выходных опций.','data'),
 ('data.filter','WHERE / subsetting IF','WHERE фильтрует вход; IF фильтрует текущее состояние строки. В поддержанном шаге без присваиваний условия объединяются AND.','Нельзя переносить IF перед присваиваниями, MERGE или RETAIN. Числовую истинность и missing проверять отдельно.','statements'),
 ('data.assign','Присваивание','Реализованы последовательные CTE: каждое присваивание читает результат предыдущего; нужен известный состав SET.','Каталог PDV: исходные/новые переменные, типы, длины, усечение строк, начальные missing.','statements'),
 ('data.branch','IF THEN / ELSE','Реализованы CASE с фиксацией условий, ELSE IF, вложенные DO/END и последовательные независимые IF при известном составе SET.','Порядок операторов, связь ELSE, PDV, неявный OUTPUT; нельзя объединять независимые IF одним CASE.','statements'),
 ('data.by_state','BY / FIRST. / LAST. / RETAIN','Реализованы FIRST/LAST через ROW_NUMBER по префиксу BY-ключей при заявленном полном уникальном порядке; RETAIN пока не реализован.','Полный воспроизводимый порядок с разрешением равных ключей, DESCENDING/NOTSORTED, missing; RETAIN требует модели состояния.','statements'),
 ('data.merge','MERGE / IN=','Разобрать BY-группы и PDV. JOIN допустим только при доказанной кратности; иначе моделировать позиционное потребление и удержание значений.','Кардинальности обеих сторон, порядок, конфликты имён, длительность IN-флагов в группе.','merge'),
 ('proc.sort','PROC SORT','Обычная сортировка → контракт порядка; NODUPKEY → ROW_NUMBER по ключам с выбором первой строки.','EQUALS/NOEQUALS, NODUP/NODUPKEY, колляция, порядок входа, DUPOUT; не заменять вслепую DISTINCT.','sort'),
 ('proc.transpose','PROC TRANSPOSE / ID / VAR','Стабильный список ID → условные агрегаты/PIVOT, BY → ключи групп, VAR → значения.','Каталог ID, отсутствующие ID, дубликаты и LET, имена выходных колонок и типы.','transpose'),
 ('proc.datasets','PROC DATASETS / DELETE','Отдельный план DDL и жизненного цикла промежуточных наборов.','LIBRARY, MEMBER TYPE, зависимости и разрешение удаления; не исполнять автоматически.','statements'),
 ('macro.definition','%MACRO / %MEND / вызов','Построить шаблон и таблицу областей видимости; раскрывать вызов с привязкой фактических параметров.','Тело макроса не является самостоятельным выполняемым шагом; неизвестный вызов остаётся блокировкой.','macro'),
 ('macro.parameters','%LET / &parameter / %SYSFUNC','Разрешать только известные чистые выражения; сохранить типизированный манифест параметров запуска.','Дата запуска, каталог EXIST, значения и область видимости; не подставлять произвольный текст в SQL.','macro'),
 ('macro.branch','%IF / %DO / %END / %ELSE','Построить условный граф и ограничить область ветви; раскрывать после определения условий/границ цикла.','Неразрешённые ветви блокируются. Независимый следующий шаг можно анализировать как черновик.','macro'),
 ('runtime.quitrun','%QUITRUN','Закрыть SAS-шаг; обработчик статуса, остановку и уведомления перенести в оркестратор.','Это пользовательский макрос. SQL части шага не заменяет обработчик и не снимает блокировку.','macro'),
 ('runtime.abort','%ABORT / ABORT / ENDSAS','Узел остановки процесса с сохранением причины и результата предыдущего шага.','Границы транзакций, политика публикации/отката, отмены и уведомлений.','statements'),
 ('runtime.environment','LIBNAME / FILENAME / OPTIONS','Манифест подключений и параметров исполнения; секреты получать во время запуска.','Отображение библиотек на схемы/таблицы; SAS options могут менять семантику, их нельзя отбросить.','statements'),
 ('runtime.log','%PUT / PUT / FILE','Отдельный шаг журнала, файла или уведомления.','Назначение FILE, кодировка, формат и правила экспозиции значений.','statements'),
 ('data.symput','CALL SYMPUT / SYMPUTX','Выход вычислительного шага → типизированный параметр следующего шага.','Тип/формат значения, момент присвоения и область макропеременной; не подменять текстовым SELECT.','macro'),
 ('data.inline','INPUT / INFILE / CARDS / DATALINES','Отдельный типизированный входной набор и загрузка; источник строк хранится вне отчёта аудита.','Разделители, informats, длины, кодировка. Замаскированные строки нельзя восстановить из интерфейса.','statements'),
 ('data.attributes','FORMAT / INFORMAT / LENGTH / LABEL','Разделить тип хранения, разбор строки, отображение и справочник пользовательского формата.','PUT с пользовательским форматом требует экспорт формата; CAST не является общей заменой.','data'),
 ('runtime.boundary','RUN / QUIT','Зафиксировать конец шага/процедуры; RUN внутри PROC SQL не закрывает процедуру.','RUN CANCEL, процедуры с RUN-группами и пользовательские макрограницы учитываются отдельно.','statements'),
 ('unknown.construct','Новая конструкция','Сохранить исходный диапазон и блокировку; добавить разбор и регрессионный пример до генерации.','Для этой конструкции ещё нет адресного правила.','statements'),
]
CATALOG = {i: {'id':i,'label':label,'lowering':lowering,'requires':requires,'documentation':SOURCES[source]}
           for i,label,lowering,requires,source in RECIPES}

FEATURES = [
 (r'\b(?:put|input)\s*\(', 'function.format', 'PUT / INPUT / пользовательский формат',
  'Числовой/строковый формат → отдельная функция форматирования или разбора; пользовательский формат → таблица соответствий и JOIN с правилами диапазонов.',
  'Экспорт форматов (включая OTHER, MULTILABEL, интервалы), длины и правила округления. Идентификаторы нельзя преобразовывать через число без проверки ведущих нулей.'),
 (r'\bprxmatch\s*\(', 'function.regex', 'PRXMATCH',
  'Проверка PRXMATCH(...) > 0 → проверка совпадения; возвращаемая позиция требует отдельной функции. PostgreSQL/Greenplum и ClickHouse используют разные regex-движки.',
  'Проверить флаги, lookaround/backreferences, Unicode, пустую строку и no-match; тестировать каждый шаблон на целевом движке.'),
 (r'\b(?:cats|catx|compress)\s*\(', 'function.strings', 'CATS / CATX / COMPRESS',
  'CATS обрезает края аргументов, CATX также пропускает пустые аргументы; COMPRESS удаляет указанные символы. Одноаргументный COMPRESS уже имеет ограниченное правило удаления пробелов.',
  'Типы аргументов, автоматическое числовое форматирование SAS, missing, пробелы, длина результата и модификаторы COMPRESS.'),
 (r'\b(?:datepart|today|datetime|date|intnx|year|month|mdy)\s*\(', 'function.calendar', 'Даты / INTNX / DATEPART',
  'Разделить SAS days/seconds и DATE/TIMESTAMP. INTNX → календарный сдвиг плюс alignment; TODAY/DATETIME → зафиксированное время запуска.',
  'Единицы хранения, календарь, timezone, начало/конец интервала, leap day. Для YEAR/MONTH/DATEPART уже есть правило при явном native-контракте.'),
 (r'\b(?:sum|mean|min|max|n|nmiss)\s*\(', 'function.statistics', 'Статистические функции',
  'В PROC SQL один аргумент → агрегат; несколько → построчное выражение с политикой missing. В DATA STEP это другой контекст, агрегатное правило применять нельзя.',
  'Различать SQL и DATA, типы аргументов, пустые наборы, all-missing; отдельно выявлять aggregate remerge.'),
 (r'\b(?:is\s+(?:not\s+)?missing|not\s+in\s*\(\s*\.|(?:eq|ne)\s*\.)|[=<>]\s*\.(?!\w)', 'expression.missing', 'Обычные и специальные SAS missing',
  'При сохранении только обычного числового missing сравнение с точкой может стать IS NULL/IS NOT NULL; специальные .A–.Z требуют отдельного кодирования и порядка сравнения.',
  'Числовой или строковый тип, наличие специальных missing, различие пустой строки/пробелов/NULL; обычное <> в SQL не включает NULL.'),
 (r'\b(?:format|informat|length|label)\s*=', 'sql.attributes', 'Атрибуты колонок в PROC SQL',
  'Извлечь атрибуты в каталог результата; LENGTH влияет на хранение/усечение, FORMAT — на отображение или последующий PUT.',
  'Нельзя просто удалить атрибуты из SQL: нужны типы, длины и все дальнейшие чтения результата.'),
 (r'\b(?:bulkload|dbcommit|insertbuff|compress)\s*=', 'load.options', 'Опции физической загрузки',
  'Сохранить как параметры адаптера загрузки: пакетность, транзакции, staging и публикация.',
  'Целевой движок/версия, поддержка bulk load, политика частичного сбоя и повторного запуска; опции не входят в SELECT.'),
 (r'\bcalculated\b', 'sql.calculated', 'CALCULATED',
  'Подставить предыдущее выражение псевдонима через AST; при необходимости выделить дополнительный SELECT.',
  'Запрещены циклы, несуществующие/неоднозначные псевдонимы, агрегат в WHERE; простые предыдущие псевдонимы уже поддержаны.'),
]
for _, ident, label, lowering, requires in FEATURES:
    CATALOG[ident]={'id':ident,'label':label,'lowering':lowering,'requires':requires,
                   'documentation':SOURCES['statements']}
for ident, url in {
    'function.format': SAS+'lefunctionsref/63354/HTML/default/n0mlfb88dkhbmun1x08qbh5xbs7e.htm',
    'function.regex': SAS+'lefunctionsref/63354/HTML/default/n0bj9p4401w3n9n1gmv6tfshit9m.htm',
    'function.strings': SAS+'lefunctionsref/63354/HTML/default/n0p7wxtk0hvn83n1pveisbcp2ae9.htm',
    'function.calendar': SAS+'lefunctionsref/63354/HTML/default/p10v3sa3i4kfxfn1sovhi5xzxh8n.htm',
    'function.statistics': SAS+'sqlproc/63043/HTML/default/n123fsko39j44pn16zlt087e1m2h.htm',
    'sql.attributes': SOURCES['data'],
    'sql.calculated': SAS+'sqlproc/63043/HTML/default/n0jxri3yohqdwbn1slekmsc882gz.htm',
}.items():
    CATALOG[ident]['documentation']=url


def feature_cases(masked):
    result=[dict(CATALOG[ident]) for pattern,ident,*_ in FEATURES if re.search(pattern,masked,re.I)]
    if '&' in masked:result.append(dict(CATALOG['macro.parameters']))
    if re.search(r'\b(?:keep|drop|rename)\s*=',masked,re.I):result.append(dict(CATALOG['data.projection']))
    return result


def recipe(masked, owner):
    text = masked.strip()
    patterns = [
        (r'%quitrun\b','runtime.quitrun'),
        (r'(?:%?abort|endsas)\b','runtime.abort'),
        (r'%(?:if|else|do|end)\b','macro.branch'),
        (r'%let\b','macro.parameters'),
        (r'%put\b','runtime.log'),
        (r'%','macro.definition'),
        (r'(?:run|quit)\b','runtime.boundary'),
        (r'proc\s+sql\b','sql.query'),
        (r'proc\s+sort\b','proc.sort'),
        (r'proc\s+transpose\b|(?:id|var)\b','proc.transpose'),
        (r'proc\s+datasets\b','proc.datasets'),
        (r'(?:libname|filename|options|ods|title|footnote)\b','runtime.environment'),
        (r'(?:merge|update|modify)\b','data.merge'),
        (r'(?:by|retain)\b|.*\b(?:first|last)\.','data.by_state'),
        (r'(?:data|set)\b','data.pipeline'),
        (r'(?:keep|drop|rename)\b','data.projection'),
        (r'(?:if|else)\b.*\bthen\b|else\b','data.branch'),
        (r'(?:where|if)\b','data.filter'),
        (r'call\s+symputx?\b','data.symput'),
        (r'(?:input|infile|cards4?|datalines4?)\b','data.inline'),
        (r'(?:format|informat|length|label|attrib)\b','data.attributes'),
        (r'(?:file|put)\b','runtime.log'),
    ]
    if owner.get('kind') == 'proc_sql' and re.match(r'(?:select|create)\b',text,re.I):
        return CATALOG['sql.query']
    if owner.get('procedure') == 'datasets' and re.match(r'delete\b',text,re.I):
        return CATALOG['proc.datasets']
    for pattern, ident in patterns:
        if re.match(pattern,text,re.I|re.S):
            return CATALOG[ident]
    if owner.get('kind') == 'data' and '=' in text:
        return CATALOG['data.assign']
    return CATALOG['unknown.construct']


def annotate(operations, items, owners, statement_id):
    for n, item in enumerate(items):
        ident = statement_id(n, item)
        case = recipe(item['masked'], owners[ident])
        op = operations[ident]
        op['migration_case'] = dict(case)
        op['migration_features'] = feature_cases(item['masked'])
        if op['status']=='unsupported' and op.get('family')=='unclassified' and not op.get('reason'):
            op['reason'] = case['label'] + ': ' + case['requires']
