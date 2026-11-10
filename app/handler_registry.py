"""Allowlisted bindings between embedded rule IDs and capabilities.

The existing compiler still has several focused handlers rather than one
generic dispatcher.  The registry gives those handlers stable public IDs and
maps their emitted capability tags to the machine-readable catalog bundled
with the backend. No external rule source can supply an import path or callable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HandlerSpec:
    key: str
    revision: int
    rule_ids: tuple[str, ...]
    legacy_tags: tuple[str, ...]
    node_kinds: tuple[str, ...]
    targets: tuple[str, ...] = ("greenplum", "postgresql", "clickhouse")


_SPECS = (
    HandlerSpec("sas.boundary.omit.v1", 1, ("B01", "B02", "B03"), ("sas.syntax.omitted",), ("boundary", "proc_sql")),
    HandlerSpec("sas.storage.v1", 1, ("T01", "T02", "T03", "T04"), (), ("dataset_write",)),
    HandlerSpec("sas.proc_sql.select.v1", 1, ("Q01", "Q02", "Q04", "Q05", "Q06", "Q07", "Q10", "Q12", "Q13", "Q15"), ("proc_sql.select",), ("query",)),
    HandlerSpec("sas.name_literal.v1", 1, ("Q03",), ("sas.name_literal",), ("query", "expression")),
    HandlerSpec("sas.in_list.v1", 1, ("Q08", "Q09"), ("sas.in_list", "data.in.ordinary_missing"), ("expression",)),
    HandlerSpec("sas.calculated.v1", 1, ("Q11",), ("sas.calculated.expand",), ("query",)),
    HandlerSpec("sas.projection_attributes.v1", 1, ("Q16", "M11"), ("sas.projection.display_attributes",), ("query", "data_step")),
    HandlerSpec("sas.data.set.v2", 2, ("D01", "D02", "D03", "D04", "D05", "D06"), ("data.set.single", "data.set.union_all", "data.set.prefix_catalog"), ("data_step",)),
    HandlerSpec("sas.data.projection.v2", 2, ("D07", "D08", "D09", "D27"), ("data.keep.explicit", "data.rename.input", "data.rename.output"), ("data_step",)),
    HandlerSpec("sas.data.program.v2", 2, ("D10", "D11", "D12", "D13", "D14", "D15", "D25"), ("data.program.sequential", "data.if_else.case", "data.filter.sequential", "data.if.subsetting"), ("data_step",)),
    HandlerSpec("sas.data.merge.v1", 1, ("D19",), ("data.merge.positional_two_streams",), ("data_step",)),
    HandlerSpec("sas.data.by.v1", 1, ("D20", "D24"), ("data.by.first_last.ordered",), ("data_step",)),
    HandlerSpec("sas.data.accumulator.v1", 1, ("D21",), ("data.sum_statement.window",), ("data_step",)),
    HandlerSpec("sas.proc_sort.v1", 1, ("P01", "P02"), ("proc.sort.order_contract", "proc.sort.nodupkey.first"), ("procedure",)),
    HandlerSpec("sas.statistics.v2", 2, ("F01", "F02", "F03", "F31"), ("data.statistics.scalar", "sas.sum.aggregate", "sas.sum.row_missing", "sas.mean.aggregate", "sas.mean.row_missing", "sas.min.aggregate", "sas.min.row_missing", "sas.max.aggregate", "sas.max.row_missing", "sas.n.aggregate", "sas.n.row_missing", "sas.nmiss.aggregate", "sas.nmiss.row_missing"), ("function_call", "expression")),
    HandlerSpec("sas.put.v1", 1, ("F04", "F06", "F26"), ("sas.put.integer_width", "sas.put.custom_exact", "sas.put.date9", "sas.put.yymmn6"), ("function_call",)),
    HandlerSpec("sas.input.v1", 1, ("F05",), ("sas.input.numeric_literal",), ("function_call",)),
    HandlerSpec("sas.strings.v2", 2, ("F07", "F08", "F09", "F10", "F11", "F12", "F13"), ("sas.string.cat", "sas.string.cats", "sas.string.catx", "sas.string.strip", "sas.string.trim", "sas.string.left", "sas.string.substr", "sas.string.tranwrd", "sas.compress.blanks", "sas.compress.characters", "sas.lowcase", "sas.upcase"), ("function_call",)),
    HandlerSpec("sas.dates.v2", 2, ("F18", "F19", "F23"), ("sas.datepart.native", "sas.year.native", "sas.month.native", "sas.intnx.calendar.b", "sas.intnx.calendar.e", "sas.intnx.calendar.s", "sas.date_literal.native"), ("function_call", "literal")),
    HandlerSpec("sas.comparisons.v1", 1, ("F17", "F32", "F33", "F34", "F35"), ("sas.comparison_operator", "data.comparison.ordinary_missing", "data.missing.ordinary"), ("expression",)),
    HandlerSpec("sas.functions.typed.v1", 1, ("F16", "F24", "F25", "F30", "F38"), ("sas.coalesce.typed", "sas.mod.numeric", "sas.floor.numeric", "sas.index.character", "sas.missing.typed", "sas.length.character", "sas.lengthn.character", "sas.lengthc.character"), ("function_call", "expression")),
    HandlerSpec("sas.regex.condition.v1", 1, ("F14",), ("sas.prxmatch.condition",), ("function_call", "expression")),
    HandlerSpec("sas.proc_sql.union.v1", 1, ("Q14",), ("proc_sql.union",), ("query",)),
    HandlerSpec("sas.proc.transpose.v1", 1, ("P03",), ("proc.transpose.conditional_aggregate",), ("procedure",)),
    HandlerSpec("sas.proc.append.v1", 1, ("P04",), ("proc.append.insert_select",), ("procedure",)),
)


class HandlerRegistry:
    def __init__(self, specs: tuple[HandlerSpec, ...] = _SPECS):
        self._by_key = {spec.key: spec for spec in specs}
        self._by_rule: dict[str, HandlerSpec] = {}
        self._by_tag: dict[str, HandlerSpec] = {}
        for spec in specs:
            for rule_id in spec.rule_ids:
                if rule_id in self._by_rule:
                    raise ValueError(f"Duplicate handler binding for {rule_id}")
                self._by_rule[rule_id] = spec
            for tag in spec.legacy_tags:
                if tag in self._by_tag:
                    raise ValueError(f"Duplicate capability binding for {tag}")
                self._by_tag[tag] = spec

    def for_rule(self, rule_id: str) -> HandlerSpec | None:
        return self._by_rule.get(rule_id)

    def for_tag(self, tag: str) -> HandlerSpec | None:
        return self._by_tag.get(tag)

    def trace(self, tags: list[str] | tuple[str, ...]) -> list[dict]:
        result: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for tag in tags:
            spec = self.for_tag(tag)
            if spec is None:
                result.append({"capability": tag, "status": "internal_only", "rule_ids": [], "handler_key": None})
                continue
            key = (tag, spec.key)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "capability": tag,
                    "status": "bound",
                    "rule_ids": list(spec.rule_ids),
                    "handler_key": spec.key,
                    "handler_revision": spec.revision,
                }
            )
        return result

    def manifest(self) -> list[dict]:
        return [
            {
                "key": spec.key,
                "revision": spec.revision,
                "rule_ids": list(spec.rule_ids),
                "legacy_tags": list(spec.legacy_tags),
                "node_kinds": list(spec.node_kinds),
                "targets": list(spec.targets),
            }
            for spec in sorted(self._by_key.values(), key=lambda value: value.key)
        ]


REGISTRY = HandlerRegistry()


def trace_rules(tags: list[str] | tuple[str, ...]) -> list[dict]:
    return REGISTRY.trace(tags)

