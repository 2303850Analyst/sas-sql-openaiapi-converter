"""Dataset lists with nested options; no macro evaluation or guessed identifiers."""
import re

NAME = r"[A-Za-z_&][\w&.]*:?"


def dataset_list(text):
    result, pos = [], 0
    while pos < len(text):
        m = re.match(r"[\s,;]+", text[pos:])
        if m:
            pos += m.end()
            continue
        m = re.match(NAME, text[pos:])
        if not m:
            return result, False
        result.append(m[0])
        pos += m.end()
        while pos < len(text) and text[pos].isspace(): pos += 1
        if pos < len(text) and text[pos] == "(":
            depth, pos = 1, pos + 1
            while pos < len(text) and depth:
                if text[pos] == "(": depth += 1
                elif text[pos] == ")": depth -= 1
                pos += 1
            if depth: return result, False
        if pos < len(text) and text[pos] == "/":
            return result, False
    return result, True
