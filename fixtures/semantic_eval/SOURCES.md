# Основания семантических проверок

Проверено 6 ноября 2026 года. Все исходники этого набора синтетические. Документация объясняет ожидаемое поведение; она не заменяет независимый запуск SAS.

| Проверка | Официальное основание |
|---|---|
| Числовые и символьные пропуски | [SAS MISSING](https://support.sas.com/documentation/cdl/en/lefunctionsref/63354/HTML/default/p06ybg84o0asa4n17l9ieauk58hb.htm) |
| Пропуск меньше любого числа, отдельные специальные пропуски | [SAS Order of Missing Values](https://support.sas.com/documentation/cdl/en/lrcon/62955/HTML/default/a000989180.htm) |
| Порядок NULL в SQL | [PostgreSQL 17 ORDER BY](https://www.postgresql.org/docs/17/queries-order.html) |
| Сравнение двух NULL | [PostgreSQL 17 comparison predicates](https://www.postgresql.org/docs/17/functions-comparison.html) |
| Построчный SUM отличается от сложения | [SAS SUM function](https://support.sas.com/documentation/cdl/en/lefunctionsref/63354/HTML/default/n0zxive1z1ctqin12w06c85jfigd.htm) |
| Накопление с начальным нулём | [SAS sum statement](https://support.sas.com/documentation/cdl/en/lestmtsref/63323/HTML/default/n1dfiqj146yi2cn1maeju9wo7ijs.htm) |
| Число итераций MERGE, отсутствие декартова произведения | [SAS MERGE statement](https://support.sas.com/documentation/cdl/en/lestmtsref/63323/HTML/default/n1i8w2bwu1fn5kn1gpxj18xttbb0.htm) |
| Перезапись общих полей, в том числе пропуском | [SAS Usage Note 41064](https://support.sas.com/kb/41/064.html) |
| Очередь условного LAG | [SAS LAG function](https://support.sas.com/documentation/cdl/en/lefunctionsref/63354/HTML/default/n0l66p5oqex1f2n1quuopdvtcjqb.htm) |
| Длина символьной переменной и усечение | [SAS Setting the Length of Character Variables](https://support.sas.com/documentation/cdl/en/basess/58133/HTML/default/a001336069.htm) |

Числовые пропуски в положительных сценариях представлены как SQL NULL. Специальные теги `.A` и другие не смешиваются с обычным пропуском в эталоне: сценарий с потерянными тегами требует отказа.

Для строк принята транспортная модель VARCHAR: NULL, пустая строка и ASCII-пробелы эквивалентны при проверке пропуска; конечные ASCII-пробелы игнорируются при сравнении SAS-значений. Исходные значения промежуточных таблиц сохраняются в транспортном представлении. Это не побайтовая модель фиксированного буфера SAS. Табуляция и NBSP включены в проверки как отдельные символы.

Проверка LENGTH охватывает ASCII-литерал. Кодировка, многобайтовое усечение, локаль сортировки и национальные форматы остаются отдельными задачами. Общая совместимость строковых форматов этим тестом не подтверждается.
