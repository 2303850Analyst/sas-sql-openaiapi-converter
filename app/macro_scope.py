"""Bound unknown macro branches without executing or expanding the macro language."""
import re


def blocked_statements(items):
    blocked = []
    depth = 0
    definition = 0
    unbounded = False
    for item in items:
        text = item['masked'].strip()
        if re.match(r'%macro\b', text, re.I):
            definition += 1
        if definition:
            blocked.append(True)
            if re.match(r'%mend\b', text, re.I):
                definition -= 1
            continue
        control = bool(re.match(r'%(?:if|else|do|end|include)\b', text, re.I))
        inside = depth > 0
        if re.match(r'%include\b', text, re.I):
            unbounded = True  # unknown included text can open an arbitrary SAS step
        if re.match(r'%(?:if|else)\b', text, re.I):
            if not re.search(r'%do\b', text, re.I):
                unbounded = True  # single-statement generated code is not parsed here
        if re.match(r'%end\b', text, re.I):
            if depth:
                depth -= 1
            else:
                unbounded = True
        elif re.match(r'%do\b', text, re.I) or (control and re.search(r'%do\b', text, re.I)):
            depth += 1
        blocked.append(inside or control or unbounded)
    return blocked
