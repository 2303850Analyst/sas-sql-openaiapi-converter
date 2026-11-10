"""Deterministic SAS statement lexer used by analysis and compilation.

This module owns statement boundaries only.  It deliberately does not try to
execute macros or interpret DATA/PROC semantics.  Keeping the lexer separate
prevents the SQL compiler and the semantic model from splitting SAS source in
different ways.
"""
from __future__ import annotations

import re

from tools.inspect_sources import LexicalError, mask_noncode


def without_block_comments(text: str) -> str:
    """Remove comments for lowering while preserving quoted literals and line spans."""
    token = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|/\*.*?\*/", re.S)
    def replace(match: re.Match[str]) -> str:
        value = match.group()
        return value if not value.startswith("/*") else "".join(c if c in "\r\n" else " " for c in value)
    return token.sub(replace, text)


def lex_statements(source: str) -> tuple[list[dict], int | None]:
    """Return SAS statements with stable source spans and an optional cutoff.

    Semicolons inside comments, quoted literals and inline data do not split a
    statement.  A lexical failure produces a parsed prefix plus an explicit
    cutoff instead of silently discarding the tail.
    """
    inline_regions: list[tuple[int, int, str]] = []
    try:
        masked = mask_noncode(source, sas=True, inline_regions=inline_regions)
        cutoff = None
    except LexicalError as error:
        inline_regions = []
        masked = mask_noncode(source[: error.offset], sas=True, inline_regions=inline_regions)
        cutoff = source.count("\n", 0, error.offset) + 1

    # Compile-time macro quotes can contain statement delimiters and macro headers.
    # Preserve the whole invocation for the macro layer, hiding only its payload
    # from structural consumers. Percent-escaped parentheses do not affect depth.
    chars = list(masked)
    pos = 0
    quote_pattern = re.compile(r"%(?:nrstr|str)\s*\(", re.I)
    while pos < len(masked):
        match = quote_pattern.search(masked, pos)
        if not match:
            masked = "".join(chars)
            break
        pos = match.start()
        opening = match.end() - 1
        depth, end = 1, opening + 1
        while end < len(masked) and depth:
            if source[end] == "%" and end + 1 < len(source) and source[end + 1] in "()'\"":
                end += 2
                continue
            if source[end] == "(": depth += 1
            elif source[end] == ")": depth -= 1
            end += 1
        if depth:
            cutoff = source.count("\n", 0, pos) + 1
            masked = "".join(chars[:pos])
            break
        for i in range(opening + 1, end - 1):
            if chars[i] not in "\r\n": chars[i] = " "
        pos = end
    else:
        masked = "".join(chars)

    start = 0
    result: list[dict] = []
    for match in re.finditer(";", masked):
        segment = masked[start : match.start()]
        if segment.strip():
            offset = start + len(segment) - len(segment.lstrip())
            result.append(
                {
                    "text": source[offset : match.end()],
                    "masked": masked[offset : match.end()],
                    "line": source.count("\n", 0, offset) + 1,
                    "end_line": source.count("\n", 0, match.end()) + 1,
                    "offset": offset,
                    "end_offset": match.end(),
                }
            )
        start = match.end()
    if masked[start:].strip():
        cutoff = source.count("\n", 0, start) + 1

    for begin, end, marker in inline_regions:
        result.append(
            {
                "text": marker,
                "masked": marker,
                "inline_data": True,
                "line": source.count("\n", 0, begin) + 1,
                "end_line": source.count("\n", 0, end) + 1,
                "offset": begin,
                "end_offset": end,
            }
        )
    result.sort(key=lambda item: item["offset"])
    return result, cutoff

