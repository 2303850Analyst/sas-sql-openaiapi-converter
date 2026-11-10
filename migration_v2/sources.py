"""Safe EGP intake and immutable content-addressed originals, never projections."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

from tools.inspect_sources import decode_source
from .contracts import digest

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


class SourceVault:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, raw: bytes) -> str:
        key = digest(raw)
        target = self.root / key
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError("Source integrity violation")
            return key
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return key

    def read(self, key: str) -> bytes:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid source key")
        raw = (self.root / key).read_bytes()
        if digest(raw) != key:
            raise ValueError("Source integrity violation")
        return raw

    def text(self, source: dict) -> str:
        raw = self.read(source["sha256"])
        return raw.decode(source["encoding"])


def ingest(name: str, raw: bytes, vault: SourceVault) -> dict:
    if Path(name).suffix.casefold() != ".egp" or not raw or len(raw) > MAX_ARCHIVE:
        raise ValueError("Upload one EGP, at most 32 MiB")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise ValueError("Expanded archive limit")
        names = [i.filename for i in entries]
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError("Duplicate archive member")
        for entry in entries:
            n = entry.filename
            path = PurePosixPath(n.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in n or "\x00" in n:
                raise ValueError("Unsafe archive path")
            if entry.flag_bits & 1 or entry.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Encrypted/symlink archive member")
        xml_name = next((n for n in names if n.casefold() == "project.xml"), None)
        if not xml_name:
            raise ValueError("Project.xml missing")
        xml_text, _ = decode_source(archive.read(xml_name))
        if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
            raise ValueError("DTD and entities forbidden")
        root = ET.fromstring(xml_text)
        if root.tag != "ProjectCollection":
            raise ValueError("Unsupported EGP root")
        elements = root.findall("./Elements/Element")
        ids = [e.findtext("./Element/ID") for e in elements]
        if any(not ident for ident in ids) or len(set(ids)) != len(ids):
            raise ValueError("Missing or duplicate project ID")
        labels = {e.findtext("./Element/ID"): e.findtext("./Element/Label") or "Без названия" for e in elements}
        sources, scenarios, diagnostics, unsupported_nodes = [], [], [], []
        lookup = {n.casefold(): n for n in names}
        for e in elements:
            ident = e.findtext("./Element/ID")
            kind = e.get("Type", "").rsplit(".", 1)[-1]
            if kind == "CodeTask":
                member = lookup.get(f"{ident}/code.sas".casefold())
                item = {"id": ident, "label": labels[ident],
                        "flow": labels.get(e.findtext("./Element/Container")),
                        "member": member, "status": "missing"}
                if member:
                    code = archive.read(member)
                    text, encoding = decode_source(code)
                    if encoding == "utf-8" and code.startswith(b"\xef\xbb\xbf"):
                        encoding = "utf-8-sig"
                    item.update(sha256=vault.put(code), encoding=encoding,
                                bytes=len(code), characters=len(text), status="available")
                else:
                    diagnostics.append({"code": "external_source_missing", "source_id": ident})
                sources.append(item)
            elif kind == "OrderedList":
                scenarios.append({"id": ident, "label": labels[ident],
                                  "source_ids": [x.text for x in e.findall(".//TASKID")],
                                  "origin": "egp_ordered_list"})
            elif kind.endswith("Task"):
                unsupported_nodes.append({"id": ident, "label": labels[ident], "kind": kind,
                                          "status": "metadata_preserved_not_interpreted"})
                diagnostics.append({"code": "unsupported_task_type", "node_id": ident, "kind": kind})
        associated = {s["member"] for s in sources}
        for member in names:
            if member.casefold().endswith(".sas") and member not in associated:
                code = archive.read(member)
                text, encoding = decode_source(code)
                if encoding == "utf-8" and code.startswith(b"\xef\xbb\xbf"):
                    encoding = "utf-8-sig"
                sources.append({"id": "orphan-" + digest(member.encode())[:16], "label": member,
                                "member": member, "status": "available", "flow": None,
                                "sha256": vault.put(code), "encoding": encoding,
                                "bytes": len(code), "characters": len(text)})
                diagnostics.append({"code": "unassociated_source", "member": member})
        if not sources:
            raise ValueError("No SAS programs in EGP")
        snapshot = {"schema_version": 1, "archive_sha256": vault.put(raw),
                    "eg_version": root.get("EGVersion"), "sources": sources,
                    "scenarios": scenarios, "diagnostics": diagnostics,
                    "unsupported_nodes": unsupported_nodes,
                    "element_types": dict(Counter(e.get("Type", "").rsplit(".", 1)[-1] for e in elements)),
                    "limits": ["structural_context_only", "non_CodeTask_payloads_not_interpreted",
                               "external_resources_not_opened", "no_semantic_equivalence"]}
        snapshot["hash"] = digest(snapshot)
        return snapshot
