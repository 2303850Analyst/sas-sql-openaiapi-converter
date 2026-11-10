"""Bundled public dialect facts; no documentation/network fetch at runtime."""


def facts(target):
    common={'execution_evidence':'not_run','session':'one uninterrupted connection per scenario; predecessor TEMP tables remain visible'}
    if target=='clickhouse':
        return {**common,'temporary_ddl':'CREATE TEMPORARY TABLE name ENGINE=Memory AS SELECT ...',
            'temporary_namespace':'Temporary tables have no database qualifier. An unqualified temporary name resolves to the temporary table in this same session.',
            'temporary_lifetime':'Ends with the session; default engine is Memory.',
            'reference':'https://clickhouse.com/docs/reference/statements/create/table/temporary-table'}
    return {**common,'temporary_ddl':'CREATE TEMP TABLE name AS SELECT ...',
        'temporary_namespace':'Temporary predecessor names are unqualified; WORK.x is mapped to approved TEMP x.',
        'reference':'https://www.postgresql.org/docs/current/sql-createtableas.html' if target=='postgresql' else 'Greenplum PostgreSQL-compatible CTAS subset; target execution remains unchecked'}
