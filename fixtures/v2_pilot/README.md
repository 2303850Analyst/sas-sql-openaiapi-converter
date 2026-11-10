# Учебная проверка конвертации

Все имена, данные и пароли здесь вымышленные. `Quality_Pilot.egp` предназначен для проверки нашего импортера; открытие в нативном Enterprise Guide не подтверждено. Эталон `monthly_kpi.csv` задан независимо от модели, но исполнением SAS не получен.

В примере 17 исходных наблюдений и отдельный набор с 18-й записью ровно на нижней границе периода. Сценарий содержит макропараметры, макрос с вызовом, IF/ELSE, два OUTPUT, даты, сортировку, FIRST., NODUPKEY, MERGE BY и агрегации.

## Повторить без API

Команды выполняются из корня комплекта разработки в PowerShell. Нужен Python 3.13 x64. Зависимости устанавливаются только из вложенного wheelhouse:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip --isolated install --no-index --only-binary=:all: --require-hashes --find-links vendor/wheelhouse -r packaging_v2/requirements-test.lock
```

Тестовая PostgreSQL запускается отдельно. Docker должен уже иметь образ с хешем из `compose.yaml`; `pull_policy: never` запрещает автоматическое скачивание. Образ PostgreSQL и Docker не входят в EXE и не нужны для просмотра сохранённых результатов. В изолированной сети образ передаётся отдельно одобренным способом.

```powershell
docker compose -f fixtures/v2_pilot/compose.yaml up -d --wait
$env:SAS_MIGRATION_DB_PASSWORD='synthetic_local_validation'
$taskReplay=Join-Path $env:TEMP ('sas-v2-replay-'+[guid]::NewGuid().ToString('N'))
.venv/Scripts/python -m tools.run_v2_pilot --data $taskReplay --config fixtures/v2_pilot/database-only.json --candidate-sql evidence/v2-pipeline-verified-20261106.sql --execute --report evidence/replayed-pipeline.json
.venv/Scripts/python -m tools.check_v2_mutants --data $taskReplay --config fixtures/v2_pilot/database-only.json --report evidence/replayed-mutants.json
```

Ожидается `REFERENCE_MATCHED`, восемь выполненных SQL-шагов и совпадение трёх таблиц: 12, 3 и 5 строк. Расход API этого повтора равен нулю. SQL берётся из сохранённого ответа модели; повтор не является новой генерацией.

Восемь проверок изменённого SQL включают правильный вариант, пропущенную калибровку, лишние дубли, неверный аргумент макроса и граничные условия. Базовый набор не отличает `>=` от `>` на нижней границе; дополнительный набор эту ошибку обнаруживает. Отчёт сохраняет оба результата.

```powershell
$env:SAS_V2_TEST_POSTGRES='1'
$env:SAS_V2_PG_PORT='55438'
.venv/Scripts/python -m pytest tests_v2 -q
docker compose -f fixtures/v2_pilot/compose.yaml stop
```

Здесь 37 проверок. Учебная база использует отдельный порт 55438 и ограниченную роль `migration_validator`. Приложение выполняет тестовую копию SQL с временными таблицами и завершает её откатом. Исходные назначения сохранены в скачиваемом SQL. Гейт 2 остаётся закрытым: синтетическое совпадение не доказывает эквивалентность реальному SAS.
