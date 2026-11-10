"""Bounded, deterministic SAS macro partial evaluator.

The processor expands only the macro constructs implemented here.  It never
executes SAS, reads files, connects to a database, or treats an unknown value
as false.  Unsupported or runtime-dependent constructs stay in the expanded
source and produce explicit requirements.
"""
from __future__ import annotations

import ast
import calendar
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .ir import canonical_hash
from .sas_lexer import lex_statements


class _Unknown:
    def __bool__(self):  # pragma: no cover - prevents accidental truth tests
        raise TypeError("Unknown macro value cannot be used as boolean")


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class MacroRequirement:
    code: str
    message: str
    program_id: str
    line: int
    macro: str = ""
    blocking: bool = True

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "program_id": self.program_id,
            "line": self.line,
            "macro": self.macro,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class MacroTrace:
    kind: str
    program_id: str
    line: int
    macro: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "program_id": self.program_id,
            "line": self.line,
            "macro": self.macro,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MacroParameter:
    name: str
    default: str | None = None


@dataclass(frozen=True)
class MacroDefinition:
    name: str
    parameters: tuple[MacroParameter, ...]
    body: tuple[dict, ...]
    program_id: str
    line: int
    revision: int
    runtime_effect: bool = False


@dataclass
class MacroFrame:
    name: str
    locals: dict[str, str] = field(default_factory=dict)
    declared_local: set[str] = field(default_factory=set)
    call_line: int = 0
    definition_line: int = 0
    parent: MacroFrame | None = None


@dataclass(frozen=True)
class ExpandedProgram:
    program_id: str
    source: str
    source_hash: str
    expanded_hash: str
    requirements: tuple[dict, ...]
    trace: tuple[dict, ...]
    source_map: tuple[dict, ...]


def _canonical_dataset(value: str) -> str:
    name = value.strip().casefold()
    return name[5:] if name.startswith("work.") else name


def _safe_symbol_value(name: str, value: str) -> dict:
    hidden = bool(re.search(r"password|passwd|pwd|token|secret|userid|login|mail", name, re.I))
    safe = not hidden and len(value) <= 128 and not any(ord(char) < 32 for char in value)
    return {
        "name": name,
        "status": "known",
        "value": value if safe else None,
        "value_hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "redacted": not safe,
    }


class MacroProcessor:
    """Expand a sequence of programs while preserving SAS session macro state."""

    def __init__(
        self,
        *,
        run_date: date | None = None,
        catalog: dict | None = None,
        catalog_complete: bool = False,
        max_expanded_statements: int = 20_000,
        max_iterations: int = 1_000,
        max_call_depth: int = 30,
    ):
        self.run_date = run_date
        self.catalog = {_canonical_dataset(name): value for name, value in (catalog or {}).items()}
        self.catalog_complete = catalog_complete
        self.max_expanded_statements = max_expanded_statements
        self.max_iterations = max_iterations
        self.max_call_depth = max_call_depth
        self.globals: dict[str, str] = {}
        self.definitions: dict[str, MacroDefinition] = {}
        self.requirements: list[MacroRequirement] = []
        self.trace: list[MacroTrace] = []
        self._definition_revisions: dict[str, int] = {}
        self._program_id = ""
        self._emitted = 0
        self._source_map: list[dict] = []

    def seed(self, parameters: dict | None) -> None:
        for name, value in (parameters or {}).items():
            if isinstance(value, dict) and value.get("type") == "date":
                parsed = date.fromisoformat(value["value"])
                self.globals[name.casefold()] = str((parsed - date(1960, 1, 1)).days)
            else:
                self.globals[name.casefold()] = str(value)

    def public_symbols(self) -> list[dict]:
        return [_safe_symbol_value(name, value) for name, value in sorted(self.globals.items())]

    def state_hash(self) -> str:
        definitions = {
            name: {
                "program_id": definition.program_id,
                "line": definition.line,
                "revision": definition.revision,
                "parameters": [(parameter.name, parameter.default) for parameter in definition.parameters],
                "body_hash": canonical_hash([item["text"] for item in definition.body]),
                "runtime_effect": definition.runtime_effect,
            }
            for name, definition in sorted(self.definitions.items())
        }
        return canonical_hash({"globals": self.globals, "definitions": definitions})

    def expand_program(self, program_id: str, source: str, parameters: dict | None = None) -> ExpandedProgram:
        self._program_id = program_id
        self._source_map = []
        self.seed(parameters)
        items, cutoff = lex_statements(source)
        before_requirements = len(self.requirements)
        before_trace = len(self.trace)
        if cutoff is not None:
            self._require("macro_source_cutoff", "Макрораскрытие остановлено на лексически неразобранном хвосте", cutoff)
        output = self._execute(items, None, 0)
        expanded = "\n".join(output)
        return ExpandedProgram(
            program_id,
            expanded,
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
            hashlib.sha256(expanded.encode("utf-8")).hexdigest(),
            tuple(requirement.to_dict() for requirement in self.requirements[before_requirements:]),
            tuple(item.to_dict() for item in self.trace[before_trace:]),
            tuple(self._source_map),
        )

    def _require(self, code: str, message: str, line: int, macro: str = "", blocking: bool = True) -> None:
        key = (code, self._program_id, line, macro.casefold(), message)
        if any((value.code, value.program_id, value.line, value.macro.casefold(), value.message) == key for value in self.requirements):
            return
        self.requirements.append(MacroRequirement(code, message, self._program_id, line, macro, blocking))

    def _add_trace(self, kind: str, line: int, macro: str = "", detail: str = "") -> None:
        self.trace.append(MacroTrace(kind, self._program_id, line, macro, detail))

    def _emit(self, output: list[str], text: str, line: int, frame: MacroFrame | None = None) -> None:
        if not text.strip():
            return
        self._emitted += 1
        if self._emitted > self.max_expanded_statements:
            self._require("macro_expansion_limit", "Превышен лимит размера раскрытого макрокода", line)
            raise ValueError("Превышен лимит размера раскрытого макрокода")
        output.append(text)
        self._source_map.append({
            "expanded_statement": self._emitted,
            "program_id": self._program_id,
            "source_line": line,
            "macro": frame.name if frame else "",
            "macro_call_line": frame.call_line if frame else 0,
            "macro_definition_line": frame.definition_line if frame else 0,
        })

    def _execute(self, items: list[dict] | tuple[dict, ...], frame: MacroFrame | None, depth: int) -> list[str]:
        if depth > self.max_call_depth:
            line = items[0]["line"] if items else 1
            self._require("macro_call_depth", "Превышена допустимая глубина макровызовов", line, frame.name if frame else "")
            return [item["text"] for item in items]
        output: list[str] = []
        pos = 0
        items = list(items)
        while pos < len(items):
            item = items[pos]
            text = item["text"].strip()
            masked = item["masked"].strip()
            line = item["line"]

            if re.match(r"%macro\b", masked, re.I):
                end = self._find_mend(items, pos)
                if end is None:
                    self._require("macro_unclosed_definition", "Определение %MACRO не закрыто %MEND", line)
                    self._emit(output, item["text"], line, frame)
                    pos += 1
                    continue
                definition = self._definition(item, items[pos + 1 : end])
                self.definitions[definition.name] = definition
                self._add_trace("definition", line, definition.name, f"revision={definition.revision}")
                pos = end + 1
                continue

            if re.match(r"%mend\b", masked, re.I):
                self._require("macro_unmatched_mend", "%MEND без активного определения", line)
                self._emit(output, item["text"], line, frame)
                pos += 1
                continue

            if re.match(r"%global\b", masked, re.I):
                names = re.findall(r"[A-Za-z_]\w*", text[len("%global") :].rstrip(";"))
                for name in names:
                    self.globals.setdefault(name.casefold(), "")
                self._add_trace("scope", line, frame.name if frame else "", "global:" + ",".join(name.casefold() for name in names))
                pos += 1
                continue

            if re.match(r"%local\b", masked, re.I):
                if frame is None:
                    self._require("macro_local_outside_call", "%LOCAL допустим только во время выполнения макроса", line)
                    self._emit(output, item["text"], line, frame)
                else:
                    names = re.findall(r"[A-Za-z_]\w*", text[len("%local") :].rstrip(";"))
                    for name in names:
                        frame.declared_local.add(name.casefold())
                        frame.locals.setdefault(name.casefold(), "")
                    self._add_trace("scope", line, frame.name, "local:" + ",".join(name.casefold() for name in names))
                pos += 1
                continue

            if re.match(r"%let\b", masked, re.I):
                self._execute_let(text, frame, line)
                pos += 1
                continue

            if re.match(r"%if\b", masked, re.I):
                node, following = self._parse_if(items, pos)
                if node is None:
                    self._require("macro_if_syntax", "Не удалось определить границы %IF/%THEN/%ELSE", line, frame.name if frame else "")
                    self._emit(output, item["text"], line, frame)
                    pos += 1
                    continue
                output.extend(self._execute_if_node(node, frame, depth, items[pos:following]))
                pos = following
                continue

            if re.match(r"%do\b", masked, re.I):
                body, following = self._do_body(items, pos)
                if body is None:
                    self._require("macro_do_unclosed", "%DO не закрыт %END", line, frame.name if frame else "")
                    self._emit(output, item["text"], line, frame)
                    pos += 1
                    continue
                output.extend(self._execute_do(text, body, frame, depth, line))
                pos = following
                continue

            if re.match(r"%(?:end|else)\b", masked, re.I):
                self._require("macro_control_unmatched", "Управляющий оператор макроса находится вне ожидаемой области", line, frame.name if frame else "")
                self._emit(output, item["text"], line, frame)
                pos += 1
                continue

            if re.match(r"data\s+_null_\s*;", masked, re.I):
                end = self._find_data_run(items, pos)
                if end is not None and self._execute_data_null(items[pos : end + 1], frame, line):
                    pos = end + 1
                    continue

            if re.match(r"select\b", masked, re.I) and re.search(r"\binto\s+:", masked, re.I):
                handled, replacement = self._select_into(text, frame, line)
                if handled:
                    if replacement:
                        self._emit(output, replacement, line, frame)
                    pos += 1
                    continue

            call = re.fullmatch(r"%([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*;", text, re.I | re.S)
            if call and call[1].casefold() not in {"abort", "include", "put", "goto", "return"}:
                name = call[1].casefold()
                definition = self.definitions.get(name)
                if definition is not None:
                    if definition.runtime_effect:
                        self._require("macro_runtime_effect", "Макрос зависит от состояния исполнения или внешнего эффекта", line, name)
                        self._emit(output, item["text"], line, frame)
                    else:
                        child = self._call_frame(definition, call[2] or "", frame, line)
                        if child is None:
                            self._emit(output, item["text"], line, frame)
                        else:
                            self._add_trace("call", line, name, f"definition_revision={definition.revision}")
                            output.extend(self._execute(definition.body, child, depth + 1))
                    pos += 1
                    continue
                if masked.startswith("%"):
                    self._require("macro_unknown", "Определение макроса недоступно в выбранном сценарии", line, name)
                    self._emit(output, item["text"], line, frame)
                    pos += 1
                    continue

            expanded = self._expand_text(item["text"], frame, line)
            if expanded is UNKNOWN:
                self._emit(output, item["text"], line, frame)
            else:
                self._emit(output, str(expanded), line, frame)
            pos += 1
        return output

    def _select_into(self, text: str, frame: MacroFrame | None, line: int) -> tuple[bool, str]:
        """Partially evaluate literal INTO and classify data-dependent forms.

        SAS assigns INTO values after the SELECT executes. Only a SELECT with
        no FROM and one scalar literal is safe to evaluate without a database.
        All table, list and range forms stay in source with an explicit runtime
        requirement; no arbitrary first row is selected.
        """
        expanded = self._expand_text(text, frame, line)
        candidate = text if expanded is UNKNOWN else str(expanded)
        match = re.match(
            r"select\s+(.*?)\s+into\s+:([A-Za-z_]\w*)\s*(trimmed)?\s*(?:separated\s+by\s+(.+?))?\s*;\s*$",
            candidate,
            re.I | re.S,
        )
        if match and not re.search(r"\bfrom\b", match[1], re.I) and match[4] is None:
            expression = match[1].strip()
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", expression):
                value = expression
            else:
                literal = re.fullmatch(r"(['\"])(.*?)\1", expression, re.S)
                if not literal:
                    self._require("sql_into_runtime_required", "SELECT INTO зависит от выполнения SQL или неподдержанного выражения", line, frame.name if frame else "")
                    return True, candidate
                value = literal[2]
            if match[3]:
                value = value.strip()
            self._assign(match[2], value, frame)
            self._add_trace("sql_into", line, frame.name if frame else "", match[2].casefold())
            return True, ""
        if re.search(r"\binto\s+:[A-Za-z_]", candidate, re.I):
            code = "sql_into_range_unsupported" if re.search(r"\binto\s+:[A-Za-z_]\w*\s*-\s*:", candidate, re.I) else "sql_into_runtime_required"
            message = "Диапазон макропеременных SELECT INTO требует отдельной модели числа строк" if code.endswith("unsupported") else "SELECT INTO зависит от табличных значений; требуется runtime-этап"
            self._require(code, message, line, frame.name if frame else "")
            return True, candidate
        return False, candidate

    def _find_mend(self, items: list[dict], start: int) -> int | None:
        depth = 0
        for pos in range(start, len(items)):
            masked = items[pos]["masked"].strip()
            if re.match(r"%macro\b", masked, re.I):
                depth += 1
            elif re.match(r"%mend\b", masked, re.I):
                depth -= 1
                if depth == 0:
                    return pos
        return None

    def _definition(self, header: dict, body: list[dict]) -> MacroDefinition:
        match = re.match(r"%macro\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*;", header["text"].strip(), re.I | re.S)
        if not match:
            raise ValueError("Некорректный заголовок %MACRO")
        name = match[1].casefold()
        parameters: list[MacroParameter] = []
        for raw in self._split_arguments(match[2] or ""):
            if not raw.strip():
                continue
            if "=" in raw:
                key, default = raw.split("=", 1)
                parameters.append(MacroParameter(key.strip().casefold(), default.strip()))
            else:
                parameters.append(MacroParameter(raw.strip().casefold()))
        if any(not re.fullmatch(r"[A-Za-z_]\w*", parameter.name) for parameter in parameters):
            raise ValueError("Некорректный параметр %MACRO")
        revision = self._definition_revisions.get(name, 0) + 1
        self._definition_revisions[name] = revision
        body_text = "\n".join(item["masked"] for item in body)
        runtime_effect = bool(re.search(r"&\s*syserr\b|%abort\b|\bfilename\b|\bemail\b|call\s+(?:execute|dosubl)\b|%include\b", body_text, re.I))
        return MacroDefinition(name, tuple(parameters), tuple(body), self._program_id, header["line"], revision, runtime_effect)

    def _execute_let(self, text: str, frame: MacroFrame | None, line: int) -> None:
        match = re.match(r"%let\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s*;\s*$", text, re.I | re.S)
        if not match:
            self._require("macro_let_syntax", "Не удалось разобрать %LET", line, frame.name if frame else "")
            return
        name = match[1].casefold()
        value = self._expand_text(match[2], frame, line)
        if value is UNKNOWN:
            self._require("macro_value_unknown", "Значение %LET зависит от неразрешённого макроконтекста", line, frame.name if frame else "")
            return
        self._assign(name, str(value).strip(), frame)
        self._add_trace("assignment", line, frame.name if frame else "", name)

    def _assign(self, name: str, value: str, frame: MacroFrame | None) -> None:
        key = name.casefold()
        current = frame
        while current is not None:
            if key in current.locals or key in current.declared_local:
                current.locals[key] = value
                return
            current = current.parent
        if key in self.globals or frame is None:
            self.globals[key] = value
        else:
            frame.locals[key] = value

    def _lookup(self, name: str, frame: MacroFrame | None) -> str | _Unknown:
        key = name.casefold()
        current = frame
        while current is not None:
            if key in current.locals:
                return current.locals[key]
            current = current.parent
        return self.globals.get(key, UNKNOWN)

    def _call_frame(self, definition: MacroDefinition, raw: str, parent: MacroFrame | None, line: int) -> MacroFrame | None:
        values = self._split_arguments(raw)
        positional: list[str] = []
        named: dict[str, str] = {}
        for value in values:
            if "=" in value and re.match(r"\s*[A-Za-z_]\w*\s*=", value):
                key, item = value.split("=", 1)
                named[key.strip().casefold()] = item.strip()
            elif value.strip() or values:
                positional.append(value.strip())
        if len(positional) > len(definition.parameters):
            self._require("macro_argument_count", "Передано больше позиционных аргументов, чем объявлено", line, definition.name)
            return None
        frame = MacroFrame(
            definition.name,
            call_line=line,
            definition_line=definition.line,
            parent=parent,
        )
        for index, parameter in enumerate(definition.parameters):
            raw_value = named.pop(parameter.name, positional[index] if index < len(positional) else parameter.default)
            if raw_value is None:
                self._require("macro_argument_missing", "Не задан обязательный аргумент макроса", line, definition.name)
                return None
            expanded = self._expand_text(raw_value, parent, line)
            if expanded is UNKNOWN:
                self._require("macro_argument_unknown", "Аргумент макроса не разрешён", line, definition.name)
                return None
            frame.locals[parameter.name] = str(expanded)
            frame.declared_local.add(parameter.name)
        if named:
            self._require("macro_argument_unknown_name", "Передан неизвестный именованный аргумент макроса", line, definition.name)
            return None
        return frame

    def _do_body(self, items: list[dict], start: int) -> tuple[list[dict] | None, int]:
        depth = 1
        body: list[dict] = []
        pos = start + 1
        while pos < len(items):
            masked = items[pos]["masked"].strip()
            opens = bool(re.match(r"%do\b", masked, re.I) or re.match(r"%(?:if|else\s+if).*?%then\s+%do\b", masked, re.I | re.S) or re.match(r"%else\s+%do\b", masked, re.I))
            if opens:
                depth += 1
            if re.match(r"%end\b", masked, re.I):
                depth -= 1
                if depth == 0:
                    return body, pos + 1
            body.append(items[pos])
            pos += 1
        return None, start + 1

    def _execute_do(self, header: str, body: list[dict], frame: MacroFrame | None, depth: int, line: int) -> list[str]:
        text = header.strip().rstrip(";").strip()
        counted = re.match(r"%do\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s+%to\s+(.*?)(?:\s+%by\s+(.*))?$", text, re.I | re.S)
        while_match = re.match(r"%do\s+%while\s*\((.*)\)\s*$", text, re.I | re.S)
        if counted:
            name = counted[1].casefold()
            start = self._integer(counted[2], frame, line)
            end = self._integer(counted[3], frame, line)
            step = self._integer(counted[4] or "1", frame, line)
            if UNKNOWN in {start, end, step} or step == 0:
                self._require("macro_loop_bounds", "Границы или шаг %DO не разрешены", line, frame.name if frame else "")
                return [header] + [item["text"] for item in body] + ["%end;"]
            count = max(0, ((end - start) // step) + 1) if (end - start) * step >= 0 else 0
            if count > self.max_iterations:
                self._require("macro_loop_limit", "Число итераций %DO превышает лимит", line, frame.name if frame else "")
                return [header] + [item["text"] for item in body] + ["%end;"]
            output: list[str] = []
            for iteration, value in enumerate(range(start, end + (1 if step > 0 else -1), step), 1):
                self._assign(name, str(value), frame)
                self._add_trace("iteration", line, frame.name if frame else "", f"{name}:{iteration}")
                output.extend(self._execute(body, frame, depth + 1))
            return output
        if while_match:
            output: list[str] = []
            for iteration in range(1, self.max_iterations + 1):
                condition = self._condition(while_match[1], frame, line)
                if condition is UNKNOWN:
                    self._require("macro_loop_condition", "Условие %DO %WHILE не разрешено", line, frame.name if frame else "")
                    return [header] + [item["text"] for item in body] + ["%end;"]
                if not condition:
                    return output
                self._add_trace("iteration", line, frame.name if frame else "", f"while:{iteration}")
                output.extend(self._execute(body, frame, depth + 1))
            self._require("macro_loop_limit", "%DO %WHILE не завершился в пределах лимита", line, frame.name if frame else "")
            return [header] + [item["text"] for item in body] + ["%end;"]
        if re.fullmatch(r"%do", text, re.I):
            return self._execute(body, frame, depth + 1)
        self._require("macro_do_variant", "Вариант %DO пока не поддержан", line, frame.name if frame else "")
        return [header] + [item["text"] for item in body] + ["%end;"]

    def _parse_if(self, items: list[dict], start: int, else_prefix: bool = False):
        raw = items[start]["text"].strip()
        if else_prefix or re.match(r"%else\s+%if\b", raw, re.I):
            raw = re.sub(r"^%else\s+", "", raw, flags=re.I)
        match = re.match(r"%if\s+(.*?)\s+%then\s+(.*?)\s*;\s*$", raw, re.I | re.S)
        if not match:
            return None, start + 1
        condition, action = match[1], match[2].strip()
        if re.fullmatch(r"%do", action, re.I):
            yes, following = self._do_body(items, start)
            if yes is None:
                return None, start + 1
        else:
            yes = [self._pseudo_item(action + ";", items[start])]
            following = start + 1
        no: list[dict] | tuple | None = []
        if following < len(items) and re.match(r"%else\b", items[following]["masked"].strip(), re.I):
            other = items[following]["text"].strip()
            if re.match(r"%else\s+%if\b", other, re.I):
                nested, following = self._parse_if(items, following, True)
                no = ("if", nested) if nested is not None else None
            else:
                else_action = re.sub(r"^%else\s+", "", other, flags=re.I).rstrip(";").strip()
                if re.fullmatch(r"%do", else_action, re.I):
                    no, following = self._do_body(items, following)
                else:
                    no = [self._pseudo_item(else_action + ";", items[following])]
                    following += 1
        return {"condition": condition, "yes": yes, "no": no, "line": items[start]["line"]}, following

    def _execute_if_node(self, node: dict, frame: MacroFrame | None, depth: int,
                         original_items: list[dict] | None = None) -> list[str]:
        condition = self._condition(node["condition"], frame, node["line"])
        if condition is UNKNOWN:
            self._require("macro_condition_unknown", "Условие %IF зависит от неизвестного значения", node["line"], frame.name if frame else "")
            preserved: list[str] = []
            for item in original_items or []:
                self._emit(preserved, item["text"], item["line"], frame)
            return preserved
        self._add_trace("branch", node["line"], frame.name if frame else "", "then" if condition else "else")
        branch = node["yes"] if condition else node["no"]
        if isinstance(branch, tuple) and branch and branch[0] == "if":
            return self._execute_if_node(branch[1], frame, depth + 1, original_items)
        if not isinstance(branch, list):
            return []
        return self._execute(branch, frame, depth + 1)

    @staticmethod
    def _pseudo_item(text: str, source: dict) -> dict:
        return {**source, "text": text, "masked": text}

    def _integer(self, text: str, frame: MacroFrame | None, line: int):
        expanded = self._expand_text(text, frame, line)
        if expanded is UNKNOWN:
            return UNKNOWN
        try:
            return int(str(expanded).strip())
        except ValueError:
            try:
                return int(self._safe_arithmetic(str(expanded)))
            except (ValueError, TypeError):
                return UNKNOWN

    def _condition(self, text: str, frame: MacroFrame | None, line: int):
        expanded = self._expand_text(text, frame, line)
        if expanded is UNKNOWN:
            return UNKNOWN
        return self._condition_value(str(expanded).strip())

    def _condition_value(self, value: str):
        value = self._strip_outer_parentheses(value.strip())
        parts = self._split_logical(value, "or")
        if len(parts) > 1:
            values = [self._condition_value(part) for part in parts]
            if any(item is True for item in values):
                return True
            return UNKNOWN if any(item is UNKNOWN for item in values) else False
        parts = self._split_logical(value, "and")
        if len(parts) > 1:
            values = [self._condition_value(part) for part in parts]
            if any(item is False for item in values):
                return False
            return UNKNOWN if any(item is UNKNOWN for item in values) else True
        if re.match(r"^not\b", value, re.I):
            child = self._condition_value(re.sub(r"^not\b", "", value, flags=re.I).strip())
            return UNKNOWN if child is UNKNOWN else not child
        match = re.match(r"^(.*?)(>=|<=|\^=|~=|\bne\b|\beq\b|\bgt\b|\blt\b|\bge\b|\ble\b|=|>|<)(.*)$", value, re.I | re.S)
        if match:
            left, operator, right = match[1].strip(), match[2].casefold(), match[3].strip()
            left_value: Any = int(left) if re.fullmatch(r"[+-]?\d+", left) else left
            right_value: Any = int(right) if re.fullmatch(r"[+-]?\d+", right) else right
            if type(left_value) is not type(right_value):
                left_value, right_value = str(left_value), str(right_value)
            return {
                "=": left_value == right_value,
                "eq": left_value == right_value,
                "^=": left_value != right_value,
                "~=": left_value != right_value,
                "ne": left_value != right_value,
                ">": left_value > right_value,
                "gt": left_value > right_value,
                "<": left_value < right_value,
                "lt": left_value < right_value,
                ">=": left_value >= right_value,
                "ge": left_value >= right_value,
                "<=": left_value <= right_value,
                "le": left_value <= right_value,
            }[operator]
        if not value:
            return False
        if re.fullmatch(r"[+-]?\d+", value):
            return int(value) != 0
        return UNKNOWN

    def _expand_text(self, text: str, frame: MacroFrame | None, line: int):
        value: str | _Unknown = text
        for _ in range(50):
            if value is UNKNOWN:
                return UNKNOWN
            previous = value
            value = self._expand_references(value, frame, line)
            if value is UNKNOWN:
                return UNKNOWN
            value = self._expand_functions(value, frame, line)
            if value is UNKNOWN or value == previous:
                return value
        self._require("macro_resolution_limit", "Превышен лимит косвенного раскрытия макропеременных", line, frame.name if frame else "")
        return UNKNOWN

    def _expand_references(self, text: str, frame: MacroFrame | None, line: int):
        output: list[str] = []
        pos = 0
        single = False
        while pos < len(text):
            char = text[pos]
            if char == "'":
                if single and pos + 1 < len(text) and text[pos + 1] == "'":
                    output.append("''")
                    pos += 2
                    continue
                single = not single
                output.append(char)
                pos += 1
                continue
            if not single and char == "&":
                match = re.match(r"&([A-Za-z_]\w*)\.?", text[pos:])
                if match:
                    value = self._lookup(match[1], frame)
                    if value is UNKNOWN:
                        self._require("macro_variable_unknown", "Макропеременная не определена в активной области", line, frame.name if frame else "")
                        return UNKNOWN
                    output.append(value)
                    pos += match.end()
                    continue
            output.append(char)
            pos += 1
        return "".join(output)

    def _expand_functions(self, text: str, frame: MacroFrame | None, line: int):
        pos = 0
        single = False
        while pos < len(text):
            if text[pos] == "'":
                if single and pos + 1 < len(text) and text[pos + 1] == "'":
                    pos += 2
                    continue
                single = not single
                pos += 1
                continue
            if not single and text[pos] == "%":
                match = re.match(r"%([A-Za-z_]\w*)\s*\(", text[pos:])
                if match:
                    opening = pos + match.end() - 1
                    closing = self._matching_parenthesis(text, opening)
                    if closing is None:
                        self._require("macro_function_parenthesis", "Макрофункция имеет незакрытую скобку", line, frame.name if frame else "")
                        return UNKNOWN
                    content = text[opening + 1 : closing]
                    result = self._macro_function(match[1].casefold(), content, frame, line)
                    if result is UNKNOWN:
                        return UNKNOWN
                    text = text[:pos] + str(result) + text[closing + 1 :]
                    continue
            pos += 1
        return text

    def _macro_function(self, name: str, content: str, frame: MacroFrame | None, line: int):
        if name == "sysfunc":
            expanded = self._expand_text(content, frame, line)
            if expanded is UNKNOWN:
                return UNKNOWN
            return self._sysfunc(str(expanded), line, frame)
        args = self._split_arguments(content)
        resolved: list[str] = []
        for argument in args:
            value = self._expand_text(argument, frame, line)
            if value is UNKNOWN:
                return UNKNOWN
            resolved.append(str(value))
        if name == "scan" and len(resolved) >= 2:
            try:
                index = int(resolved[1])
            except ValueError:
                return UNKNOWN
            delimiter = resolved[2] if len(resolved) > 2 and resolved[2] else None
            words = resolved[0].split(delimiter) if delimiter else resolved[0].split()
            return words[index - 1] if 1 <= index <= len(words) else ""
        if name == "eval" and len(resolved) == 1:
            try:
                return str(int(self._safe_arithmetic(resolved[0])))
            except (ValueError, TypeError):
                return UNKNOWN
        if name == "length" and len(resolved) == 1:
            return str(len(resolved[0]))
        if name == "substr" and 2 <= len(resolved) <= 3:
            try:
                start = int(resolved[1]) - 1
                length = int(resolved[2]) if len(resolved) == 3 else None
            except ValueError:
                return UNKNOWN
            return resolved[0][start:] if length is None else resolved[0][start : start + length]
        if name == "upcase" and len(resolved) == 1:
            return resolved[0].upper()
        if name == "lowcase" and len(resolved) == 1:
            return resolved[0].lower()
        self._require("macro_function_unsupported", "Макрофункция не входит в детерминированный allowlist", line, name)
        return UNKNOWN

    def _sysfunc(self, value: str, line: int, frame: MacroFrame | None):
        match = re.match(r"\s*([A-Za-z_]\w*)\s*\((.*)\)\s*(?:,.*)?$", value, re.I | re.S)
        if not match:
            self._require("sysfunc_syntax", "Не удалось разобрать вызов %SYSFUNC", line, frame.name if frame else "")
            return UNKNOWN
        name = match[1].casefold()
        args = [argument.strip() for argument in self._split_arguments(match[2])]
        if name in {"today", "date"} and not args:
            if self.run_date is None:
                self._require("run_date_required", "Для DATE/TODAY нужна зафиксированная дата запуска", line, frame.name if frame else "")
                return UNKNOWN
            return str((self.run_date - date(1960, 1, 1)).days)
        if name == "countw" and args:
            return str(len(args[0].split()))
        if name == "exist" and args:
            key = _canonical_dataset(args[0])
            if key in self.catalog:
                return "1"
            if self.catalog_complete:
                return "0"
            self._require("catalog_completeness_required", "Нельзя отличить отсутствующую таблицу от неполного снимка каталога", line, frame.name if frame else "")
            return UNKNOWN
        if name in {"year", "month"} and len(args) == 1 and re.fullmatch(r"[+-]?\d+", args[0]):
            actual = date(1960, 1, 1) + timedelta(days=int(args[0]))
            return str(actual.year if name == "year" else actual.month)
        if name == "putn" and len(args) >= 2 and re.fullmatch(r"[+-]?\d+", args[0]):
            format_name = args[1].strip().casefold()
            width = re.fullmatch(r"z(\d+)\.", format_name)
            if width:
                return f"{int(args[0]):0{int(width[1])}d}"
        if name == "strip" and len(args) == 1:
            return args[0].strip()
        if name == "compress" and args:
            remove = args[1] if len(args) > 1 else " "
            return "".join(char for char in args[0] if char not in set(remove))
        if name == "substr" and 2 <= len(args) <= 3:
            try:
                start = int(args[1]) - 1
                length = int(args[2]) if len(args) == 3 else None
            except ValueError:
                return UNKNOWN
            return args[0][start:] if length is None else args[0][start : start + length]
        self._require("sysfunc_unsupported", "Функция %SYSFUNC не входит в реализованный allowlist или имеет неизвестные аргументы", line, name)
        return UNKNOWN

    def _find_data_run(self, items: list[dict], start: int) -> int | None:
        for pos in range(start + 1, len(items)):
            if re.fullmatch(r"run\s*;", items[pos]["masked"].strip(), re.I):
                return pos
            if re.match(r"(?:data|proc)\b", items[pos]["masked"].strip(), re.I):
                return None
        return None

    def _execute_data_null(self, items: list[dict], frame: MacroFrame | None, line: int) -> bool:
        env: dict[str, Any] = {}
        pending: list[tuple[str, str]] = []
        for item in items[1:-1]:
            text = item["text"].strip()
            expanded = self._expand_text(text, frame, item["line"])
            if expanded is UNKNOWN:
                return False
            assignment = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.*?)\s*;", str(expanded), re.I | re.S)
            symput = re.fullmatch(r"call\s+(symputx?)\s*\(\s*(['\"])([A-Za-z_]\w*)\2\s*,\s*(.*?)\s*(?:,\s*(['\"])[glf]\5\s*)?\)\s*;", str(expanded), re.I | re.S)
            if assignment:
                value = self._data_expression(assignment[2], env)
                if value is UNKNOWN:
                    return False
                env[assignment[1].casefold()] = value
            elif symput:
                # G/L/F scope selection needs the DATA-step symbol-table
                # lifetime; do not silently assign it in the default scope.
                if symput[5] is not None:
                    return False
                value = self._data_expression(symput[4], env)
                if value is UNKNOWN:
                    return False
                rendered = self._format_scalar(value)
                if symput[1].casefold() == "symputx":
                    rendered = rendered.strip()
                pending.append((symput[3].casefold(), rendered))
            else:
                return False
        for name, value in pending:
            self._assign(name, value, frame)
            self._add_trace("symput", line, frame.name if frame else "", name)
        self._add_trace("data_null_partial_evaluation", line, frame.name if frame else "", f"symbols={len(pending)}")
        return True

    def _data_expression(self, text: str, env: dict[str, Any]):
        # SAS doubles the delimiting quote and does not interpret Python
        # backslash escapes. Keep literal payloads out of Python's parser.
        literals: dict[str, str] = {}
        pieces = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", text)
        for index in range(1, len(pieces), 2):
            token = pieces[index]
            name = f"__sas_literal_{len(literals)}"
            literals[name] = token[1:-1].replace(token[0] * 2, token[0])
            pieces[index] = name
        normalized = "".join(pieces).replace("||", "+")
        normalized = re.sub(r"(?i)(,\s*)([A-Za-z_]\w*\d*\.)\s*(?=\))", r"\1'\2'", normalized)
        # SAS accepts decimal integer literals such as 01. Python's AST does
        # not, so normalize only unquoted numeric tokens before safe parsing.
        pieces = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", normalized)
        for index in range(0, len(pieces), 2):
            pieces[index] = re.sub(r"(?<![\w.])0+(\d+)(?![\w.])", r"\1", pieces[index])
        normalized = "".join(pieces)
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError:
            return UNKNOWN

        def visit(node):
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in {str, int, float}:
                return node.value
            if isinstance(node, ast.Name):
                if node.id in literals:
                    return literals[node.id]
                return env.get(node.id.casefold(), UNKNOWN)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                value = visit(node.operand)
                if value is UNKNOWN or not isinstance(value, (int, float)):
                    return UNKNOWN
                return -value if isinstance(node.op, ast.USub) else value
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                left, right = visit(node.left), visit(node.right)
                if UNKNOWN in {left, right}:
                    return UNKNOWN
                try:
                    if isinstance(node.op, ast.Add):
                        return left + right
                    if isinstance(node.op, ast.Sub):
                        return left - right
                    if isinstance(node.op, ast.Mult):
                        return left * right
                    return left / right
                except TypeError:
                    return UNKNOWN
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                values = [visit(argument) for argument in node.args]
                if any(value is UNKNOWN for value in values):
                    return UNKNOWN
                return self._data_function(node.func.id.casefold(), values)
            return UNKNOWN

        return visit(tree)

    def _data_function(self, name: str, args: list[Any]):
        if name in {"date", "today"} and not args:
            return UNKNOWN if self.run_date is None else (self.run_date - date(1960, 1, 1)).days
        if name in {"year", "month"} and len(args) == 1 and isinstance(args[0], (int, float)):
            actual = date(1960, 1, 1) + timedelta(days=int(args[0]))
            return actual.year if name == "year" else actual.month
        if name == "mdy" and len(args) == 3:
            try:
                actual = date(int(args[2]), int(args[0]), int(args[1]))
            except (TypeError, ValueError):
                return UNKNOWN
            return (actual - date(1960, 1, 1)).days
        if name == "intnx" and 3 <= len(args) <= 4 and isinstance(args[1], (int, float)) and isinstance(args[2], (int, float)):
            actual = date(1960, 1, 1) + timedelta(days=int(args[1]))
            interval = str(args[0]).casefold()
            shift = int(args[2])
            alignment = str(args[3]).casefold() if len(args) == 4 else "beginning"
            if interval.startswith("month"):
                index = actual.year * 12 + actual.month - 1 + shift
                year, month = divmod(index, 12)
                month += 1
                day = 1 if alignment.startswith(("b", "s")) else calendar.monthrange(year, month)[1]
                if alignment.startswith("s"):
                    day = min(actual.day, calendar.monthrange(year, month)[1])
                return (date(year, month, day) - date(1960, 1, 1)).days
            if interval.startswith("day"):
                return int(args[1]) + shift
            return UNKNOWN
        if name == "put" and len(args) == 2 and isinstance(args[0], (int, float)):
            format_name = str(args[1]).casefold()
            if format_name == "date9.":
                actual = date(1960, 1, 1) + timedelta(days=int(args[0]))
                months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
                return f"{actual.day:02d}{months[actual.month - 1]}{actual.year:04d}"
            width = re.fullmatch(r"(\d+)\.", format_name)
            if width:
                return f"{int(args[0]):{int(width[1])}d}"
            return UNKNOWN
        if name == "compress" and args:
            remove = str(args[1]) if len(args) > 1 else " "
            return "".join(char for char in str(args[0]) if char not in set(remove))
        return UNKNOWN

    @staticmethod
    def _format_scalar(value: Any) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @staticmethod
    def _safe_arithmetic(value: str):
        tree = ast.parse(value, mode="eval")

        def visit(node):
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                return node.value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                item = visit(node.operand)
                return -item if isinstance(node.op, ast.USub) else item
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div, ast.Mod)):
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Mod):
                    return left % right
                return left // right if isinstance(node.op, ast.FloorDiv) else left / right
            raise ValueError("unsupported arithmetic")

        return visit(tree)

    @staticmethod
    def _matching_parenthesis(value: str, opening: int) -> int | None:
        depth = 0
        quote = ""
        pos = opening
        while pos < len(value):
            char = value[pos]
            if quote:
                if char == quote:
                    if pos + 1 < len(value) and value[pos + 1] == quote:
                        pos += 2
                        continue
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return pos
            pos += 1
        return None

    @classmethod
    def _split_arguments(cls, value: str) -> list[str]:
        if value == "":
            return []
        result: list[str] = []
        start = 0
        depth = 0
        quote = ""
        pos = 0
        while pos < len(value):
            char = value[pos]
            if quote:
                if char == quote:
                    if pos + 1 < len(value) and value[pos + 1] == quote:
                        pos += 2
                        continue
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                result.append(value[start:pos].strip())
                start = pos + 1
            pos += 1
        result.append(value[start:].strip())
        return result

    @classmethod
    def _split_logical(cls, value: str, operator: str) -> list[str]:
        result: list[str] = []
        start = 0
        depth = 0
        quote = ""
        pattern = re.compile(rf"\b{operator}\b", re.I)
        pos = 0
        while pos < len(value):
            char = value[pos]
            if quote:
                if char == quote:
                    quote = ""
                pos += 1
                continue
            if char in "'\"":
                quote = char
                pos += 1
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0:
                match = pattern.match(value, pos)
                if match:
                    result.append(value[start:pos].strip())
                    start = match.end()
                    pos = match.end()
                    continue
            pos += 1
        if result:
            result.append(value[start:].strip())
            return result
        return [value]

    @staticmethod
    def _strip_outer_parentheses(value: str) -> str:
        while value.startswith("(") and value.endswith(")"):
            closing = MacroProcessor._matching_parenthesis(value, 0)
            if closing != len(value) - 1:
                break
            value = value[1:-1].strip()
        return value

