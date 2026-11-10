"""Resolve explicitly attached literal INCLUDE references against one source revision."""
import re

from app.sas_lexer import lex_statements
from .contracts import digest


def include_index(attachments, parameters):
    key = digest(parameters)
    return {(item["parent_source_id"], item["offset"]): item
            for item in attachments if item["parameters_hash"] == key}


def attached_include(index, parent, statement):
    item = index.get((parent["id"], statement["offset"]))
    return item if item and item["parent_sha256"] == parent["sha256"] else None


def inline_includes(vault, index, source, stack=(), budget=None):
    """Analysis-only view. Keep the original statement when a reference cannot resolve."""
    text = vault.text(source)
    if budget is None:
        budget = [4_000_000]
    if not index or len(stack) >= 16 or source["sha256"] in stack:
        return text
    statements, _ = lex_statements(text)
    parts, cursor = [], 0
    for statement in statements:
        if not re.match(r"\s*%include\b", statement["masked"], re.I):
            continue
        child = attached_include(index, source, statement)
        if not child or child["sha256"] in (*stack, source["sha256"]):
            continue
        size = len(vault.text(child))
        if size > budget[0]:
            continue
        budget[0] -= size
        parts.extend((text[cursor:statement["offset"]], "\n",
                      inline_includes(vault, index, child, (*stack, source["sha256"]), budget), "\n"))
        cursor = statement["end_offset"]
    parts.append(text[cursor:])
    return "".join(parts)
