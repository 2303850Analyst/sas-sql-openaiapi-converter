# SAS → SQL V2

Локальное Windows-приложение для переноса проектов SAS Enterprise Guide в SQL для PostgreSQL, Greenplum и ClickHouse. V2 разбирает EGP, сохраняет согласованные источники и типы, генерирует SQL через явно настроенный OpenAI-compatible API и проверяет выходы перед выпуском. Модель и подключение к СУБД по умолчанию отключены.

- [Запуск пробной сборки](START_HERE_RU.md) и [ограничения](KNOWN_ISSUES.md)
- [Навигация по проекту](docs/START_HERE.md)
- [Архитектура V2](docs/ARCHITECTURE_V2.md) и [проверка результатов](TESTIFIER.md)
- [Синтетические проекты](expansion/corpus) и [результаты последних испытаний](expansion/results)
- [Сборка и локальная проверка](docs/REPRODUCE.md)

## Запуск и сборка

Готовый Windows EXE доступен в Releases. Для сборки из исходников на Windows x64 нужен Python 3.13. Проверенные wheels для автономной сборки уже лежат в `vendor/wheelhouse`.

```powershell
.\Build-V2.ps1
```

Для запуска из исходников и тестов используйте отдельное виртуальное окружение.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --require-hashes --find-links vendor/wheelhouse -r packaging_v2/requirements-test.lock
.\.venv\Scripts\python.exe -m pytest tests_v2 -q
.\.venv\Scripts\python.exe -m migration_v2 --port 18779
```

Для просмотра импорта подойдёт `expansion/corpus/ex_observatory.egp`. Настройки провайдера и ключ хранятся локально. Поддерживаемые параметры заданы в `migration_v2/runtime.py`, хранение ключей через Windows DPAPI - в `migration_v2/secrets_store.py`.

## Доказательства

В последнем синтетическом прогоне три проекта прошли оба гейта. PostgreSQL дал 23/23 совпадения выполненных наборов, ClickHouse - 11/11 на финальных ревизиях с исполняемым SQL. Матрица отдельно показывает заблокированные профили. Greenplum проверен на уровне кода и ревью. Выполнение на Greenplum и сравнение с нативным SAS остаются открытыми.

Тестовые пароли в лабораторных Docker-конфигурациях относятся к вымышленным локальным стендам. Исходники, примеры, SQL и выборочные артефакты испытаний пригодны для синтетического воспроизведения.
