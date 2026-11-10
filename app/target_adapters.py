"""Target SQL adapters behind one rendering/validation interface."""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass(frozen=True)
class TargetCapabilities:
    dialect: str
    temporary_tables: bool
    transactional_ddl: bool
    distribution_policy: bool
    append_insert: bool
    physical_row_order: bool = False


class TargetAdapter:
    name = ""
    capabilities = TargetCapabilities("postgres", True, True, False, True)

    def prepare(self, tree: exp.Expression) -> exp.Expression:
        from .storage_rules import target_tree
        result = target_tree(tree)
        for identifier in result.find_all(exp.Identifier):
            if not identifier.quoted:
                identifier.set("this", identifier.this.lower())
        return result

    def render(self, tree: exp.Expression) -> str:
        prepared = self.prepare(tree)
        sql = prepared.sql(
            dialect=self.capabilities.dialect,
            identify=True,
            pretty=True,
            unsupported_level=sqlglot.ErrorLevel.RAISE,
        ) + ";"
        parsed = sqlglot.parse(sql, read=self.capabilities.dialect, error_level=sqlglot.ErrorLevel.RAISE)
        if len(parsed) != 1 or parsed[0] is None or parsed[0].find(exp.Command):
            raise ValueError("Результат не прошёл обратный синтаксический разбор целевого диалекта")
        return sql


class PostgreSqlAdapter(TargetAdapter):
    name = "postgresql"
    capabilities = TargetCapabilities("postgres", True, True, False, True)


class GreenplumAdapter(TargetAdapter):
    name = "greenplum"
    capabilities = TargetCapabilities("postgres", True, True, True, True)


class ClickHouseAdapter(TargetAdapter):
    name = "clickhouse"
    capabilities = TargetCapabilities("clickhouse", True, False, False, True)


_ADAPTERS = {adapter.name: adapter for adapter in (PostgreSqlAdapter(), GreenplumAdapter(), ClickHouseAdapter())}


def adapter_for(target: str) -> TargetAdapter:
    try:
        return _ADAPTERS[target]
    except KeyError:
        raise ValueError("Неизвестная целевая СУБД") from None
