"""Content identity for code-review evidence, identical in source and frozen builds."""
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
import sys

from .contracts import digest


RULE_SOURCES = (
    "migration_v2/ai_pipeline.py", "migration_v2/clickhouse_nulls.py", "migration_v2/linker.py", "migration_v2/verification.py",
    "migration_v2/context.py", "migration_v2/contracts.py", "migration_v2/dataset_syntax.py",
    "migration_v2/destinations.py", "migration_v2/greenplum.py", "migration_v2/greenplum_execution.py", "migration_v2/dialect_adapter.py", "migration_v2/dialect_facts.py",
    "migration_v2/includes.py", "migration_v2/numeric_rules.py", "migration_v2/semantic_guidance.py", "migration_v2/task_planner.py",
    "app/sas_lexer.py", "app/sas_blocks.py", "tools/inspect_sources.py",
)


def source_identity(paths):
    # Neither the installation directory nor a display filename contributes.
    return [digest(path.read_text(encoding="utf-8").encode("utf-8")) for path in paths]


@lru_cache(maxsize=1)
def rules_fingerprint():
    root = Path(sys._MEIPASS) / "rule_sources" if getattr(sys,"frozen",False) else Path(__file__).resolve().parents[1]
    return digest({"code":source_identity(root / name for name in RULE_SOURCES),
                   "sqlglot":version("sqlglot"),"pydantic":version("pydantic"),
                   "schema":1})
