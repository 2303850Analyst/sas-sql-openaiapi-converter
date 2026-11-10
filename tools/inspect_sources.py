"""Read-only fixture inventory, NOT a SAS parser, translator or SQL validator.

Only code feature names/counts/line numbers and selected project metadata leave
memory. Logs, ODS results, XML authors and source text are not exported.
7-Zip is optional and is used only to read a user-selected SQL archive to stdout.
This development utility is not the production upload security boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

LIMIT = 32 * 1024 * 1024


class LexicalError(ValueError):
    def __init__(self, message: str, offset: int):
        super().__init__(message)
        self.offset = offset


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_source(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1251"), "cp1251"


def mask_noncode(text: str, *, sas: bool, inline_regions=None) -> str:
    """Conservative lexical scan: keep newlines/offsets, mask comments/literals.

    Not macro expansion. Macro quoting, vendor-specific escaping and embedded
    languages require the future semantic parser.
    """
    out = list(text)
    i, at_start = 0, True
    while i < len(text):
        start = i
        data_match = re.match(r"(?:datalines4|cards4|datalines|cards)\s*;", text[i:], re.I) if sas and at_start else None
        if data_match:
            marker = ";;;;" if "4" in data_match.group() else ";"
            end = re.search(r"(?m)^\s*" + re.escape(marker), text[i + data_match.end():])
            if end is None:
                raise LexicalError("Unterminated inline data", start)
            i += data_match.end() + end.end()
            if inline_regions is not None:
                inline_regions.append((start, i, data_match.group()))
            at_start = True
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise LexicalError("Unterminated block comment", start)
            if text.find("/*", i + 2, end) >= 0:
                raise LexicalError("Nested comment syntax requires SAS-aware review", start)
            i = end + 2
        elif text.startswith("*/", i):
            raise LexicalError("Unexpected comment terminator", start)
        elif not sas and text.startswith("--", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
        elif sas and (text.startswith("%*", i) or (at_start and text[i] == "*")):
            end = text.find(";", i + 1)
            if end < 0:
                raise LexicalError("Unterminated SAS statement comment", start)
            i = end + 1
        elif text[i] in "\"'":
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise LexicalError("Unterminated quoted text", start)
            at_start = False
        else:
            if not text[i].isspace():
                at_start = text[i] == ";"
            i += 1
            continue
        for pos in range(start, i):
            if out[pos] not in "\r\n":
                out[pos] = " "
    return "".join(out)


SAS_FEATURES = {
    "data_step": r"(?:^|;)\s*data\s+",
    "merge": r"(?:^|;)\s*merge\s+",
    "first_last": r"\b(?:first|last)\.",
    "transpose": r"\bproc\s+transpose\b",
    "macro_definition": r"%macro\b",
    "macro_include": r"%include\b",
    "runtime_table_exists": r"%sysfunc\s*\(\s*exist\s*\(",
    "calculated_alias": r"\bcalculated\b",
    "stop_execution": r"\b(?:abort|endsas)\b",
    "email_side_effect": r"\bfilename\s+\w+\s+email\b",
}
SQL_FEATURES = {
    "create_table": r"\bcreate\s+(?:(?:temp|temporary)\s+)?table\b",
    "drop_table": r"\bdrop\s+table\b",
    "update": r"\bupdate\s+\w",
    "insert": r"\binsert\s+into\b",
    "delete": r"\bdelete\s+from\b",
    "analyze": r"\banalyze\s+\w",
    "commit": r"\bcommit\s*;",
    "distribution": r"\bdistributed\s+(?:by|randomly|replicated)\b",
    "window": r"\bover\s*\(",
}


def features(text: str, *, sas: bool, allow_partial: bool = False) -> dict:
    diagnostics = []
    try:
        masked = mask_noncode(text, sas=sas)
    except LexicalError as exc:
        if not allow_partial:
            raise
        masked = mask_noncode(text[:exc.offset], sas=sas)
        diagnostics.append({"code": "partial_lexical_scan", "reason": str(exc),
                            "stopped_at_line": text.count("\n", 0, exc.offset) + 1})
    patterns = SAS_FEATURES if sas else SQL_FEATURES
    locations = {}
    for name, pattern in patterns.items():
        locations[name] = [masked.count("\n", 0, m.start() + len(m.group()) - len(m.group().lstrip("; \t\r\n"))) + 1
                           for m in re.finditer(pattern, masked, re.I | re.M)]
    result = {"lexical_diagnostics": diagnostics,
              "feature_counts": {k: len(v) for k, v in locations.items()},
              "feature_lines": {k: v for k, v in locations.items() if v}}
    if sas:
        result["procedures"] = dict(sorted(Counter(
            p.lower() for p in re.findall(r"\bproc\s+(\w+)", masked, re.I)
        ).items()))
    return result


def check_name(name: str) -> None:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in name or "\x00" in name:
        raise ValueError("Unsafe archive member name")


def inspect_egp(path: Path) -> dict:
    if path.stat().st_size > LIMIT:
        raise ValueError("Input exceeds development inventory limit")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [e.filename for e in entries]
        if len(entries) > 1000 or sum(e.file_size for e in entries) > LIMIT:
            raise ValueError("Expanded archive exceeds inventory limits")
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive members")
        for name in names:
            check_name(name)
        xml_bytes = archive.read("project.xml")
        xml, _ = decode_source(xml_bytes)
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
            raise ValueError("DTD/entity declarations are not accepted")
        root = ET.fromstring(xml)
        if root.tag != "ProjectCollection":
            raise ValueError("Unsupported EGP XML root")
        elements = root.findall("./Elements/Element")
        labels = {e.findtext("./Element/ID"): e.findtext("./Element/Label")
                  for e in elements}
        nodes, orders, flows, diagnostics = [], [], [], []
        for element in elements:
            kind = element.get("Type", "").rsplit(".", 1)[-1]
            ident = element.findtext("./Element/ID")
            label = labels.get(ident)
            if kind == "ProcessFlowContainer":
                flows.append({"id": ident, "label": label})
            elif kind == "OrderedList":
                ids = [e.text for e in element.findall(".//TASKID")]
                orders.append({"id": ident, "label": label, "task_ids": ids,
                               "task_labels": [labels.get(i) for i in ids]})
                for task_id in ids:
                    if task_id not in labels:
                        diagnostics.append({"code": "unresolved_order_task", "id": task_id})
            elif kind == "CodeTask":
                member = f"{ident}/code.sas"
                node = {"id": ident, "label": label,
                        "flow": labels.get(element.findtext("./Element/Container")),
                        "member": member}
                if member not in names:
                    node["status"] = "source_missing_or_external"
                    diagnostics.append({"code": "source_missing", "id": ident})
                else:
                    raw = archive.read(member)
                    source, encoding = decode_source(raw)
                    findings = features(source, sas=True, allow_partial=True)
                    status = "partial_lexical_inventory" if findings["lexical_diagnostics"] else "inventoried"
                    node.update(status=status, sha256=digest(raw), encoding=encoding,
                                line_count=len(source.splitlines()), **findings)
                    for diagnostic in findings["lexical_diagnostics"]:
                        diagnostics.append({"id": ident, **diagnostic})
                nodes.append(node)
        known_members = {n["member"] for n in nodes}
        orphan_code = [n for n in names if n.lower().endswith(".sas") and n not in known_members]
        if orphan_code:
            diagnostics.append({"code": "unassociated_sas_members", "members": orphan_code})
        return {"file_name": path.name, "sha256": digest(path.read_bytes()),
                "eg_version": root.get("EGVersion"), "flows": flows, "code_tasks": nodes,
                "ordered_lists": orders,
                "element_types": dict(sorted(Counter(e.get("Type") for e in elements).items())),
                "diagnostics": diagnostics,
                "limits": ["lexical_inventory_only", "no_macro_expansion", "no_dependency_graph",
                           "non_CodeTask_payloads_not_parsed", "logs_and_results_not_read"]}


def run_7zip(executable: str, args: list[str]) -> bytes:
    result = subprocess.run([executable, *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise ValueError(f"7-Zip failed with code {result.returncode}; source output suppressed")
    return result.stdout


def inspect_sql_archive(path: Path, executable: str) -> dict:
    if path.stat().st_size > LIMIT:
        raise ValueError("Input exceeds development inventory limit")
    listing = run_7zip(executable, ["l", "-slt", "-ba", "-sccUTF-8", "-p-", "--", str(path)])
    entries = []
    for block in listing.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" in fields:
            check_name(fields["Path"])
            entries.append(fields)
    if len(entries) > 1000 or sum(int(e.get("Size", "0")) for e in entries) > LIMIT:
        raise ValueError("Expanded archive exceeds inventory limits")
    if len({e["Path"] for e in entries}) != len(entries):
        raise ValueError("Duplicate archive members")
    scripts = []
    for entry in sorted(entries, key=lambda e: e["Path"]):
        name = entry["Path"]
        if not name.lower().endswith(".sql"):
            continue
        raw = run_7zip(executable, ["x", "-so", "-spd", "-p-", "--", str(path), name])
        if len(raw) != int(entry["Size"]):
            raise ValueError("Member size mismatch")
        source, encoding = decode_source(raw)
        scripts.append({"member": name, "sha256": digest(raw), "encoding": encoding,
                        "line_count": len(source.splitlines()), **features(source, sas=False, allow_partial=True)})
    return {"file_name": path.name, "sha256": digest(path.read_bytes()), "scripts": scripts,
            "skipped_members": [e["Path"] for e in entries if not e["Path"].lower().endswith(".sql")],
            "limits": ["lexical_inventory_only", "no_SQL_parsing", "no_execution",
                       "source_target_equivalence_not_established"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--egp", type=Path, required=True)
    parser.add_argument("--sql-archive", type=Path)
    parser.add_argument("--seven-zip", default=shutil.which("7z"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.sql_archive and not args.seven_zip:
        parser.error("--seven-zip is required to inspect a 7z archive")
    sources = [p.resolve() for p in (args.egp, args.sql_archive) if p]
    if args.output.resolve() in sources:
        parser.error("Output must not overwrite an input")
    report = {"schema_version": 1, "status": "inventory_only", "egp": inspect_egp(args.egp)}
    if args.sql_archive:
        report["sql_archive"] = inspect_sql_archive(args.sql_archive, args.seven_zip)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Inventory written. Translation and execution have not been performed.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
